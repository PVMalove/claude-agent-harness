"""Hardening tests for the coordinator's git access (`core/git_utils.py`)."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from harness.orchestration.core import git_utils
from harness.orchestration.core.utils import CoordinatorError


def _run(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _run(path, "init", "-q")
    _run(path, "config", "user.name", "Test")
    _run(path, "config", "user.email", "test@example.invalid")
    _run(path, "commit", "-q", "--allow-empty", "-m", "base")
    return path


def test_commit_evidence_of_a_non_utf8_file_does_not_crash(repo: Path) -> None:
    base = _run(repo, "rev-parse", "HEAD")
    # A Latin-1 text file has no NUL byte, so git prints its raw bytes in the diff.
    (repo / "legacy.txt").write_bytes(b"caf\xe9\n")
    _run(repo, "add", "legacy.txt")
    _run(repo, "commit", "-q", "-m", "latin-1 file")

    evidence = git_utils._commit_evidence(repo, base, "HEAD")
    whole_commit = git_utils._commit_evidence(repo, None, "HEAD")

    assert "legacy.txt" in evidence
    assert "caf�" in evidence
    assert "caf�" in whole_commit


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda repo: git_utils._git(repo, "status"), id="git"),
        pytest.param(lambda repo: git_utils._head_commit(repo), id="head-commit"),
        pytest.param(
            lambda repo: git_utils._git_is_ancestor(repo, "HEAD", "HEAD"),
            id="is-ancestor",
        ),
        pytest.param(lambda repo: git_utils._patch_id(repo, "HEAD"), id="patch-id"),
    ],
)
def test_a_hung_git_command_fails_as_a_coordinator_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch, call: Callable[[Path], object]
) -> None:
    seen: list[object] = []

    def hang(*args: object, **kwargs: object) -> object:
        seen.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(CoordinatorError) as caught:
        call(repo)

    assert seen == [git_utils.GIT_TIMEOUT_SECONDS]
    assert str(git_utils.GIT_TIMEOUT_SECONDS) in caught.value.message
    assert "retry" in caught.value.remedy


def test_a_missing_git_executable_fails_as_a_coordinator_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError(2, "No such file or directory", "git")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(CoordinatorError) as caught:
        git_utils._head_commit(repo)

    assert "cannot run git" in caught.value.message
    assert "PATH" in caught.value.remedy


def test_head_commit_of_an_unborn_branch_is_none(tmp_path: Path) -> None:
    _run(tmp_path, "init", "-q")

    assert git_utils._head_commit(tmp_path) is None


def test_patch_id_of_a_real_commit_is_stable(repo: Path) -> None:
    (repo / "file.txt").write_text("one\n", encoding="utf-8")
    _run(repo, "add", "file.txt")
    _run(repo, "commit", "-q", "-m", "change")

    first = git_utils._patch_id(repo, "HEAD")

    assert first is not None
    assert first == git_utils._patch_id(repo, "HEAD")
    assert git_utils._git_is_ancestor(repo, "HEAD~1", "HEAD") is True
    assert git_utils._git_is_ancestor(repo, "HEAD", "HEAD~1") is False
