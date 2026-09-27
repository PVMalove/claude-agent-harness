"""Run each batch on the harness runtime it was planned under.

A batch pins the hash of the runtime installed when it was created.  The checkout's runtime is one
mutable install shared by every batch, so reinstalling it for one task would otherwise strand every
other active batch.  `batch create` keeps an immutable snapshot of the pinned runtime; a command
for a batch whose pin differs from the installed runtime is handed to that snapshot instead.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _non_empty,
    _repo,
)
from harness.orchestration.core.workspace import (
    MODULE_ROOT,
    RUNTIMES_DIR,
    _harness_runtime_sha256,
    _pinned_runtime_entry,
    _store_runtime_snapshot,
)
from harness.orchestration.ledger.ledger_ops import (
    _load_batch,
    _load_dispatch,
    _state_root,
)


def _pinned_hash(batch: JsonObject) -> str | None:
    value = batch.get("harness_runtime_sha256")
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
        return value
    return None


def _target_batch(args: argparse.Namespace, root: Path) -> JsonObject | None:
    """The batch a command acts on, or None when it names none or its records are unreadable --
    the command itself then reports that, on the installed runtime."""
    try:
        batch_id = getattr(args, "batch", None)
        if not _non_empty(batch_id):
            dispatch_id = getattr(args, "dispatch", None)
            if not _non_empty(dispatch_id):
                return None
            batch_id = _load_dispatch(root, dispatch_id).get("batch_id")
            if not _non_empty(batch_id):
                return None
        return _load_batch(root, batch_id)
    except (CoordinatorError, OSError):
        return None


# Runs a snapshot's coordinator with the runtime hash taken from the snapshot itself rather than
# from the checkout's installed runtime.  It patches only `_runtime_snapshot_root`, which every
# runtime that pins a hash has, so it also runs snapshots restored from before this module existed.
_PINNED_BOOTSTRAP = """\
import importlib.util, sys
from pathlib import Path
entry = Path(sys.argv[1])
sys.argv = [str(entry), *sys.argv[2:]]
spec = importlib.util.spec_from_file_location("pinned_coordinator", entry)
coordinator = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = coordinator
spec.loader.exec_module(coordinator)
workspace = sys.modules["harness.orchestration.core.workspace"]
workspace._runtime_snapshot_root = lambda repo: entry.parent
raise SystemExit(coordinator.main())
"""


def _is_pinned_snapshot() -> bool:
    """A snapshot (`<state>/runtimes/<key>/harness/orchestration`) never hands a command on, so a
    snapshot whose hash check fails reports it instead of re-running itself without end."""
    parents = MODULE_ROOT.parents
    return len(parents) > 2 and parents[2].name == RUNTIMES_DIR


def pinned_runtime_command(args: argparse.Namespace) -> list[str] | None:
    """The command line that re-runs this invocation on its batch's pinned runtime snapshot, or
    None when the running runtime is the right one to run it."""
    if getattr(args, "installed_runtime_only", False) or _is_pinned_snapshot():
        return None
    repo = _repo(args)
    root = _state_root(args, repo)
    batch = _target_batch(args, root)
    expected = _pinned_hash(batch) if batch is not None else None
    if expected is None or expected == _harness_runtime_sha256(repo):
        return None
    entry = _pinned_runtime_entry(root, expected)
    if entry is None:
        return None
    return [sys.executable, "-c", _PINNED_BOOTSTRAP, str(entry), *sys.argv[1:]]


def restore_batch_runtime(args: argparse.Namespace) -> JsonObject:
    """Store the snapshot a batch planned before snapshots existed needs to finish on its pin."""
    repo = _repo(args)
    root = _state_root(args, repo)
    batch = _load_batch(root, args.batch)
    expected = _pinned_hash(batch)
    if expected is None:
        raise CoordinatorError(
            "this batch pins no harness runtime, so it already runs on the installed one",
            remedy="continue the batch with the installed runtime",
        )
    source = Path(args.source).expanduser().resolve()
    if not (source / "orchestration").is_dir():
        raise CoordinatorError(
            f"{source} is not a harness runtime directory: it has no orchestration/",
            remedy="pass the .harness directory installed from the revision the batch was planned on",
        )
    snapshot = _store_runtime_snapshot(root, source, expected)
    return {
        "batch_id": batch["batch_id"],
        "harness_runtime_sha256": expected,
        "runtime_snapshot": str(snapshot),
    }
