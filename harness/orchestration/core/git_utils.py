"""Git access of the orchestration coordinator.

Every git invocation the lifecycle depends on lives here, so the layers above reason about commits
and changed files rather than about subprocess plumbing.  Failures surface as `CoordinatorError`
with the git output as the remedy hint.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from harness.orchestration.core.utils import CoordinatorError


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise CoordinatorError(
            f"git command failed: {detail or 'unknown error'}",
            remedy=f"inspect the git error above and fix the repository state before retrying 'git {' '.join(arguments)}'",
        )
    return result.stdout.strip()


def _head_commit(repo: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _fetch_ref_tip(repo: Path, ref: str) -> str:
    """The current commit an integration ref points to on origin, fetched fresh — never a locally
    cached remote-tracking branch, which is exactly the staleness this gate exists to catch."""
    try:
        _git(repo, "fetch", "origin", ref)
    except CoordinatorError as exc:
        raise CoordinatorError(
            f"could not fetch origin {ref!r}: {exc}",
            remedy=f"inspect the git fetch error above for {ref!r} and retry",
        ) from exc
    return _git(repo, "rev-parse", "--verify", "FETCH_HEAD")


def _commit_changed_files(repo: Path, commit: str) -> list[str]:
    output = _git(
        repo, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", commit
    )
    return [line.replace("\\", "/") for line in output.splitlines() if line.strip()]


def _commit_parent(repo: Path, commit: str) -> str | None:
    output = _git(repo, "rev-list", "--parents", "-n", "1", commit).split()
    return output[1] if len(output) > 1 else None


def _git_is_ancestor(repo: Path, base: str, candidate: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", base, candidate],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise CoordinatorError(
            "cannot verify the candidate diff ancestry",
            remedy="verify the candidate and base commits both exist and are reachable in this repository",
        )
    return result.returncode == 0


def _changed_files_between(repo: Path, base: str, candidate: str) -> list[str]:
    output = _git(repo, "diff", "--name-only", "--no-renames", base, candidate)
    return [line.replace("\\", "/") for line in output.splitlines() if line.strip()]


def _commit_evidence(repo: Path, base: str | None, commit: str) -> str:
    if base:
        return "\n".join(
            (
                _git(repo, "log", "--format=%B", f"{base}..{commit}"),
                _git(repo, "diff", "--no-ext-diff", "--no-renames", base, commit),
            )
        )
    return _git(repo, "show", "--format=%B", "--no-ext-diff", "--no-renames", commit)


def _candidate_commit(repo: Path, value: object) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-9a-fA-F]{7,64}", value.strip()) is None
    ):
        raise CoordinatorError(
            "candidate_commit must be a hexadecimal commit SHA",
            remedy="pass candidate_commit as a 7-64 character hex commit SHA",
        )
    try:
        return _git(repo, "rev-parse", "--verify", f"{value.strip()}^{{commit}}")
    except CoordinatorError as exc:
        raise CoordinatorError(
            "candidate_commit does not resolve to a commit",
            remedy="pass a candidate_commit that resolves to a real commit in this repository",
        ) from exc
