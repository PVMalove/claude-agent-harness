"""Clean-room installation contract for the optional Claude Code test-author."""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import cast

from harness.health.project_files import validate_project_json

CLI = Path(__file__).resolve().parents[2] / "harness" / "bin" / "harness.py"
REAL_GIT = shutil.which("git")
PROJECT_FLAGS = [
    "--language",
    "en",
    "--pr-base-branch",
    "main",
    "--branch-pattern",
    "^feature/.+",
    "--qa-gate-command",
    "echo test",
    "--tracker-type",
    "local",
]


def _repo(path: Path) -> Path:
    """Create an empty Git checkout for the installer."""
    path.mkdir(parents=True)
    assert REAL_GIT is not None
    subprocess.run([REAL_GIT, "init", "-q"], cwd=path, check=True)
    return path


def _init(repo: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    """Install pvmalove-suite without interactive prompts."""
    return subprocess.run(
        [
            sys.executable,
            str(CLI),
            "init",
            str(repo),
            "--capability",
            "pvmalove-suite",
            *PROJECT_FLAGS,
            *flags,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        check=False,
    )


def _config(repo: Path) -> dict[str, object]:
    """Read the installed configuration after applying the shared validator."""
    problems: list[str] = []
    validate_project_json(repo, problems)
    assert problems == []
    return cast(
        dict[str, object],
        json.loads((repo / ".harness" / "project.json").read_text(encoding="utf-8")),
    )


def test_install_seeds_test_author_but_leaves_it_disabled_without_opt_in(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "repo")

    result = _init(repo)

    assert result.returncode == 0, result.stderr
    data = _config(repo)
    assert "test_paths" not in data
    assert "test_runner" not in data
    for relative in (
        ".claude/agents/test-author.md",
        ".claude/hooks/test-author-guard.py",
        ".claude/hooks/test-author-preflight.sh",
        ".claude/hooks/run-test.sh",
        "docs/agents/test-author.md",
        ".harness/skills/tdd/SKILL.md",
    ):
        assert (repo / relative).is_file(), relative
    assert (repo / ".claude/hooks/test-author-guard.py").stat().st_mode & stat.S_IXUSR
    assert (repo / ".claude/hooks/test-author-preflight.sh").stat().st_mode & stat.S_IXUSR
    registry = (repo / ".harness/skills/REGISTRY.md").read_text(encoding="utf-8")
    assert "| `tdd` | `.harness/skills/tdd` |" in registry
    settings = (repo / ".claude/settings.local.json").read_text(encoding="utf-8")
    assert "test-author-guard" not in settings


def test_install_writes_both_activation_settings_without_assumed_project_paths(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "repo")

    result = _init(
        repo,
        "--test-path",
        "tests/**",
        "--test-runner-command",
        "python3 -m unittest '{test_id}'",
    )

    assert result.returncode == 0, result.stderr
    data = _config(repo)
    assert data["test_paths"] == ["tests/**"]
    assert data["test_runner"] == {
        "command": ["python3", "-m", "unittest", "{test_id}"]
    }
