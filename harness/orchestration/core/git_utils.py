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


METADATA_DENIED = "metadata-write-denied"
REMOTE_DENIED = "remote-access-denied"
REMOTE_UNREACHABLE = "remote-unreachable"

# Remote patterns are tried first: a refused push also prints "permission denied".
_REMOTE_DENIED = re.compile(
    r"permission denied \(publickey|authentication failed|could not read (?:username|password)"
    r"|invalid username (?:or|and) password|access denied|returned error: 40[13]"
    r"|permission to \S+ denied|insufficient permission|repository not found"
    r"|unable to create temporary object directory|unpacker error"
    r"|\[remote rejected\][^\n]*(?:denied|protected|permission|forbidden)"
    r"|remote: [^\n]*(?:denied|forbidden)",
    re.IGNORECASE,
)
_REMOTE_UNREACHABLE = re.compile(
    r"could not resolve host|connection (?:refused|timed out|reset)"
    r"|network is unreachable|unable to connect|does not appear to be a git repository"
    r"|could not read from remote repository|operation timed out"
    r"|name or service not known|unable to access",
    re.IGNORECASE,
)
_METADATA_DENIED = re.compile(
    r"permission denied|read-only file system|operation not permitted"
    r"|unable to (?:create|write|unlink|open)|could not (?:create|lock)|cannot lock ref",
    re.IGNORECASE,
)
_ACCESS_REMEDY = {
    METADATA_DENIED: (
        "grant the coordinator process write access to the repository's Git metadata (the shared "
        "Git directory and the worktree administrative files) in its sandbox or filesystem "
        "permissions, then repeat the command; the lifecycle state was not advanced"
    ),
    REMOTE_DENIED: (
        "grant the credentials this process uses permission on the Git remote (and the network "
        "access to its host), then repeat the command; the lifecycle state was not advanced"
    ),
    REMOTE_UNREACHABLE: (
        "restore connectivity to the Git remote from this environment, then repeat the command; "
        "the lifecycle state was not advanced"
    ),
}


def classify_git_failure(detail: str) -> str | None:
    """The access category of a Git failure's output, or ``None`` for any other failure.

    One classifier serves every coordinator Git operation, so a refused metadata write and a
    refused or unreachable remote read the same everywhere instead of as an opaque Git error.
    """
    if _REMOTE_DENIED.search(detail):
        return REMOTE_DENIED
    if _REMOTE_UNREACHABLE.search(detail):
        return REMOTE_UNREACHABLE
    if _METADATA_DENIED.search(detail):
        return METADATA_DENIED
    return None


class GitAccessError(CoordinatorError):
    """A Git command that the environment refused: metadata write, remote access or reachability."""

    def __init__(
        self, message: str, *, remedy: str, category: str, detail: str
    ) -> None:
        super().__init__(message, remedy=remedy)
        self.category = category
        self.detail = detail


