"""Shared storage resolution stays bounded and fail-safe when git cannot answer."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness import storage


def _hanging_git(monkeypatch: pytest.MonkeyPatch, timeouts: list[float | None]) -> None:
    """Every git call times out; record the timeout it was given."""

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        timeout = kwargs.get("timeout")
        assert timeout is None or isinstance(timeout, (int, float))
        timeouts.append(timeout)
        raise subprocess.TimeoutExpired(argv, timeout or 0)

    monkeypatch.setattr(subprocess, "run", run)


def test_a_git_that_does_not_answer_falls_back_to_the_local_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timeouts: list[float | None] = []
    _hanging_git(monkeypatch, timeouts)
    assert storage.storage_root(tmp_path) == tmp_path.resolve() / ".harness"
    assert timeouts == [storage.GIT_TIMEOUT_SECONDS]


def test_a_linked_worktree_without_an_answering_git_has_no_storage_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")
    _hanging_git(monkeypatch, [])
    with pytest.raises(ValueError, match="cannot locate main checkout"):
        storage.storage_root(tmp_path, require_main_checkout=True)
