"""The real-Claude probe of the clean-room test-author scenario, without calling Claude."""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest

claude_probe = importlib.import_module("scripts.clean_room.claude_probe")
ToolCall = claude_probe.ToolCall

DENY_WRITE = "PreToolUse hook: test-author: write_outside_test_paths"
DENY_BASH = "PreToolUse hook: test-author: runner_only"
RUNNER_RESULT = '{"result":"failed","reason":"test_failed","run_id":"abc123"}'


def _stream(*events: dict[str, object]) -> str:
    return "\n".join(json.dumps(event) for event in events)


def _use(call_id: str, name: str, tool_input: dict[str, object]) -> dict[str, object]:
    block = {"type": "tool_use", "id": call_id, "name": name, "input": tool_input}
    return {"type": "assistant", "message": {"content": [block]}}


def _result(call_id: str, content: object, *, error: bool = False) -> dict[str, object]:
    block = {
        "type": "tool_result",
        "tool_use_id": call_id,
        "content": content,
        "is_error": error,
    }
    return {"type": "user", "message": {"content": [block]}}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    return tmp_path


def _guard_calls(project: Path, **overrides: object) -> list[object]:
    """The transcript of a correct guard run, with the files it leaves behind."""
    (project / "tests" / "allowed-probe.txt").write_text("allowed", encoding="utf-8")
    calls = {
        "write": ToolCall("Write", {"file_path": "src/canary.txt"}, DENY_WRITE, True),
        "bash": ToolCall("Bash", {"command": "touch bash-probe.txt"}, DENY_BASH, True),
        "allowed": ToolCall(
            "Write", {"file_path": "tests/allowed-probe.txt"}, "ok", False
        ),
    }
    for key, value in overrides.items():
        calls[key] = value
    return list(calls.values())


def test_probe_mode_follows_the_two_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(claude_probe.VERIFY_VARIABLE, raising=False)
    monkeypatch.delenv(claude_probe.REQUIRE_VARIABLE, raising=False)
    assert claude_probe.probe_mode() == "off"
    monkeypatch.setenv(claude_probe.VERIFY_VARIABLE, "1")
    assert claude_probe.probe_mode() == "verify"
    monkeypatch.setenv(claude_probe.REQUIRE_VARIABLE, "1")
    assert claude_probe.probe_mode() == "require"
    monkeypatch.setenv(claude_probe.VERIFY_VARIABLE, "0")
    monkeypatch.setenv(claude_probe.REQUIRE_VARIABLE, "0")
    assert claude_probe.probe_mode() == "off"


def test_tool_calls_pair_uses_with_results_in_call_order() -> None:
    stream = _stream(
        {"type": "system", "subtype": "init"},
        _use("a", "Write", {"file_path": "x"}),
        _use("b", "Bash", {"command": "ls"}),
        _result("b", [{"type": "text", "text": "listing"}]),
        _result("a", "denied", error=True),
        _use("c", "Write", {"file_path": "y"}),
    )

    calls = claude_probe.tool_calls("not json\n" + stream)

    assert calls == [
        ToolCall("Write", {"file_path": "x"}, "denied", True),
        ToolCall("Bash", {"command": "ls"}, "listing", False),
        ToolCall("Write", {"file_path": "y"}, "", False),
    ]


def test_guard_calls_pass_when_the_hook_denied_and_allowed_correctly(
    project: Path,
) -> None:
    claude_probe.check_guard_calls(project, _guard_calls(project))


@pytest.mark.parametrize(
    ("override", "message"),
    [
        (
            {
                "write": ToolCall(
                    "Write", {"file_path": "src/canary.txt"}, "created", False
                )
            },
            "did not deny a write",
        ),
        (
            {
                "write": ToolCall(
                    "Write", {"file_path": "src/canary.txt"}, "other error", True
                )
            },
            "did not deny a write",
        ),
        (
            {"bash": ToolCall("Bash", {"command": "touch bash-probe.txt"}, "", False)},
            "did not deny an arbitrary Bash",
        ),
        (
            {
                "allowed": ToolCall(
                    "Write", {"file_path": "tests/allowed-probe.txt"}, DENY_WRITE, True
                )
            },
            "denied a write inside test_paths",
        ),
    ],
)
def test_guard_calls_fail_when_the_hook_did_not_decide_as_designed(
    project: Path, override: dict[str, object], message: str
) -> None:
    with pytest.raises(SystemExit, match=message):
        claude_probe.check_guard_calls(project, _guard_calls(project, **override))


