"""The repository a batch runs in.

Where an agent may write (the scratch inbox and the worktrees this repository actually has), which
branch and worktree a brief may name, and the hash that pins a batch to one harness runtime.  These
are facts about the checkout, not about the lifecycle, so every layer above may depend on them.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import uuid
from pathlib import Path

from harness.errors import INTERNAL_INVARIANT_REMEDY
from harness.orchestration.core.config import (
    _project,
)
from harness.orchestration.core.constants import (
    AGENT_INBOX_REL,
    NON_ENGLISH_BRIEF_PATTERN,
)
from harness.orchestration.core.git_utils import (
    _git,
)
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _non_empty,
)

# The orchestration package's own directory: `harness/orchestration/` in this repository, and
# `.harness/orchestration/` once packaged into a target project.
MODULE_ROOT = Path(__file__).resolve().parents[1]
# Under the coordinator state root: one immutable runtime snapshot per pinned hash.
RUNTIMES_DIR = "runtimes"
RUNTIME_VOLATILE_PARTS = frozenset({"state", "__pycache__"})


def _agent_inbox(repo: Path) -> Path:
    """The one canonical place a role writes the JSON it is about to hand to the coordinator.

    Without a declared absolute location a role invents one (``~/reports``, ``~/review-reports``,
    the system temp), so the evidence a human later looks for is scattered outside the project.
    """
    return (repo / AGENT_INBOX_REL).resolve()


def _reject_non_english(values: object, field: str) -> None:
    """Hold the language contract where it is machine-checkable: brief text handed to a role.

    Agent-to-agent protocol text is English. A completion report addressed to the coordinator is
    Russian by contract and is deliberately not checked here.
    """
    items = values if isinstance(values, (list, tuple)) else [values]
    for item in items:
        if isinstance(item, str) and NON_ENGLISH_BRIEF_PATTERN.search(item):
            raise CoordinatorError(
                f"{field} is handed to a role as agent-to-agent protocol text and must be written "
                "in English; translate it before creating the batch and keep commands, paths, IDs "
                "and quoted evidence verbatim",
                remedy=f"rewrite {field} in English, keeping commands, paths, IDs and quoted evidence verbatim",
            )


def _prepare_agent_inbox(repo: Path) -> Path:
    """Create the staging directory and keep its contents out of version control."""
    inbox = _agent_inbox(repo)
    inbox.mkdir(parents=True, exist_ok=True)
    ignore = inbox.parent / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n!.gitignore\n", encoding="utf-8")
    return inbox


def _worktree_roots(repo: Path) -> set[Path]:
    """Every checkout of this repository: the main one plus each linked worktree."""
    roots = {repo.resolve()}
    try:
        listing = _git(repo, "worktree", "list", "--porcelain")
    except CoordinatorError:
        return roots
    for line in listing.splitlines():
        if line.startswith("worktree "):
            roots.add(Path(line[len("worktree ") :].strip()).resolve())
    return roots


def _agent_authored_file(repo: Path, value: str, label: str) -> Path:
    """Resolve a role-authored payload and refuse anything written outside the project.

    A path under the home directory or the system temp is never the project's audit trail; it is a
    guessed location, and accepting it is what lets evidence drift out of the ledger.
    """
    path = Path(value).expanduser().resolve()
    for root in _worktree_roots(repo):
        try:
            path.relative_to(root)
        except ValueError:
            continue
        return path
    raise CoordinatorError(
        f"{label} must be written inside the repository or one of its worktrees, not at {path}; "
        f"use the canonical staging path {_agent_inbox(repo)}",
        remedy=f"write role-authored files under the repository or one of its worktrees, e.g. {_agent_inbox(repo)}",
    )


def _runtime_snapshot_root(repo: Path) -> Path:
    # A pinned snapshot's bootstrap (workflow/runtime_pin.py) replaces this function so the
    # snapshot hashes itself -- including snapshots of runtimes older than that bootstrap.
    installed = repo / ".harness/orchestration"
    return installed if installed.is_dir() else MODULE_ROOT


def _harness_runtime_sha256(repo: Path) -> str:
    """Hash the coordinator runtime without volatile state so a batch cannot span an update."""
    return _runtime_tree_sha256(_runtime_snapshot_root(repo))


def _runtime_tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        item
        for item in root.rglob("*")
        if item.is_file()
        and not RUNTIME_VOLATILE_PARTS.intersection(item.relative_to(root).parts)
        and item.suffix != ".pyc"
    ):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _validate_harness_runtime_snapshot(repo: Path, batch: JsonObject) -> None:
    expected = batch.get("harness_runtime_sha256")
    if (
        expected is None
    ):  # Explicitly supported legacy batch; never rewrite history in place.
        return
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise CoordinatorError(
            "batch has an invalid harness runtime snapshot hash",
            remedy="the batch's recorded harness runtime snapshot hash is malformed -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    actual = _harness_runtime_sha256(repo)
    if actual != expected:
        raise CoordinatorError(
            "the harness runtime changed after this batch was planned; do not spend a worker on recovery. "
            "Finish with the pinned harness revision or abandon and re-plan the batch.",
            remedy="restore the pinned runtime with 'batch restore-runtime --batch "
            f"{batch.get('batch_id')} --from <.harness directory of the pinned revision>', "
            "or abandon and re-plan the batch under the current one",
        )


def _pinned_runtime_dir(state_root: Path, sha256: str) -> Path:
    # A 16-hex key keeps the snapshot's deepest paths under Windows MAX_PATH; the full hash is
    # still verified before the snapshot runs.
    return state_root / RUNTIMES_DIR / sha256[:16]


def _pinned_runtime_entry(state_root: Path, sha256: str) -> Path | None:
    """The pinned snapshot's coordinator, after proving the snapshot still hashes to the pin."""
    snapshot = _pinned_runtime_dir(state_root, sha256) / "harness"
    entry = snapshot / "orchestration" / "coordinator.py"
    if not entry.is_file():
        return None
    if _runtime_tree_sha256(snapshot / "orchestration") != sha256:
        raise CoordinatorError(
            f"the pinned runtime snapshot {snapshot} no longer matches the batch's runtime hash",
            remedy=f"delete {snapshot.parent} and restore it with 'batch restore-runtime', "
            "or abandon and re-plan the batch",
        )
    return entry


