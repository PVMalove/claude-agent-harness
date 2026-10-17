"""Explicit operator handoff of an unfinished writer checkout; never a completion report."""

from __future__ import annotations

import hashlib
import re
import stat
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import NoReturn, cast

from harness.orchestration.core.git_utils import _run_git, git_environment, git_failure
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.ledger.ledger_ops import (
    _load_dispatch,
    _load_dispatch_status,
)
from harness.orchestration.ledger.recovery_chain import active_dispatches


def _refuse(message: str) -> NoReturn:
    raise CoordinatorError(
        message,
        remedy="inspect the stopped writer checkout and repeat human-approved batch rewind "
        "with its exact --preserved-commit and every --preserved-file; never reset or stash",
    )


def _git_bytes(checkout: Path, *arguments: str) -> bytes:
    result = _run_git(checkout, arguments, env=git_environment())
    if result.returncode:
        raise git_failure(
            "cannot inspect preserved writer progress",
            result.stderr.decode("utf-8", "replace"),
            remedy="inspect the Git failure and repeat recovery without changing history",
        )
    return result.stdout


def _names(checkout: Path, *arguments: str) -> list[str]:
    try:
        return [
            p for p in _git_bytes(checkout, *arguments).decode("utf-8").split("\0") if p
        ]
    except UnicodeDecodeError:
        _refuse("preserved paths must be UTF-8")


def _valid_path(path: object) -> bool:
    return (
        isinstance(path, str)
        and bool(path)
        and "\\" not in path
        and not PurePosixPath(path).is_absolute()
        and PurePosixPath(path).as_posix() == path
        and all(part not in {"", ".", ".."} for part in path.split("/"))
    )


def _inside_scope(paths: list[str], scope: list[str]) -> bool:
    return all(
        _valid_path(path) and any(fnmatchcase(path, pattern) for pattern in scope)
        for path in paths
    )


def _checkout_state(checkout: Path, scope: list[str]) -> JsonObject:
    names = sorted(
        set(
            _names(
                checkout,
                "diff",
                "--cached",
                "--name-only",
                "--no-renames",
                "-z",
                "HEAD",
                "--",
            )
            + _names(checkout, "diff", "--name-only", "--no-renames", "-z", "--")
            + _names(checkout, "ls-files", "--others", "--exclude-standard", "-z")
        )
    )
    if not _inside_scope(names, scope):
        _refuse("preserved dirty files exceed the stopped writer scope")
    files: JsonObject = {}
    for name in names:
        path = checkout / name
        # Reject symlinks in parents too; recovery must not read outside this checkout.
        if any(
            (checkout / Path(*Path(name).parts[:n])).is_symlink()
            for n in range(1, len(Path(name).parts) + 1)
        ):
            _refuse("preserved dirty files contain a symlink")
        if not path.exists():
            files[name] = None
        elif path.is_file():
            files[name] = {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "mode": stat.S_IMODE(path.stat().st_mode),
            }
        else:
            _refuse("preserved dirty files must be regular files or deletions")
    return {
        "files": files,
        "index_sha256": hashlib.sha256(
            _git_bytes(
                checkout,
                "diff",
                "--cached",
                "--binary",
                "--no-ext-diff",
                "--no-textconv",
                "HEAD",
                "--",
            )
        ).hexdigest(),
        "worktree_sha256": hashlib.sha256(
            _git_bytes(
                checkout, "diff", "--binary", "--no-ext-diff", "--no-textconv", "--"
            )
        ).hexdigest(),
    }


def capture(
    repo: Path,
    root: Path,
    batch: JsonObject,
    target: str,
    commit: str,
    files: list[str],
) -> JsonObject:
    """Attest an explicitly named, stopped writer's HEAD, scope and exact pending file state."""
    from harness.orchestration.workflow.recovery import verified_checkout

    entries = active_dispatches(batch)
    entry = entries[-1] if entries else {}
    role = {"developer": "developer", "resolve-conflict": "conflict-resolver"}.get(
        target
    )
    if (
        role is None
        or entry.get("role") != role
        or entry.get("state") != "cancelled"
        or entry.get("report")
        or not entry.get("cancellation", {}).get("runtime_stopped")
    ):
        _refuse(
            "preserved progress requires the latest writer cancelled with --runtime-stopped"
        )
    brief = _load_dispatch(root, entry["dispatch_id"])
    status = _load_dispatch_status(root, entry["dispatch_id"])
    if (
        brief.get("purpose") != "work"
        or status.get("state") != "cancelled"
        or status.get("cancellation") != entry["cancellation"]
    ):
        _refuse("preserved progress has no matching stopped writer evidence")
    if (
        not isinstance(commit, str)
        or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) is None
    ):
        _refuse("--preserved-commit requires the full writer HEAD SHA")
    if (
        not isinstance(files, list)
        or any(not _valid_path(p) for p in files)
        or len(set(files)) != len(files)
    ):
        _refuse("--preserved-file requires unique canonical repository-relative paths")
    checkout = Path(batch["worktree"]).resolve()
    proof = verified_checkout(
        repo,
        {
            **brief,
            "snapshot_commit": commit,
            "worker_worktree": str(checkout),
        },
        str(checkout),
    )
    snapshot = brief["snapshot_commit"]
    ancestor = _run_git(
        checkout,
        ["merge-base", "--is-ancestor", snapshot, commit],
        env=git_environment(),
    )
    if ancestor.returncode == 1:
        _refuse("preserved HEAD is not a descendant of the stopped writer snapshot")
    if ancestor.returncode:
        raise git_failure(
            "cannot inspect preserved writer ancestry",
            ancestor.stderr.decode("utf-8", "replace"),
            remedy="inspect Git history before repeating recovery",
        )
    committed = _names(
        checkout, "diff", "--name-only", "--no-renames", "-z", snapshot, commit, "--"
    )
    if not _inside_scope(committed, brief["write_paths"]):
        _refuse("preserved commits exceed the stopped writer scope")
    state = _checkout_state(checkout, brief["write_paths"])
    if sorted(files) != sorted(state["files"]):
        _refuse("--preserved-file does not match all pending writer files")
    return {
        "source_dispatch_id": brief["dispatch_id"],
        "snapshot_commit": snapshot,
        "head_commit": commit,
        "checkout": proof,
        **state,
    }


