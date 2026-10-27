"""ChangeImpact behavioral hooks (ADR 0024, section 2a): impact-first-touch, targeted-check-reminder
and sensitive-path-recheck.

The public seam is the hook entry point: the installed `.claude/hooks/<hook>.sh` script, or the
module `change_impact_hooks.py` that it runs, receives the runtime JSON on stdin. The verdict is the
last stderr line of the envelope (`hook_response.py`); stdout carries only the runtime protocol.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from harness.orchestration import hook_policy
from harness.orchestration.hook_policy import parse_hook_response
from harness.repo_map import change_impact

HOOKS = Path(__file__).resolve().parents[2] / "harness" / "project" / "hooks"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="the hooks are bash scripts")

FIRST_TOUCH = "impact-first-touch"
REMINDER = "targeted-check-reminder"
SENSITIVE = "sensitive-path-recheck"
PROFILES = ("minimal", "standard", "strict")
ROLES = ("developer", "review", "qa", "coordinator")
# ADR 0024, section 2a: the decision of each hook per profile.
MATRIX = {
    "minimal": {FIRST_TOUCH: "allow", REMINDER: "allow", SENSITIVE: "allow"},
    "standard": {FIRST_TOUCH: "warn", REMINDER: "warn", SENSITIVE: "allow"},
    "strict": {FIRST_TOUCH: "warn", REMINDER: "warn", SENSITIVE: "warn"},
}


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "change_impact_hooks_unit", HOOKS / "change_impact_hooks.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


hooks = _load()


def _project(root: Path, policy: object | None = None) -> Path:
    """An installed project: a git checkout with `.claude/hooks/` and an optional hook_policy."""
    shutil.copytree(
        HOOKS,
        root / ".claude" / "hooks",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (root / ".git").mkdir()
    if policy is not None:
        config = root / ".harness" / "project.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"hook_policy": policy}), encoding="utf-8")
    return root


def _identity(role: str) -> dict[str, str]:
    """The coordinator is the main agent; an in-process role is a subagent with its own agent_id."""
    if role == "coordinator":
        return {"session_id": "session-main"}
    return {
        "session_id": "session-main",
        "agent_id": f"agent-{role}",
        "agent_type": role,
    }


def _edit(path: Path, role: str = "developer", **extra: object) -> dict[str, object]:
    return {
        **_identity(role),
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": str(path), "old_string": "a", "new_string": "b"},
        "cwd": str(path.parent),
        **extra,
    }


def _stop(role: str = "developer", **extra: object) -> dict[str, object]:
    event = "Stop" if role == "coordinator" else "SubagentStop"
    return {
        **_identity(role),
        "hook_event_name": event,
        "stop_hook_active": False,
        **extra,
    }


def _script(
    project: Path, hook_id: str, payload: object, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the installed hook script the way the runtime does: JSON on stdin."""
    return subprocess.run(
        [str(BASH), str(project / ".claude" / "hooks" / f"{hook_id}.sh")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=project,
        env={**os.environ, **(env or {})},
        check=False,
        timeout=60,
    )


def _body(
    project: Path,
    hook_id: str,
    payload: object,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str]:
    """Run the hook body in process (the module entry point) against ``project``."""
    monkeypatch.setattr(hooks, "project_root", lambda: project)
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    code = hooks.main([hook_id])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _write(path: Path, text: str = "x = 1\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# Each path with its expected ChangeImpact path code and sensitivity.
CLASSIFIED = [
    ("pyproject.toml", "build-config-changed", True),
    ("uv.lock", "build-config-changed", True),
    ("Makefile", "build-config-changed", False),
    ("tests/conftest.py", "build-config-changed", False),
    (".github/workflows/verify.yml", "build-config-changed", True),
    ("scripts/verify.py", "build-config-changed", False),
    ("scripts/verification/stages.py", "build-config-changed", False),
    ("harness/CAPABILITIES.json", "capabilities-or-skills-changed", False),
    ("skills/first-party/x/SKILL.md", "capabilities-or-skills-changed", False),
    ("services/x.py", None, False),
    ("docs/guide.md", None, False),
    ("frontend/package.json", None, True),
    ("native/Cargo.lock", None, True),
    ("requirements-dev.txt", None, True),
    ("constraints.txt", None, True),
    ("app/migrations/0001_initial.py", None, True),
    ("db/alembic/env.py", None, True),
    ("services/auth_service.py", None, True),
    ("services/oauth2-client.py", None, True),
    ("api/permissions.py", None, True),
    ("api/rbac/roles.py", None, True),
    ("services/author.py", None, False),
    ("services/authority/x.py", None, False),
    ("docs/CODEOWNERS", None, True),
    (".claude/settings.local.json", None, True),
    (".claude/hooks/x.sh", None, True),
    (".claude/agents/test-author.md", None, True),
    (".codex/hooks.json", None, True),
    (".codex/config.toml", None, True),
    (".harness/project.json", None, True),
    (".harness/orchestration.json", None, False),
]


@pytest.mark.parametrize(("path", "code", "sensitive"), CLASSIFIED)
def test_the_hook_copies_classify_paths_like_the_harness_sources(
    path: str, code: str | None, sensitive: bool
) -> None:
    assert change_impact.path_unknown_code(path) == code
    assert hook_policy.is_sensitive_path(path) is sensitive
    assert hooks.risky_code(path) == code
    assert hooks.is_sensitive(path) is sensitive


def _decision(done: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert done.returncode == 0, done.stderr
    return parse_hook_response(done.stderr)


@needs_bash
@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("profile", PROFILES)
def test_each_profile_gives_each_role_the_decision_of_the_matrix(
    tmp_path: Path, profile: str, role: str
) -> None:
    project = _project(tmp_path, {"profile": profile})
    # The role and the profile variables in the environment change no decision.
    env = {"HOOK_PROFILE": "minimal", "HARNESS_HOOK_PROFILE": "strict", "ROLE": role}
    risky = _write(project / "pyproject.toml", "[project]\n")
    code = _write(project / "services" / "x.py")

    first = _script(project, FIRST_TOUCH, _edit(risky, role), env=env)
    sensitive = _script(project, SENSITIVE, _edit(risky, role), env=env)
    touched = _script(project, REMINDER, _edit(code, role), env=env)
    _write(code, "x = 2\n")
    reminder = _script(project, REMINDER, _stop(role), env=env)

    verdicts = {
        FIRST_TOUCH: _decision(first),
        SENSITIVE: _decision(sensitive),
        REMINDER: _decision(reminder),
    }
    assert {hook: v["decision"] for hook, v in verdicts.items()} == MATRIX[profile]
    assert {v["profile"] for v in verdicts.values()} == {profile}
    assert _decision(touched)["decision"] == "allow"
    assert touched.stdout == ""
    for hook, verdict in verdicts.items():
        if verdict["decision"] == "allow":
            assert verdict["reason_code"] == "ok"
    if MATRIX[profile][FIRST_TOUCH] == "warn":
        assert verdicts[FIRST_TOUCH]["reason_code"] == "risky-path-first-touch"
        context = json.loads(first.stdout)["hookSpecificOutput"]
        assert context["hookEventName"] == "PreToolUse"
        assert "pyproject.toml" in context["additionalContext"]
        assert "permissionDecision" not in context
    else:
        assert first.stdout == ""
    if MATRIX[profile][REMINDER] == "warn":
        assert verdicts[REMINDER]["reason_code"] == "targeted-checks-unreported"
        assert "checks_run" in json.loads(reminder.stdout)["systemMessage"]
    else:
        assert reminder.stdout == ""
    if MATRIX[profile][SENSITIVE] == "warn":
        assert verdicts[SENSITIVE]["reason_code"] == "sensitive-path-touched"
        assert verdicts[SENSITIVE]["evidence_ref"]
        context = json.loads(sensitive.stdout)["hookSpecificOutput"]
        assert "pyproject.toml" in context["additionalContext"]
    else:
        assert sensitive.stdout == ""


@needs_bash
def test_an_inactive_profile_runs_no_body_and_writes_no_state(tmp_path: Path) -> None:
    project = _project(tmp_path, {"profile": "minimal"})
    code = _write(project / "services" / "x.py")

    for hook_id in (FIRST_TOUCH, SENSITIVE, REMINDER):
        _decision(_script(project, hook_id, _edit(code)))

    assert not (project / ".harness" / ".sandboxes").exists()


@needs_bash
def test_a_project_without_hook_policy_gets_the_standard_decisions(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    risky = _write(project / "uv.lock", "")

    assert _decision(_script(project, FIRST_TOUCH, _edit(risky)))["decision"] == "warn"
    assert _decision(_script(project, SENSITIVE, _edit(risky)))["decision"] == "allow"


def test_an_ordinary_path_raises_no_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path)
    plain = _write(project / "services" / "x.py")

    for hook_id in (FIRST_TOUCH, SENSITIVE):
        code, out, err = _body(project, hook_id, _edit(plain), monkeypatch, capsys)
        assert (code, out, err) == (0, "", "")


def test_a_path_outside_a_git_checkout_is_allowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path / "project")
    outside = _write(tmp_path / "elsewhere" / "pyproject.toml", "")

    for hook_id in (FIRST_TOUCH, SENSITIVE, REMINDER):
        code, out, err = _body(project, hook_id, _edit(outside), monkeypatch, capsys)
        assert (code, out, err) == (0, "", "")
    assert not (project / ".harness").exists()


def test_a_risky_path_is_named_relative_to_its_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path / "project")
    worktree = tmp_path / "project" / ".harness" / ".sandboxes" / "worktrees" / "w"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    risky = _write(worktree / "tests" / "conftest.py")

    code, out, err = _body(project, FIRST_TOUCH, _edit(risky), monkeypatch, capsys)

    assert code == 0
    assert err.startswith("[risky-path-first-touch] ")
    assert "tests/conftest.py (build-config-changed)" in err
    context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert ".sandboxes" not in context


def _reminder(
    project: Path,
    payload: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str]:
    return _body(project, REMINDER, payload, monkeypatch, capsys)


def test_the_reminder_names_the_changed_files_without_a_later_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path)
    first = _write(project / "services" / "a.py")
    second = _write(project / "services" / "b.py")
    for path in (first, second):
        assert _reminder(project, _edit(path), monkeypatch, capsys) == (0, "", "")

    code, out, err = _reminder(project, _stop(), monkeypatch, capsys)

    assert code == 0
    assert err.startswith("[targeted-checks-unreported] ")
    message = json.loads(out)["systemMessage"]
    assert "2" in message
    assert str(project) in message
    assert "checks_run" in message
    assert set(json.loads(out)) == {"systemMessage"}


