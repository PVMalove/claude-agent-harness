"""Validate the runtime's actual checkout before it may submit a role report.

The coordinator owns the small interface: ``attest(repo, dispatch, worktree)``.  It hides Git
worktree discovery and role-specific pin checks so adapters and report gates share one proof.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


class AttestationError(Exception):
    """The runtime did not start in the immutable dispatch's Git context."""


def _git(path: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(path), *arguments], capture_output=True, text=True, encoding="utf-8")
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise AttestationError(detail or f"git {' '.join(arguments)} failed")
    return result.stdout.strip()


def _registered_worktrees(repo: Path) -> set[Path]:
    worktrees: set[Path] = set()
    for line in _git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            worktrees.add(Path(line.removeprefix("worktree ")).resolve())
    return worktrees


def attest(repo: Path, dispatch: dict[str, Any], worktree: str) -> dict[str, str]:
    """Return immutable startup evidence or reject a wrong CWD before role work is accepted."""
    checkout = Path(worktree).resolve()
    if not checkout.is_dir():
        raise AttestationError("reported worktree does not exist")
    if checkout not in _registered_worktrees(repo):
        raise AttestationError("reported worktree is not registered by the dispatch repository")
    try:
        top_level = Path(_git(checkout, "rev-parse", "--show-toplevel")).resolve()
    except AttestationError as exc:
        raise AttestationError("reported worktree is not a Git worktree") from exc
    if top_level != checkout:
        raise AttestationError("reported worktree must be its Git top-level, not a subdirectory")
    head = _git(checkout, "rev-parse", "--verify", "HEAD^{commit}")
    snapshot = dispatch.get("snapshot_commit")
    if isinstance(snapshot, str) and snapshot and head != snapshot:
        raise AttestationError("runtime worktree HEAD does not match the immutable startup snapshot")
    role = dispatch.get("role")
    branch = _git(checkout, "branch", "--show-current")
    if role in {"architect", "developer"}:
        expected_branch = dispatch.get("branch")
        if branch != expected_branch:
            raise AttestationError("runtime worktree branch does not match the immutable issue branch")
        ancestor = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", head, expected_branch],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if ancestor.returncode:
            raise AttestationError("runtime worktree HEAD is not reachable from the immutable issue branch")
    elif role == "code-review":
        candidate = dispatch.get("candidate_commit")
        if head != candidate:
            raise AttestationError("review runtime worktree HEAD does not match the pinned candidate commit")
    return {"worktree": str(checkout), "branch": branch, "head_commit": head}
