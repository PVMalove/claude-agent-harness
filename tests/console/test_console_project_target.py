"""Selecting a project only inspects it; the plan addresses that checkout explicitly."""

from __future__ import annotations

import subprocess
from pathlib import Path

from harness.console.project_target import (
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
