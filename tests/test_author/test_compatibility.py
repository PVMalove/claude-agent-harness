"""The test-author's only legitimate calls must pass the project-wide hooks as well as its guard."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import find_bash

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "harness" / "project" / "hooks"
AGENT = ROOT / "harness" / "project" / "agents" / "test-author.md"
PROJECT_WIDE_BASH = (
    "block-public-attribution.sh",
    "block-direct-master.sh",
    "block-pr-merge.sh",
    "check-branch-name.sh",
    "require-qa-gate.sh",
    "require-bounded-check.sh",
    "block-dangerous-git.sh",
)
PROJECT_WIDE_WRITE = ("block-scratch-outside-docs-tasks.sh",)
TEST_ID = "tests.unit.test_price.TestPrice.test_total"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """An installed-shaped project: every seeded hook plus the test-author agent and settings."""
    root = tmp_path / "project"
    (root / ".harness").mkdir(parents=True)
    (root / ".claude" / "agents").mkdir(parents=True)
    shutil.copytree(HOOKS, root / ".claude" / "hooks", ignore=shutil.ignore_patterns("__pycache__"))
    (root / ".claude" / "agents" / "test-author.md").write_bytes(AGENT.read_bytes())
    (root / ".claude" / "hooks" / "run-test.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / ".claude" / "hooks" / "run-test.sh").chmod(0o755)
    (root / "tests" / "unit").mkdir(parents=True)
    (root / "src").mkdir()
    (root / ".harness" / "project.json").write_text(
        json.dumps(
            {
                "language": "en",
                "base_branch": "main",
                "branch_pattern": "^feature/.+",
                "qa_gate_commands": ["python3 -m unittest"],
                "test_paths": ["tests/**"],
                "test_runner": {"command": ["python3", "-m", "pytest", "{test_id}"]},
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", "-b", "feature/issue-1-x"], cwd=root, check=True)
    return root


def _run(project: Path, hook: str, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
    """Run one seeded hook as Claude Code does: JSON on stdin, project dir in the environment."""
    path = project / ".claude" / "hooks" / hook
    command = [sys.executable, str(path)] if hook.endswith(".py") else [find_bash(), str(path)]
    return subprocess.run(
        command,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=dict(os.environ, CLAUDE_PROJECT_DIR=str(project)),
        cwd=project,
        check=False,
    )


def _payload(tool: str, tool_input: dict[str, object]) -> dict[str, object]:
    return {
        "agent_type": "test-author",
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
    }


@pytest.mark.parametrize("hook", PROJECT_WIDE_WRITE)
def test_a_test_path_write_passes_the_project_wide_write_hooks(
    project: Path, hook: str
) -> None:
    result = _run(project, hook, _payload("Write", {"file_path": "tests/unit/test_price.py"}))

    assert result.returncode == 0, result.stderr
    guard = _run(project, "test-author-guard.py", _payload("Write", {"file_path": "tests/unit/test_price.py"}))
    assert guard.stdout == ""


@pytest.mark.parametrize("hook", PROJECT_WIDE_BASH)
def test_the_runner_call_passes_the_project_wide_bash_hooks(project: Path, hook: str) -> None:
    command = f".claude/hooks/run-test.sh {TEST_ID}"

    result = _run(project, hook, _payload("Bash", {"command": command}))

    assert result.returncode == 0, result.stderr
    guard = _run(project, "test-author-guard.py", _payload("Bash", {"command": command}))
    assert guard.stdout == ""


def test_the_project_wide_hooks_still_block_what_they_guard(project: Path) -> None:
    outside = _run(project, "block-scratch-outside-docs-tasks.sh", _payload("Write", {"file_path": "/etc/outside.txt"}))
    full_run = _run(project, "require-bounded-check.sh", _payload("Bash", {"command": "python3 -m unittest"}))

    assert outside.returncode == 2
    assert full_run.returncode == 2


def test_the_project_wide_hooks_are_not_wired_into_the_agent_frontmatter() -> None:
    frontmatter = AGENT.read_text(encoding="utf-8").split("---", 2)[1]

    assert "test-author-guard.py" in frontmatter
    assert not any(hook in frontmatter for hook in PROJECT_WIDE_BASH + PROJECT_WIDE_WRITE)
