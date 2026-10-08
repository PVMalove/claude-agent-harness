"""Cleanup and uninstall keep their fail-safe results when git is missing or does not answer."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness import cleanup
from harness.uninstall import CONFIRM_WORD, apply_uninstall, plan_uninstall


def _missing_git(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(subprocess, "run", run)


def test_git_that_does_not_answer_is_an_unsuccessful_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timeouts: list[object] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        timeouts.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(subprocess, "run", run)
    result = cleanup._git(tmp_path, "worktree", "list", "--porcelain")
    assert result.returncode != 0
    assert "git unavailable" in result.stderr
    assert timeouts == [cleanup.GIT_TIMEOUT_SECONDS]


def test_a_plan_without_runnable_git_keeps_every_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktrees = tmp_path / ".harness" / ".sandboxes" / "worktrees"
    (worktrees / "issue-1").mkdir(parents=True)
    _missing_git(monkeypatch)
    plan = cleanup.plan_cleanup(tmp_path, "hard", min_age_hours=0)
    assert plan["remove"] == []
    assert {
        "path": str(worktrees.resolve()),
        "reason": "Git or ledger state unavailable",
    } in plan["skipped"]


def test_uninstall_reports_its_result_when_git_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".harness").mkdir()
    plan = plan_uninstall(tmp_path)
    _missing_git(monkeypatch)
    result = apply_uninstall(tmp_path, plan, confirm=CONFIRM_WORD)
    assert result["removed"] == [".harness"]
    assert result["failed"] == []
    assert not (tmp_path / ".harness").exists()
