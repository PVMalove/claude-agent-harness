"""Selecting a project only inspects it; the plan addresses that checkout explicitly."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness.console.project_target import (
    ProjectTarget,
    inspect_target,
    installation_blockers,
    installation_plan,
)


def test_existing_project_is_adopted_without_touching_its_files(tmp_path: Path) -> None:
    repo = tmp_path / "backend with spaces"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    instructions = repo / "AGENTS.md"
    instructions.write_text("Project-owned instructions", encoding="utf-8")
    target = inspect_target(str(repo))
    plan = installation_plan(target, "backend-orchestration")
    assert plan[:3] == ["harness", "adopt", str(repo.resolve())]
    assert plan[-2:] == ["--capability", "backend-orchestration"]
    assert "--replace-conflicts" not in plan
    assert instructions.read_text() == "Project-owned instructions"
    assert not (repo / ".harness").exists()


def test_foreign_discovery_directory_is_blocked_before_install(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    own = tmp_path / ".agents/skills/own-skill"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_text("preserve")
    target = inspect_target(str(tmp_path))
    assert ".agents/skills" in installation_blockers(target, "backend-orchestration")
    assert not (tmp_path / ".harness").exists()
    assert (own / "SKILL.md").read_text() == "preserve"


def test_a_corrupt_lock_is_an_error_with_a_next_step_not_a_process_exit(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness/harness.lock").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="harness.lock повреждён") as checked:
        inspect_target(str(tmp_path))
    assert "«Диагностика» (3)" in str(checked.value)
    # The install path reads the same lock again: it must fail the same way, never exit.
    target = ProjectTarget(tmp_path, "update", ("pvmalove-suite",), ())
    with pytest.raises(ValueError, match="harness.lock повреждён") as planned:
        installation_blockers(target, "pvmalove-suite")
    assert "«Диагностика» (3)" in str(planned.value)