def _store_runtime_snapshot(state_root: Path, source: Path, sha256: str) -> Path:
    """Keep an immutable copy of the harness package at `source` (the directory that holds
    `orchestration/`) so a batch can finish on its pinned runtime after the checkout's runtime is
    reinstalled.  Snapshots are content-addressed: batches on one runtime share one copy."""
    target = _pinned_runtime_dir(state_root, sha256)
    if target.is_dir():
        if _runtime_tree_sha256(target / "harness" / "orchestration") != sha256:
            raise CoordinatorError(
                f"the stored runtime snapshot {target} does not match hash {sha256}",
                remedy=f"delete {target} and retry",
            )
        return target
    if _runtime_tree_sha256(source / "orchestration") != sha256:
        raise CoordinatorError(
            f"the runtime at {source} does not match the pinned hash {sha256}",
            remedy="pass the .harness directory installed from the revision the batch was planned on",
        )
    staging = target.with_name(f"{target.name}.tmp-{uuid.uuid4().hex[:8]}")
    package = staging / "harness"
    package.mkdir(parents=True)
    ignore = shutil.ignore_patterns(*RUNTIME_VOLATILE_PARTS, "*.pyc")
    try:
        for item in source.iterdir():
            if item.is_file() and item.suffix == ".py":
                shutil.copy2(item, package / item.name)
            elif item.is_dir() and (
                item.name == "orchestration" or (item / "__init__.py").is_file()
            ):
                shutil.copytree(item, package / item.name, ignore=ignore)
        if _runtime_tree_sha256(package / "orchestration") != sha256:
            raise CoordinatorError(
                f"the runtime at {source} changed while it was being copied",
                remedy="retry once the runtime is no longer being reinstalled",
            )
        staging.rename(target)
    except OSError:
        if not target.is_dir():
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return target


def _required_base_branch(repo: Path) -> str:
    """Unlike `_validate_branch`'s own silent `"master"` default, the base-commit gate has nothing
    safe to fetch when the project config omits `base_branch` — fail loudly instead of pinning
    against a branch the project never named."""
    base = _project(repo).get("base_branch")
    if not _non_empty(base):
        raise CoordinatorError(
            "project config has no usable base_branch for the integration ref fallback",
            remedy="set project config base_branch, or pass --integration-ref explicitly",
        )
    return base


def _integration_ref(repo: Path, batch: JsonObject) -> str:
    ref = batch.get("integration_ref")
    if _non_empty(ref):
        return ref
    return _required_base_branch(repo)


def _validate_branch(repo: Path, branch: str) -> None:
    if not _non_empty(branch):
        raise CoordinatorError(
            "branch must be a non-empty string", remedy="pass a non-empty branch name"
        )
    project = _project(repo)
    pattern = project.get("branch_pattern", r"^feature/issue-[0-9]+-.+")
    base = project.get("base_branch", "master")
    if not isinstance(pattern, str):
        raise CoordinatorError(
            "project config has an invalid branch_pattern",
            remedy="fix the branch_pattern regular expression in the project config",
        )
    try:
        matches = re.fullmatch(pattern, branch) is not None
    except re.error as exc:
        raise CoordinatorError(
            "project config has an invalid branch_pattern",
            remedy="fix the branch_pattern regular expression in the project config",
        ) from exc
    if branch.startswith("integration/") or branch == base or not matches:
        raise CoordinatorError(
            "branch must be an issue branch, never a protected or integration branch",
            remedy="use an issue branch, never a protected or integration/* branch",
        )


def _validate_worktree(repo: Path, worktree: str) -> None:
    if not _non_empty(worktree):
        raise CoordinatorError(
            "worktree must be a non-empty string",
            remedy="pass a non-empty worktree path",
        )
    try:
        resolved = Path(worktree).resolve()
    except Exception as exc:
        raise CoordinatorError(
            "worktree is not a valid path",
            remedy="pass a worktree that is a valid filesystem path",
        ) from exc

    output = _git(repo, "worktree", "list", "--porcelain")
    paths: set[Path] = set()
    for line in output.splitlines():
        if line.startswith("worktree "):
            paths.add(Path(line.removeprefix("worktree ")).resolve())

    if resolved not in paths:
        raise CoordinatorError(
            f"worktree {worktree!r} is not registered by git worktree",
            remedy=f"run 'git worktree add' for {worktree}, or pass a worktree already registered by git worktree",
        )
