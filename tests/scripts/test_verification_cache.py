"""Worktree cache of a verification run: one directory per worktree and environment fingerprint,
never shared with another worktree or another fingerprint, and refused in a cold environment
(ADR 0023, section 4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.orchestration.verification_plan import environment_fingerprint
from harness.storage import storage_path
from scripts.verification import cache
from tests.scripts.verify_project import make_repo


def test_the_cache_lies_under_the_worktree_id_and_the_environment_fingerprint(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "repo")

    opened = cache.open_cache(repo, {})

    expected = storage_path(
        repo, "cache", cache.worktree_id(repo), cache.checkout_fingerprint(repo)
    )
    assert opened.directory == expected and expected.is_dir()
    assert opened.pytest_dir.parent == opened.mypy_dir.parent == expected
    assert opened.junit_path == expected / "junit.xml"
    assert ".sandboxes/cache/" in opened.directory.as_posix()


def test_another_worktree_has_its_own_cache_directory(tmp_path: Path) -> None:
    first = make_repo(tmp_path / "first")
    second = make_repo(tmp_path / "second")

    assert cache.worktree_id(first) != cache.worktree_id(second)
    assert (
        cache.open_cache(first, {}).directory != cache.open_cache(second, {}).directory
    )


def test_another_environment_fingerprint_is_another_directory_and_is_never_read(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "repo")
    before = cache.open_cache(repo, {})
    before.junit_path.write_text("<testsuites/>", encoding="utf-8")

    (repo / "uv.lock").write_text("lock-2\n", encoding="utf-8")
    after = cache.open_cache(repo, {})

    assert after.directory != before.directory
    assert not after.junit_path.exists()
    assert before.junit_path.is_file()


def test_the_checkout_fingerprint_equals_the_plan_fingerprint_of_the_same_commit(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "repo")
    head = cache.head_commit(repo)

    assert cache.checkout_fingerprint(repo) == environment_fingerprint(repo, head)


def test_a_cold_environment_refuses_the_worktree_cache(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")

    with pytest.raises(cache.VerificationCacheError, match=cache.COLD_ENV):
        cache.open_cache(repo, {cache.COLD_ENV: "1"})

    assert not storage_path(repo, "cache", cache.worktree_id(repo)).exists()