def test_a_report_written_after_the_last_change_silences_the_reminder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path)
    changed = _write(project / "services" / "a.py")
    report = project / ".harness" / ".sandboxes" / "scratch" / "inbox" / "d-1.json"
    _reminder(project, _edit(changed), monkeypatch, capsys)
    _reminder(project, _edit(report), monkeypatch, capsys)
    _write(report, "{}")
    os.utime(changed, ns=(1_000_000_000, 1_000_000_000))

    assert _reminder(project, _stop(), monkeypatch, capsys) == (0, "", "")

    # A change after the report makes the reminder speak again.
    os.utime(changed, ns=(report.stat().st_mtime_ns + 10**9,) * 2)
    code, out, err = _reminder(project, _stop(), monkeypatch, capsys)
    assert err.startswith("[targeted-checks-unreported] ")


def test_task_artifacts_and_harness_state_are_not_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path)
    for path in (
        _write(project / "docs" / "tasks" / "note.md"),
        _write(project / ".harness" / "orchestration.json", "{}"),
    ):
        _reminder(project, _edit(path), monkeypatch, capsys)

    assert _reminder(project, _stop(), monkeypatch, capsys) == (0, "", "")


def test_a_turn_without_edits_or_with_a_removed_file_is_not_reminded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path)
    assert _reminder(project, _stop(), monkeypatch, capsys) == (0, "", "")

    removed = _write(project / "services" / "gone.py")
    _reminder(project, _edit(removed), monkeypatch, capsys)
    removed.unlink()

    assert _reminder(project, _stop(), monkeypatch, capsys) == (0, "", "")


