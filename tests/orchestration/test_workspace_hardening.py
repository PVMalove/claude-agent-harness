"""Hardening tests for the repository facts of a batch (`core/workspace.py`)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness.orchestration.core import workspace
from harness.orchestration.core.utils import CoordinatorError


def _run(repo: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *arguments], capture_output=True, check=True
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _run(path, "init", "-q")
    _run(path, "config", "user.name", "Test")
    _run(path, "config", "user.email", "test@example.invalid")
    _run(path, "commit", "-q", "--allow-empty", "-m", "base")
    return path


def test_a_symlink_loop_is_not_a_valid_worktree(repo: Path, tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    first.symlink_to(second)
    second.symlink_to(first)

    with pytest.raises(CoordinatorError) as caught:
        workspace._validate_worktree(repo, str(first))

    assert caught.value.message == "worktree is not a valid path"
    assert caught.value.remedy == "pass a worktree that is a valid filesystem path"


def test_a_nul_byte_is_not_a_valid_worktree(repo: Path) -> None:
    with pytest.raises(CoordinatorError) as caught:
        workspace._validate_worktree(repo, "issue\x00worktree")

    assert caught.value.message == "worktree is not a valid path"


def test_a_missing_worktree_keeps_its_registration_diagnostic(repo: Path) -> None:
    missing = repo.parent / "missing"

    with pytest.raises(CoordinatorError) as caught:
        workspace._validate_worktree(repo, str(missing))

    assert (
        caught.value.message
        == f"worktree {str(missing)!r} is not registered by git worktree"
    )


def test_registered_worktrees_are_valid_and_project_roots(
    repo: Path, tmp_path: Path
) -> None:
    linked = tmp_path / "issue-1"
    _run(repo, "worktree", "add", "-q", str(linked))
    unregistered = tmp_path / "elsewhere"
    unregistered.mkdir()

    workspace._validate_worktree(repo, str(linked))
    with pytest.raises(CoordinatorError, match="is not registered by git worktree"):
        workspace._validate_worktree(repo, str(unregistered))
    assert workspace._worktree_roots(repo) == {repo.resolve(), linked.resolve()}


def test_the_roots_of_a_non_repository_are_the_directory_itself(
    tmp_path: Path,
) -> None:
    assert workspace._worktree_roots(tmp_path) == {tmp_path.resolve()}
