"""PR refresh hardening: every Git command of the route is bounded, and a bound that fires is an
error, never an exit code read as a clean rebase."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness.orchestration.core.utils import CoordinatorError
from harness.orchestration.workflow import pr_refresh


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(f"{name}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path: Path) -> tuple[Path, str]:
    """A repository on branch ``feature`` whose target ``main`` moved on: the returned tip."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _commit(repo, "base.txt")
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "feature.txt")
    _git(repo, "checkout", "-q", "main")
    tip = _commit(repo, "moved.txt")
    _git(repo, "checkout", "-q", "feature")
    return repo, tip


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (subprocess.TimeoutExpired(["git"], 1), "did not finish within"),
        (FileNotFoundError(2, "No such file or directory"), "cannot run git"),
    ],
)
def test_a_hung_or_missing_git_is_a_coordinator_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: Exception, message: str
) -> None:
    def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["timeout"] == pr_refresh.GIT_TIMEOUT_SECONDS
        raise failure

    monkeypatch.setattr(subprocess, "run", fail)

    with pytest.raises(CoordinatorError) as refused:
        pr_refresh._run_git(tmp_path, "fetch", "origin")

    assert message in refused.value.message
    assert refused.value.remedy


def test_a_trial_rebase_that_times_out_is_never_a_clean_rebase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real hang: the pre-rebase hook outlives the bound, and the trial raises."""
    repo, tip = _repo(tmp_path)
    hook = repo / ".git" / "hooks" / "pre-rebase"
    hook.write_text(
        "#!/bin/sh\nexec sleep 3 </dev/null >/dev/null 2>&1\n", encoding="utf-8"
    )
    hook.chmod(0o755)
    monkeypatch.setattr(pr_refresh, "GIT_TIMEOUT_SECONDS", 1)

    with pytest.raises(CoordinatorError, match="did not finish within 1 seconds"):
        pr_refresh.conflicting_files(repo, "feature", tip)

    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "feature"


def test_a_restore_that_times_out_keeps_the_access_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def hang(worktree: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        raise CoordinatorError("git did not finish", remedy="retry")

    monkeypatch.setattr(pr_refresh, "_run_git", hang)
    refused = subprocess.CompletedProcess(
        ["git", "rebase"],
        128,
        "",
        "fatal: cannot write .git/index.lock: Permission denied\n",
    )

    error = pr_refresh._rebase_access_failure(tmp_path, "feature", refused)

    assert error is not None
    assert "refused by the environment" in error.message