def pending(batch: JsonObject) -> JsonObject | None:
    """Only the first writer after this rewind inherits the unfinished checkout."""
    events = batch.get("recovery_events", [])
    if batch.get("rewind_writer_pending") and events and events[-1]["kind"] == "rewind":
        return cast(JsonObject | None, events[-1]["evidence"].get("preserved_progress"))
    return None


def require_current(
    repo: Path, root: Path, batch: JsonObject, progress: JsonObject
) -> None:
    from harness.orchestration.workflow.recovery import verified_checkout

    brief = _load_dispatch(root, progress["source_dispatch_id"])
    checkout = Path(batch["worktree"]).resolve()
    proof = verified_checkout(
        repo,
        {
            **brief,
            "snapshot_commit": progress["head_commit"],
            "worker_worktree": str(checkout),
        },
        str(checkout),
    )
    if proof != progress["checkout"] or _checkout_state(
        checkout, brief["write_paths"]
    ) != {k: progress[k] for k in ("files", "index_sha256", "worktree_sha256")}:
        _refuse("preserved writer progress changed after human approval")


def validate_event(root: Path, batch: JsonObject, event: JsonObject) -> None:
    """Validate historical binding without comparing it to a checkout that can advance."""
    progress = event["evidence"].get("preserved_progress")
    if progress is None:
        return
    if not isinstance(progress, dict):
        _refuse("invalid preserved writer evidence")
    source = progress.get("source_dispatch_id")
    entry: JsonObject = next(
        (e for e in batch["dispatches"] if e["dispatch_id"] == source), {}
    )
    brief = _load_dispatch(root, source) if entry and isinstance(source, str) else {}
    files = progress.get("files")
    if (
        set(progress)
        != {
            "source_dispatch_id",
            "snapshot_commit",
            "head_commit",
            "checkout",
            "files",
            "index_sha256",
            "worktree_sha256",
        }
        or event["kind"] != "rewind"
        or event["next_action"] not in {"developer", "resolve-conflict"}
        or source not in event["evidence"].get("superseded_dispatches", [])
        or brief.get("batch_id") != batch["batch_id"]
        or brief.get("role")
        != {"developer": "developer", "resolve-conflict": "conflict-resolver"}.get(
            event["next_action"]
        )
        or brief.get("purpose") != "work"
        or entry.get("report")
        or entry.get("state") != "cancelled"
        or not entry.get("cancellation", {}).get("runtime_stopped")
        or progress.get("snapshot_commit") != brief.get("snapshot_commit")
        or progress.get("head_commit") != event["evidence"].get("writer_start_commit")
        or not isinstance(progress.get("head_commit"), str)
        or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", progress["head_commit"]) is None
        or not isinstance(files, dict)
        or not _inside_scope(list(files), brief.get("write_paths", []))
        or any(
            not isinstance(progress.get(k), str)
            or re.fullmatch(r"[0-9a-f]{64}", progress[k]) is None
            for k in ("index_sha256", "worktree_sha256")
        )
    ):
        _refuse("preserved writer evidence diverged from its stopped dispatch")


def validate_dispatch(batch: JsonObject, dispatch: JsonObject) -> None:
    transition = dispatch.get("transition") or {}
    progress = dispatch.get("preserved_progress")
    if progress is None:
        return
    event: JsonObject = next(
        (
            e
            for e in batch.get("recovery_events", [])
            if e["record_sha256"] == transition.get("recovery_event_sha256")
        ),
        {},
    )
    if (
        progress != event.get("evidence", {}).get("preserved_progress")
        or dispatch.get("snapshot_commit") != progress.get("head_commit")
        or dispatch.get("purpose") != "work"
        or dispatch.get("role") not in {"developer", "conflict-resolver"}
    ):
        _refuse("dispatch diverged from the approved preserved writer handoff")
