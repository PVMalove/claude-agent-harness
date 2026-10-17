"""Behavior tests for the test-author scoped guard."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "harness" / "project" / "hooks" / "test-author-guard.py"
AGENT = ROOT / "harness" / "project" / "agents" / "test-author.md"


def _project(tmp_path: Path) -> Path:
    """Create the smallest configured project used by guard tests."""
    project = tmp_path / "project"
    (project / ".harness").mkdir(parents=True)
    (project / ".claude" / "agents").mkdir(parents=True)
    (project / ".claude" / "hooks").mkdir()
    (project / "tests" / "unit").mkdir(parents=True)
    (project / "src").mkdir()
    (project / ".claude" / "hooks" / "run-test.sh").write_text(
        "#!/bin/sh\n", encoding="utf-8"
    )
    (project / ".claude" / "hooks" / "run-test.sh").chmod(0o755)
    (project / ".claude" / "agents" / "test-author.md").write_bytes(AGENT.read_bytes())
    (project / ".harness" / "project.json").write_text(
        json.dumps(
            {
                "language": "en",
                "base_branch": "main",
                "branch_pattern": "^feature/.+",
                "qa_gate_commands": [],
                "test_paths": ["tests/**"],
                "test_runner": {"command": ["python3", "-m", "pytest", "{test_id}"]},
            }
        ),
        encoding="utf-8",
    )
    return project


def _payload(
    tool: str, tool_input: dict[str, object], *, agent_type: str = "test-author"
) -> dict[str, object]:
    """Build the Claude Code PreToolUse payload for the target subagent."""
    return {
        "agent_type": agent_type,
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
    }


def _run_guard(
    project: Path,
    payload: dict[str, object],
    *,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Call the seed hook exactly as Claude Code does, without shell interpretation."""
    environment = dict(os.environ, CLAUDE_PROJECT_DIR=str(project))
    if extra_env:
        environment.update(extra_env)
    return subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        check=False,
    )


def _deny(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    """Parse a deny answer and enforce the soft-deny process contract."""
    assert result.returncode == 0
    assert result.stderr == ""
    data = cast(dict[str, object], json.loads(result.stdout))
    output = cast(dict[str, object], data["hookSpecificOutput"])
    assert output["hookEventName"] == "PreToolUse"
    assert output["permissionDecision"] == "deny"
    assert isinstance(output["permissionDecisionReason"], str)
    return data


def test_write_inside_test_path_is_allowed(tmp_path: Path) -> None:
    project = _project(tmp_path)

    result = _run_guard(
        project,
        _payload("Write", {"file_path": "tests/unit/test_price.py"}),
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize(
    "file_path",
    ["src/price.py", "tests/../src/price.py", str(Path("src") / "price.py")],
)
def test_write_outside_test_paths_is_softly_denied(
    tmp_path: Path, file_path: str
) -> None:
    project = _project(tmp_path)

    result = _run_guard(
        project,
        _payload("Write", {"file_path": file_path}),
    )

    answer = _deny(result)
    assert "src/price.py" not in json.dumps(answer)


def test_symlink_resolving_outside_test_paths_is_denied(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "tests" / "unit" / "link").symlink_to(
        project / "src", target_is_directory=True
    )

    result = _run_guard(
        project,
        _payload("Edit", {"file_path": "tests/unit/link/price.py"}),
    )

    _deny(result)


def test_only_one_test_runner_command_is_allowed(tmp_path: Path) -> None:
    project = _project(tmp_path)

    allowed = _run_guard(
        project,
        _payload(
            "Bash",
            {"command": ".claude/hooks/run-test.sh tests.test_price.Price.test_total"},
        ),
    )
    assert allowed.returncode == 0
    assert allowed.stdout == ""

    for command in (
        ".claude/hooks/run-test.sh --probe",
        ".claude/hooks/run-test.sh tests.test_price.Price.test_total; touch pwned",
        ".claude/hooks/run-test.sh tests.test_price.Price.test_total extra",
        "pytest tests.test_price.Price.test_total",
    ):
        _deny(_run_guard(project, _payload("Bash", {"command": command})))


def test_bash_wrapper_requires_a_trusted_interpreter(tmp_path: Path) -> None:
    project = _project(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_bash = fake_bin / "bash"
    fake_bash.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_bash.chmod(0o755)

    result = _run_guard(
        project,
        _payload(
            "Bash",
            {
                "command": "bash .claude/hooks/run-test.sh tests.test_price.Price.test_total"
            },
        ),
        extra_env={"PATH": str(fake_bin)},
    )

    _deny(result)
    absolute = _run_guard(
        project,
        _payload(
            "Bash",
            {
                "command": (
                    f"{fake_bash} .claude/hooks/run-test.sh "
                    "tests.test_price.Price.test_total"
                )
            },
        ),
    )
    _deny(absolute)


def test_guard_does_not_change_other_agent_route(tmp_path: Path) -> None:
    project = _project(tmp_path)

    result = _run_guard(
        project,
        _payload(
            "Write",
            {"file_path": "src/price.py"},
            agent_type="developer",
        ),
    )

    assert result.returncode == 0
    assert result.stdout == ""


def test_malformed_payload_is_denied_without_echoing_input(tmp_path: Path) -> None:
    project = _project(tmp_path)
    secret = "secret-canary-value"
    environment = dict(os.environ, CLAUDE_PROJECT_DIR=str(project))

    result = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps({"secret": secret}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        check=False,
    )

    answer = _deny(result)
    assert secret not in json.dumps(answer)


def test_denial_leaves_the_project_and_the_sandbox_untouched(tmp_path: Path) -> None:
    project = _project(tmp_path)
    before = sorted(path.relative_to(project).as_posix() for path in project.rglob("*"))

    result = _run_guard(project, _payload("Write", {"file_path": "src/canary.txt"}))

    _deny(result)
    after = sorted(path.relative_to(project).as_posix() for path in project.rglob("*"))
    assert after == before


def test_missing_agent_fails_closed(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / ".claude" / "agents" / "test-author.md").unlink()

    result = _run_guard(
        project,
        _payload("Write", {"file_path": "tests/unit/test_price.py"}),
    )

    answer = _deny(result)
    output = cast(dict[str, object], answer["hookSpecificOutput"])
    assert output["permissionDecisionReason"] == ("test-author: agent_missing")


@pytest.mark.parametrize("artifact", ["artifacts/**", "tests/**", "src/**"])
def test_artifact_settings_do_not_expand_author_write_access(
    tmp_path: Path, artifact: str
) -> None:
    project = _project(tmp_path)
    (project / "artifacts").mkdir()
    config = project / ".harness/project.json"
    data = json.loads(config.read_text())
    data["test_runner"]["build"] = {
        "command": ["compiler"],
        "artifact_paths": [artifact],
    }
    config.write_text(json.dumps(data))
    denied = _run_guard(project, _payload("Write", {"file_path": "artifacts/out"}))
    _deny(denied)
    if artifact == "artifacts/**":
        assert (
            _run_guard(
                project, _payload("Write", {"file_path": "tests/test_ok.py"})
            ).stdout
            == ""
        )
