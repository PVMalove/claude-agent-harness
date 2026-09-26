"""Storage root selection at repository and nested-directory boundaries."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness.storage import (
    SANDBOX_CATEGORIES,
    SANDBOXES_DIR,
    sandboxes_root,
    storage_path,
    storage_root,
)
from harness.orchestration.core.constants import (
    AGENT_INBOX_REL,
    SANDBOXES_REL,
    SCRATCH_REL,
)


def test_nested_directory_does_not_inherit_parent_repository_cache(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    nested = tmp_path / "nested"
    nested.mkdir()

    assert storage_root(tmp_path) == tmp_path / ".harness"
    assert storage_root(nested) == nested / ".harness"


def test_sandboxes_root_and_categories(tmp_path: Path) -> None:
    assert SANDBOXES_DIR == ".sandboxes"
    expected_categories = {"cache", "logs", "scratch", "runs", "reports", "worktrees"}
    assert set(SANDBOX_CATEGORIES) == expected_categories
    assert sandboxes_root(tmp_path) == tmp_path / ".harness" / ".sandboxes"


def test_sandboxes_root_in_linked_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "main_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    readme = repo / "README.md"
    readme.write_text("main", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "initial commit"], check=True)

    worktree = tmp_path / "worktree"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(worktree)], check=True)

    assert storage_root(worktree) == repo / ".harness"
    assert sandboxes_root(worktree) == repo / ".harness" / ".sandboxes"
    assert storage_path(worktree, "runs", "test-1") == repo / ".harness" / ".sandboxes" / "runs" / "test-1"


def test_storage_path_categories_and_boundary(tmp_path: Path) -> None:
    for cat in SANDBOX_CATEGORIES:
        path = storage_path(tmp_path, cat, "sub1", "sub2")
        assert path == tmp_path / ".harness" / ".sandboxes" / cat / "sub1" / "sub2"
        assert path.is_relative_to(tmp_path / ".harness" / ".sandboxes")


def test_storage_path_legacy_categories(tmp_path: Path) -> None:
    cache_path = storage_path(tmp_path, ".cache", "repo_map", "results")
    assert cache_path == tmp_path / ".harness" / ".cache" / "repo_map" / "results"
    assert cache_path.is_relative_to(tmp_path / ".harness")

    tmp_path_res = storage_path(tmp_path, "tmp", "tests")
    assert tmp_path_res == tmp_path / ".harness" / "tmp" / "tests"
    assert tmp_path_res.is_relative_to(tmp_path / ".harness")


def test_storage_path_escape_and_invalid_components(tmp_path: Path) -> None:
    # Empty calls or invalid names
    with pytest.raises(ValueError, match="single nonempty names"):
        storage_path(tmp_path)
    with pytest.raises(ValueError, match="single nonempty names"):
        storage_path(tmp_path, "cache", "")
    with pytest.raises(ValueError, match="single nonempty names"):
        storage_path(tmp_path, "cache", "..")
    with pytest.raises(ValueError, match="single nonempty names"):
        storage_path(tmp_path, "cache", ".")
    with pytest.raises(ValueError, match="single nonempty names"):
        storage_path(tmp_path, "cache", "foo/bar")
    with pytest.raises(ValueError, match="single nonempty names"):
        storage_path(tmp_path, "cache", "foo\\bar")

    # Unknown category
    with pytest.raises(ValueError, match="unknown storage category"):
        storage_path(tmp_path, "invalid_cat", "sub")


def test_orchestration_constants_sandboxes_paths() -> None:
    assert SANDBOXES_REL == Path(".harness") / ".sandboxes"
    assert SCRATCH_REL == Path(".harness") / ".sandboxes" / "scratch"
    assert AGENT_INBOX_REL == Path(".harness") / ".sandboxes" / "scratch" / "inbox"