def test_guard_calls_fail_without_a_hook_denial_in_the_transcript(
    project: Path,
) -> None:
    with pytest.raises(SystemExit, match="no Write call"):
        claude_probe.check_guard_calls(project, [])


@pytest.mark.parametrize("leftover", ["src/canary.txt", "bash-probe.txt"])
def test_guard_calls_fail_when_a_denied_file_exists(
    project: Path, leftover: str
) -> None:
    calls = _guard_calls(project)
    (project / leftover).parent.mkdir(parents=True, exist_ok=True)
    (project / leftover).write_text("x", encoding="utf-8")

    with pytest.raises(SystemExit, match="exists although the hook denied it"):
        claude_probe.check_guard_calls(project, calls)


def test_guard_calls_fail_when_the_allowed_write_did_not_land(project: Path) -> None:
    calls = _guard_calls(project)
    (project / "tests" / "allowed-probe.txt").unlink()

    with pytest.raises(SystemExit, match="did not land"):
        claude_probe.check_guard_calls(project, calls)


def _red_project(project: Path, log: str | None) -> str:
    """A run directory with `log`, and the digest of the untouched production source."""
    if log is not None:
        run = project / ".harness" / ".sandboxes" / "runs" / "isolated-test-abc123"
        run.mkdir(parents=True)
        (run / "log.txt").write_text(log, encoding="utf-8")
    return str(claude_probe._digest(project / "src" / "calc.py"))


def _red_calls() -> list[object]:
    command = ".claude/hooks/run-test.sh tests.test_probe.TestProbe.test_total"
    return [ToolCall("Bash", {"command": command}, RUNNER_RESULT, False)]


def test_red_cycle_accepts_a_failed_behavioral_assertion(project: Path) -> None:
    digest = _red_project(project, "AssertionError: 5 != 6\n")

    claude_probe.check_red_cycle(project, _red_calls(), digest)


@pytest.mark.parametrize(
    "log",
    [
        "TypeError: missing 1 required positional argument: 'self'\n",
        "ImportError: x\n",
        "",
    ],
)
def test_red_cycle_rejects_a_test_that_failed_without_a_behavioral_assertion(
    project: Path, log: str
) -> None:
    digest = _red_project(project, log)

    with pytest.raises(SystemExit, match="behavioral assertion"):
        claude_probe.check_red_cycle(project, _red_calls(), digest)


def test_red_cycle_rejects_changed_production_code(project: Path) -> None:
    digest = _red_project(project, "AssertionError: 5 != 6\n")
    (project / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a + b + 1\n", encoding="utf-8"
    )

    with pytest.raises(SystemExit, match="changed production code"):
        claude_probe.check_red_cycle(project, _red_calls(), digest)


@pytest.mark.parametrize(
    ("calls", "message"),
    [
        ([], "no Bash call"),
        (
            [
                ToolCall(
                    "Bash",
                    {"command": ".claude/hooks/run-test.sh x"},
                    '{"result":"passed"}',
                    False,
                )
            ],
            "did not report a failed test",
        ),
        (
            [
                ToolCall(
                    "Bash",
                    {"command": ".claude/hooks/run-test.sh x"},
                    '{"result":"failed"}',
                    False,
                )
            ],
            "no run id",
        ),
    ],
)
def test_red_cycle_needs_a_failed_runner_answer_with_a_run_id(
    project: Path, calls: list[object], message: str
) -> None:
    digest = _red_project(project, "AssertionError\n")

    with pytest.raises(SystemExit, match=message):
        claude_probe.check_red_cycle(project, calls, digest)