def test_the_touch_journal_belongs_to_one_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path)
    changed = _write(project / "services" / "a.py")
    _reminder(project, _edit(changed, "developer"), monkeypatch, capsys)

    # Another role is another operation: it changed nothing.
    assert _reminder(project, _stop("review"), monkeypatch, capsys) == (0, "", "")
    assert _reminder(project, _stop("coordinator"), monkeypatch, capsys) == (0, "", "")
    assert _reminder(project, _stop("developer"), monkeypatch, capsys)[2].startswith(
        "[targeted-checks-unreported] "
    )


def test_the_store_keeps_at_most_the_newest_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path)
    monkeypatch.setattr(hooks, "MAX_OPERATIONS", 3)
    changed = _write(project / "services" / "a.py")
    store = project / ".harness" / ".sandboxes" / "hook-events"

    for index in range(5):
        payload = _edit(changed, "coordinator", session_id=f"session-{index}")
        _reminder(project, payload, monkeypatch, capsys)
        os.utime(store / hooks.operation_id(f"session-{index}", ""), (index, index))

    kept = sorted(path.name for path in store.iterdir())
    assert kept == sorted(hooks.operation_id(f"session-{i}", "") for i in (2, 3, 4))


def test_a_full_operation_journal_allows_with_a_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path)
    monkeypatch.setattr(hooks, "MAX_RECORDS", 1)
    first = _write(project / "services" / "a.py")
    second = _write(project / "services" / "b.py")
    _reminder(project, _edit(first), monkeypatch, capsys)

    code, out, err = _reminder(project, _edit(second), monkeypatch, capsys)

    assert (code, out) == (0, "")
    assert err and not err.startswith("[")
    operation = project / ".harness" / ".sandboxes" / "hook-events"
    assert len(list(next(operation.iterdir()).iterdir())) == 1


