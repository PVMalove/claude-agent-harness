"""Validate the runtime's actual checkout before it may submit a role report.

The coordinator owns the small interface: ``attest(repo, dispatch, worktree)``.  It hides Git
worktree discovery and role-specific pin checks so adapters and report gates share one proof.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

from ..errors import HarnessError


class AttestationError(HarnessError):
    """The runtime did not start in the immutable dispatch's Git context."""


def _git_command(path: Path, *arguments: str) -> list[str]:
    # The coordinator may run under a different Windows sandbox account. Trust only the
    # already selected repository or registered worktree for this one Git invocation.
    trusted = path.resolve()
    return [
        "git",
        "-c",
        f"safe.directory={trusted}",
        "-C",
        str(trusted),
        *arguments,
    ]


def _git(path: Path, *arguments: str) -> str:
    result = subprocess.run(
        _git_command(path, *arguments),
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise AttestationError(
            detail or f"git {' '.join(arguments)} failed",
            remedy=f"inspect the git error above and fix the worktree/repository state before retrying 'git {' '.join(arguments)}'",
        )
    return result.stdout.strip()


def _registered_worktrees(repo: Path) -> set[Path]:
    worktrees: set[Path] = set()
    for line in _git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            worktrees.add(Path(line.removeprefix("worktree ")).resolve())
    return worktrees


def attest(repo: Path, dispatch: Mapping[str, object], worktree: str) -> dict[str, str]:
    """Return immutable startup evidence or reject a wrong CWD before role work is accepted."""
    checkout = Path(worktree).resolve()
    if not checkout.is_dir():
        raise AttestationError(
            "reported worktree does not exist",
            remedy=f"report the real worktree path instead of {checkout}",
        )
    if checkout not in _registered_worktrees(repo):
        raise AttestationError(
            "reported worktree is not registered by the dispatch repository",
            remedy=f"run 'git worktree add' for {checkout} or report the correct registered worktree",
        )
    try:
        top_level = Path(_git(checkout, "rev-parse", "--show-toplevel")).resolve()
    except AttestationError as exc:
        raise AttestationError(
            "reported worktree is not a Git worktree",
            remedy=f"report a directory that is a real Git worktree, not {checkout}",
        ) from exc
    if top_level != checkout:
        raise AttestationError(
            "reported worktree must be its Git top-level, not a subdirectory",
            remedy=f"report the Git top-level {top_level} instead of the subdirectory {checkout}",
        )
    head = _git(checkout, "rev-parse", "--verify", "HEAD^{commit}")
    snapshot = dispatch.get("snapshot_commit")
    if isinstance(snapshot, str) and snapshot and head != snapshot:
        raise AttestationError(
            f"runtime worktree HEAD ({head}) does not match the immutable startup snapshot ({snapshot}); "
            "do not rewrite the existing commit history to satisfy this check",
            remedy="create a new dispatch with the current candidate pinned as snapshot_commit, or use a worktree already at the immutable snapshot",
        )
    role = dispatch.get("role")
    branch = _git(checkout, "branch", "--show-current")
    if role in {"architect", "developer"}:
        expected_branch = dispatch.get("branch")
        if not isinstance(expected_branch, str):
            raise AttestationError(
                "runtime worktree branch does not match the immutable issue branch",
                remedy=f"pin a string issue branch in the dispatch (got {expected_branch!r}) before dispatching this role",
            )
        if branch != expected_branch:
            raise AttestationError(
                "runtime worktree branch does not match the immutable issue branch",
                remedy=f"checkout branch {expected_branch!r} in {checkout} before dispatching this role",
            )
        ancestor = subprocess.run(
            _git_command(repo, "merge-base", "--is-ancestor", head, expected_branch),
            capture_output=True,
            check=False,
        )
        if ancestor.returncode:
            raise AttestationError(
                "runtime worktree HEAD is not reachable from the immutable issue branch",
                remedy=f"rebase or merge {expected_branch!r} so commit {head} is reachable from it, or dispatch from a worktree that is",
            )
    elif role in {"verification", "code-review"}:
        candidate = dispatch.get("candidate_commit")
        if head != candidate:
            raise AttestationError(
                f"{role} runtime worktree HEAD does not match the pinned candidate commit",
                remedy=f"checkout commit {candidate} in {checkout} before dispatching the {role} role",
            )
    return {"worktree": str(checkout), "branch": branch, "head_commit": head}