def test_claude_environment_writes_each_trust_state(project: Path) -> None:
    resolved = str(project.resolve())
    expected: dict[str, object] = {
        "trusted": {resolved: {"hasTrustDialogAccepted": True}},
        "not-accepted": {resolved: {"hasTrustDialogAccepted": False}},
        "other-project": {"/elsewhere": {"hasTrustDialogAccepted": True}},
    }
    for trust, projects in expected.items():
        environment = claude_probe.claude_environment(project, trust=trust)
        state = Path(environment["HOME"]) / ".claude.json"
        assert json.loads(state.read_text(encoding="utf-8")) == {"projects": projects}
        assert environment["CLAUDE_PROJECT_DIR"] == str(project)
        assert "CLAUDE_CONFIG_DIR" not in environment

    environment = claude_probe.claude_environment(project, trust="no-file")
    assert not (Path(environment["HOME"]) / ".claude.json").exists()
    with pytest.raises(SystemExit, match="unknown trust state"):
        claude_probe.claude_environment(project, trust="bogus")


def test_credentials_are_linked_only_on_request_and_recreated(
    project: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path_factory.mktemp("claude-config")
    (source / ".credentials.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(source))
    link = project / claude_probe.HOME_RELATIVE / ".claude" / ".credentials.json"

    claude_probe.claude_environment(project)
    assert not link.exists() and not link.is_symlink()

    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(project / "stale-target")
    claude_probe.claude_environment(project, credentials=True)
    assert link.resolve() == (source / ".credentials.json").resolve()

    claude_probe.remove_credentials(project)
    assert not link.is_symlink()


def _completed(
    code: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout, stderr)


def test_an_unrequested_probe_is_not_run(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(claude_probe.VERIFY_VARIABLE, raising=False)
    monkeypatch.delenv(claude_probe.REQUIRE_VARIABLE, raising=False)

    claude_probe.verify_scoped_hook(project)

    assert "not requested" in capsys.readouterr().out


def test_a_missing_claude_is_skipped_only_when_not_required(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(claude_probe.shutil, "which", lambda name: None)
    monkeypatch.setenv(claude_probe.VERIFY_VARIABLE, "1")
    monkeypatch.delenv(claude_probe.REQUIRE_VARIABLE, raising=False)
    claude_probe.verify_scoped_hook(project)
    assert "skipped" in capsys.readouterr().out

    monkeypatch.setenv(claude_probe.REQUIRE_VARIABLE, "1")
    with pytest.raises(SystemExit, match="required but claude is unavailable"):
        claude_probe.verify_scoped_hook(project)


def test_a_requested_probe_fails_on_a_nonzero_claude_exit(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_probe.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.setattr(
        claude_probe,
        "run_step",
        lambda *args, **kwargs: _completed(1, "", "usage_limit_reached"),
    )
    monkeypatch.setenv(claude_probe.VERIFY_VARIABLE, "1")
    monkeypatch.delenv(claude_probe.REQUIRE_VARIABLE, raising=False)

    with pytest.raises(SystemExit, match="exited with 1 .*usage_limit_reached"):
        claude_probe.verify_scoped_hook(project)

    link = project / claude_probe.HOME_RELATIVE / ".claude" / ".credentials.json"
    assert not link.is_symlink()


def test_a_verified_probe_cleans_up_what_the_agent_left(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    streams = iter(["guard", "red"])

    def fake_run(
        claude: str, root: Path, env: dict[str, str], prompt: str, tools: str
    ) -> str:
        kind = next(streams)
        if kind == "red":
            (root / "tests" / "test_probe.py").write_text("x", encoding="utf-8")
        return kind

    monkeypatch.setattr(claude_probe.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.setattr(claude_probe, "_run_claude", fake_run)
    monkeypatch.setattr(claude_probe, "tool_calls", lambda stream: [])
    monkeypatch.setattr(claude_probe, "check_guard_calls", lambda root, calls: None)
    monkeypatch.setattr(
        claude_probe, "check_red_cycle", lambda root, calls, digest: None
    )
    monkeypatch.setenv(claude_probe.VERIFY_VARIABLE, "1")

    claude_probe.verify_scoped_hook(project)

    assert "scoped-hook confirmation verified" in capsys.readouterr().out
    assert not (project / "tests" / "test_probe.py").exists()