def _warned(result: tuple[int, str, str]) -> bool:
    code, out, err = result
    assert code == 0
    if not err.startswith("["):
        assert out == ""
        return False
    return True


def _triggers(project: Path) -> dict[str, list[dict[str, object]]]:
    """Per hook: the events that lead to its warning (the reminder needs a touch first)."""
    risky = _write(project / "pyproject.toml", "[project]\n")
    code = _write(project / "services" / "x.py")
    return {
        FIRST_TOUCH: [_edit(risky)],
        SENSITIVE: [_edit(risky)],
        REMINDER: [_edit(code), _stop()],
    }


def _fire(
    project: Path,
    hook_id: str,
    events: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str]:
    """Send the events in order; the answer to the last one."""
    result = (0, "", "")
    for payload in events:
        result = _body(project, hook_id, payload, monkeypatch, capsys)
    return result


@pytest.mark.parametrize("hook_id", [FIRST_TOUCH, SENSITIVE, REMINDER])
def test_a_repeated_identical_event_warns_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    hook_id: str,
) -> None:
    project = _project(tmp_path)
    events = _triggers(project)[hook_id]

    first = _fire(project, hook_id, events, monkeypatch, capsys)
    repeated = [_fire(project, hook_id, events, monkeypatch, capsys) for _ in range(3)]

    assert _warned(first)
    assert repeated == [(0, "", "")] * 3


@pytest.mark.parametrize("hook_id", [FIRST_TOUCH, SENSITIVE, REMINDER])
@pytest.mark.parametrize(
    "identity",
    [{"session_id": "session-next"}, {"agent_id": "agent-retry"}],
    ids=["new-session", "new-agent"],
)
def test_a_new_operation_warns_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    hook_id: str,
    identity: dict[str, str],
) -> None:
    project = _project(tmp_path)
    events = _triggers(project)[hook_id]
    assert _warned(_fire(project, hook_id, events, monkeypatch, capsys))

    renewed = [{**payload, **identity} for payload in events]

    assert _warned(_fire(project, hook_id, renewed, monkeypatch, capsys))


def test_a_changed_digest_warns_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path)
    triggers = _triggers(project)
    for hook_id, events in triggers.items():
        assert _warned(_fire(project, hook_id, events, monkeypatch, capsys)), hook_id

    lock = _write(project / "uv.lock", "")
    other_edit = _edit(project / "pyproject.toml")
    other_edit["tool_input"] = {"file_path": str(project / "pyproject.toml"), "x": 1}
    _write(project / "services" / "x.py", "x = 'changed size'\n")
    changed = {
        FIRST_TOUCH: [_edit(lock)],
        SENSITIVE: [other_edit],
        REMINDER: [_stop()],
    }

    for hook_id, events in changed.items():
        assert _warned(_fire(project, hook_id, events, monkeypatch, capsys)), hook_id
    # The first touch is one notification per path: another edit of the same path stays quiet.
    assert not _warned(_fire(project, FIRST_TOUCH, [other_edit], monkeypatch, capsys))


