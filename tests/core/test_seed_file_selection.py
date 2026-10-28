"""`harness update --force-seed-file` overwrites only the chosen seed files."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

CLI = Path(__file__).resolve().parents[2] / "harness" / "bin" / "harness.py"
RULES = ".claude/rules/token-efficiency.md"
OTHER = ".claude/rules/karpathy-guidelines.md"
PROJECT_FLAGS = [
    "--language",
    "ru",
    "--pr-base-branch",
    "main",
    "--branch-pattern",
    "^feature/.+",
    "--qa-gate-command",
    "true",
]


def _harness(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        stdin=subprocess.DEVNULL,
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    result = _harness(
        "init", str(tmp_path), "--capability", "engineering-suite", *PROJECT_FLAGS
    )
    assert result.returncode == 0, result.stderr
    for name in (RULES, OTHER):
        path = tmp_path / name
        path.write_text(
            path.read_text(encoding="utf-8") + "\nLOCAL\n", encoding="utf-8"
        )
    return tmp_path


def _differing(repo: Path) -> list[str]:
    result = _harness("diff", str(repo), "--json")
    return [str(path) for path in json.loads(result.stdout)["seed_differing"]]


def test_diff_lists_the_seed_files_that_differ_from_the_template(project: Path) -> None:
    assert _differing(project) == sorted([OTHER, RULES])


def test_only_the_named_seed_file_is_overwritten(project: Path) -> None:
    result = _harness("update", str(project), "--force-seed-file", RULES)
    assert result.returncode == 0, result.stderr
    assert "LOCAL" not in (project / RULES).read_text(encoding="utf-8")
    assert "LOCAL" in (project / OTHER).read_text(encoding="utf-8")
    assert _differing(project) == [OTHER]
    # The file left alone is still reported, as before.
    assert OTHER in result.stdout.replace("\\", "/")


def test_several_files_can_be_named(project: Path) -> None:
    result = _harness(
        "update",
        str(project),
        "--force-seed-file",
        RULES,
        "--force-seed-file",
        OTHER,
    )
    assert result.returncode == 0, result.stderr
    assert _differing(project) == []


def test_an_unknown_path_is_refused_before_anything_is_written(project: Path) -> None:
    result = _harness(
        "update",
        str(project),
        "--force-seed-file",
        RULES,
        "--force-seed-file",
        "nope.md",
    )
    assert result.returncode != 0
    assert "nope.md" in result.stderr
    assert "LOCAL" in (project / RULES).read_text(encoding="utf-8")


def test_a_path_outside_the_project_is_not_a_seed_file(project: Path) -> None:
    result = _harness("update", str(project), "--force-seed-file", "../escape.md")
    assert result.returncode != 0
    assert not (project.parent / "escape.md").exists()


def test_force_seed_files_still_overwrites_all(project: Path) -> None:
    result = _harness("update", str(project), "--force-seed-files")
    assert result.returncode == 0, result.stderr
    assert _differing(project) == []