def git_failure(message: str, detail: str, *, remedy: str) -> CoordinatorError:
    """The error for a failed Git command: access-classified when the output says so."""
    category = classify_git_failure(detail)
    if category is None:
        return CoordinatorError(message, remedy=remedy)
    return GitAccessError(
        message, remedy=_ACCESS_REMEDY[category], category=category, detail=detail
    )


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
        raise git_failure(
            f"git command failed: {detail or 'unknown error'}",
            detail,
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
    if not isinstance(ref, str) or ref.startswith("-") or not ref.strip():
        raise CoordinatorError(
            "ref must be a non-empty string not starting with '-'",
            remedy="pass a valid ref name (e.g. 'master' or 'main')",
        )
    try:
        _git(repo, "fetch", "origin", "--", ref)
    except CoordinatorError as exc:
        raise git_failure(
            f"could not fetch origin {ref!r}: {exc}",
            str(exc),
            remedy=f"inspect the git fetch error above for {ref!r} and retry",
        ) from exc
    return _git(repo, "rev-parse", "--verify", "FETCH_HEAD")


def _remote_branch_tip(
    repo: Path, remote: str, branch: str, *, timeout: int = 60
) -> str | None:
    """The commit a remote branch points to right now, or ``None`` when it has no such branch.

    Read-only: ``ls-remote`` never writes a local ref or ``FETCH_HEAD``.  The ref name is compared
    exactly, because ``ls-remote`` patterns also match deeper names that merely end with it.
    """
    for label, value in (("remote", remote), ("branch", branch)):
        if not isinstance(value, str) or not value.strip() or value.startswith("-"):
            raise CoordinatorError(
                f"{label} must be a non-empty string not starting with '-'",
                remedy=f"pass a valid {label} name",
            )
    name = f"refs/heads/{branch}"
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "ls-remote", "--heads", remote, name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise CoordinatorError(
            f"remote {remote!r} did not answer within {timeout} seconds",
            remedy=f"check connectivity to {remote!r} and retry",
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise git_failure(
            f"cannot read branch {branch!r} from remote {remote!r}: {detail or 'unknown error'}",
            detail,
            remedy=f"check that the remote {remote!r} is configured and reachable, then retry",
        )
    for line in result.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == name:
            return sha.strip()
    return None


def _commit_changed_files(repo: Path, commit: str) -> list[str]:
    output = _git(
        repo, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", commit, "--"
    )
    return [line.replace("\\", "/") for line in output.splitlines() if line.strip()]


def _commit_parent(repo: Path, commit: str) -> str | None:
    output = _git(repo, "rev-list", "--parents", "-n", "1", commit, "--").split()
    return output[1] if len(output) > 1 else None


def _git_is_ancestor(repo: Path, base: str, candidate: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", "--", base, candidate],
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
    output = _git(repo, "diff", "--name-only", "--no-renames", base, candidate, "--")
    return [line.replace("\\", "/") for line in output.splitlines() if line.strip()]


def _commits_between(repo: Path, base: str, candidate: str) -> list[str]:
    """Return the ordered first-parent candidate commits after an immutable snapshot."""
    return [
        line
        for line in _git(
            repo, "rev-list", "--reverse", f"{base}..{candidate}", "--"
        ).splitlines()
        if line
    ]


def _merge_base(repo: Path, first: str, second: str) -> str:
    """The best common ancestor of two commits."""
    return _git(repo, "merge-base", "--", first, second)


def _patch_id(repo: Path, commit: str) -> str | None:
    """The stable ``git patch-id`` of one commit's own diff, or ``None`` when it has no textual
    diff (an empty commit or any merge). Equal ids mean the same change, whatever its parent.

    The diff comes from plumbing ``git diff-tree``, which ignores the user's diff and log
    configuration and prints nothing for a merge."""
    diff = subprocess.run(
        ["git", "-C", str(repo), "diff-tree", "-p", "--root", "--no-commit-id"]
        + [commit, "--"],
        capture_output=True,
        check=False,
    )
    if diff.returncode != 0:
        raise CoordinatorError(
            f"git diff-tree failed for {commit}: "
            f"{diff.stderr.decode('utf-8', 'replace').strip()}",
            remedy=f"verify that commit {commit} exists in this repository",
        )
    if not diff.stdout.strip():
        return None
    result = subprocess.run(
        ["git", "-C", str(repo), "patch-id", "--stable"],
        input=diff.stdout,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise CoordinatorError(
            f"git patch-id failed for {commit}: "
            f"{result.stderr.decode('utf-8', 'replace').strip()}",
            remedy="inspect the git patch-id error above and retry",
        )
    fields = result.stdout.decode("utf-8", "replace").split()
    return fields[0] if fields else None


def _commit_evidence(repo: Path, base: str | None, commit: str) -> str:
    if base:
        return "\n".join(
            (
                _git(repo, "log", "--format=%B", f"{base}..{commit}", "--"),
                _git(repo, "diff", "--no-ext-diff", "--no-renames", base, commit, "--"),
            )
        )
    return _git(
        repo, "show", "--format=%B", "--no-ext-diff", "--no-renames", commit, "--"
    )


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