def test_the_warning_names_its_evidence_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path)
    payload = _triggers(project)[SENSITIVE][0]

    _, _, err = _body(project, SENSITIVE, payload, monkeypatch, capsys)

    first, second = err.splitlines()[:2]
    assert first.startswith("[sensitive-path-touched] ")
    reference = second.removeprefix("evidence_ref=")
    assert reference.startswith(".harness/.sandboxes/hook-events/")
    record = json.loads((project / reference).read_text(encoding="utf-8"))
    assert record["hook_id"] == SENSITIVE
    assert record["event"] == "PreToolUse"
    assert record["path"] == "pyproject.toml"
    assert record["session_id"] == "session-main"
    assert record["agent_id"] == "agent-developer"
    operation = hooks.operation_id("session-main", "agent-developer")
    assert record["operation_id"] == operation
    assert len(str(record["subject_digest"])) == 64
    assert record["recorded_at"]


@needs_bash
def test_the_strict_verdict_carries_the_evidence_record(tmp_path: Path) -> None:
    project = _project(tmp_path, {"profile": "strict"})
    risky = _write(project / "Cargo.toml", "")

    verdict = _decision(_script(project, SENSITIVE, _edit(risky)))

    assert verdict["decision"] == "warn"
    evidence = project / str(verdict["evidence_ref"])
    record = json.loads(evidence.read_text(encoding="utf-8"))
    assert (record["hook_id"], record["profile"]) == (SENSITIVE, "strict")


@needs_bash
def test_the_hooks_start_no_make_pytest_or_git(tmp_path: Path) -> None:
    project = _project(tmp_path / "project", {"profile": "strict"})
    sentinel = tmp_path / "started"
    fake = tmp_path / "bin"
    fake.mkdir()
    for program in ("make", "pytest", "git", "uv", "cargo"):
        script = fake / program
        script.write_text(f'#!/bin/sh\necho "$0" >> "{sentinel}"\n', encoding="utf-8")
        script.chmod(0o755)
    env = {"PATH": f"{fake}{os.pathsep}{os.environ['PATH']}"}
    risky = _write(project / "pyproject.toml", "")
    code = _write(project / "services" / "x.py")

    verdicts = [
        _decision(_script(project, hook_id, _edit(path), env=env))
        for hook_id in (FIRST_TOUCH, SENSITIVE, REMINDER)
        for path in (risky, code)
    ]
    verdicts.append(_decision(_script(project, REMINDER, _stop(), env=env)))

    assert {v["decision"] for v in verdicts} == {"warn", "allow"}
    assert not sentinel.exists()


@needs_bash
@pytest.mark.parametrize("role", ["coordinator", "developer"])
def test_the_end_of_a_turn_never_continues_it(tmp_path: Path, role: str) -> None:
    project = _project(tmp_path, {"profile": "strict"})
    changed = _write(project / "services" / "x.py")
    _decision(_script(project, REMINDER, _edit(changed, role)))

    active = _script(project, REMINDER, _stop(role, stop_hook_active=True))
    reminded = _script(project, REMINDER, _stop(role))

    assert (_decision(active)["decision"], active.stdout) == ("allow", "")
    assert _decision(reminded)["decision"] == "warn"
    # Only a message for the user: no block decision, no context that continues the turn.
    assert set(json.loads(reminded.stdout)) == {"systemMessage"}
    assert reminded.returncode == 0


@needs_bash
def test_the_first_touch_does_not_repeat_through_the_envelope(tmp_path: Path) -> None:
    project = _project(tmp_path, {"profile": "strict"})
    risky = _write(project / "Makefile", "all:\n")

    answers = [_script(project, FIRST_TOUCH, _edit(risky)) for _ in range(3)]

    assert [_decision(done)["decision"] for done in answers] == [
        "warn",
        "allow",
        "allow",
    ]
    assert [bool(done.stdout) for done in answers] == [True, False, False]


def test_concurrent_identical_events_warn_once(tmp_path: Path) -> None:
    project = _project(tmp_path)
    payload = json.dumps(_triggers(project)[FIRST_TOUCH][0])
    module = project / ".claude" / "hooks" / "change_impact_hooks.py"

    processes = [
        subprocess.Popen(
            [sys.executable, str(module), FIRST_TOUCH],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(8)
    ]
    answers = [process.communicate(payload, timeout=60) for process in processes]

    assert [process.returncode for process in processes] == [0] * 8
    warned = [err for _, err in answers if err.startswith("[risky-path-first-touch] ")]
    assert len(warned) == 1
    assert sum(1 for out, _ in answers if out) == 1
