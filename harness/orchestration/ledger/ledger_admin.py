"""The `ledger` CLI commands: inspect, migrate, reset and clean the lifecycle state, and release
a ledger lock its owner left behind.

These are the only commands that operate on the ledger as a whole rather than on a batch, and the
only ones allowed to discard state -- each behind its own explicit subcommand.
"""

from __future__ import annotations

import argparse
import socket
from collections.abc import Callable
from pathlib import Path
from typing import cast

from harness.cleanup import _pid_active
from harness.orchestration.core.constants import (
    LEDGER_LOCK_STALE_SECONDS,
    TERMINAL_BATCH_STATES,
)
from harness.orchestration.core.utils import CoordinatorError, JsonObject, _repo
from harness.orchestration.core.workspace import _runtime_matches
from harness.orchestration.ledger.ledger_ops import _ledger_lock, _state_root
from harness.orchestration.ledger.lifecycle import LedgerError, LifecycleLedger


def ledger_status(args: argparse.Namespace) -> JsonObject:
    """Report the selected lifecycle-ledger generation without changing it."""
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        try:
            return ledger.status()
        except LedgerError as exc:
            raise CoordinatorError(exc.message, remedy=exc.remedy) from exc


def migrate_ledger(args: argparse.Namespace) -> JsonObject:
    """Explicitly validate legacy state and atomically select its versioned replacement."""
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        try:
            _refuse_upgrade_under_pinned_batches(repo, ledger)
            return ledger.migrate()
        except LedgerError as exc:
            raise CoordinatorError(exc.message, remedy=exc.remedy) from exc


def _refuse_upgrade_under_pinned_batches(repo: Path, ledger: LifecycleLedger) -> None:
    """An unfinished batch pinned to another runtime reads the ledger in its own schema version;
    upgrading the schema under it would strand it exactly as a runtime reinstall once did."""
    stranded = [
        str(batch.get("batch_id"))
        for batch in ledger.upgrade_source_batches()
        if batch.get("state") not in TERMINAL_BATCH_STATES
        and isinstance(pinned := batch.get("harness_runtime_sha256"), str)
        and not _runtime_matches(repo, pinned)
    ]
    if stranded:
        raise CoordinatorError(
            "ledger migrate is refused while batches pinned to another harness runtime are "
            "unfinished: " + ", ".join(stranded),
            remedy="finish or abandon the listed batches on their pinned runtime, then migrate",
        )


def reset_ledger(args: argparse.Namespace) -> JsonObject:
    """Select an empty generation only after an explicit confirmation and no active batch."""
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        try:
            return ledger.reset(args.confirm)
        except LedgerError as exc:
            raise CoordinatorError(exc.message, remedy=exc.remedy) from exc


def clean_ledger(args: argparse.Namespace) -> JsonObject:
    """Safely remove orphaned dispatch evidence from the ledger state."""
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        try:
            return ledger.clean()
        except LedgerError as exc:
            raise CoordinatorError(exc.message, remedy=exc.remedy) from exc


def _release_verdict(
    state: JsonObject,
    *,
    host: str,
    pid_active: Callable[[int], bool],
    stale_after: int,
) -> tuple[bool, str]:
    """Whether ``ledger release-lock`` may release the lock ``state`` describes, and why.

    A recorded owner is judged by its process: only a process on this host can be checked, and a
    live one is never released whatever the lock's age.  A lock without a usable owner record is
    released only once it is ``stale_after`` seconds old."""
    owner = state.get("owner")
    pid = owner.get("pid") if isinstance(owner, dict) else None
    owner_host = owner.get("host") if isinstance(owner, dict) else None
    if (
        isinstance(pid, int)
        and not isinstance(pid, bool)
        and pid > 0
        and isinstance(owner_host, str)
    ):
        if owner_host != host:
            return False, "owner-on-another-host"
        if pid_active(pid):
            return False, "owner-alive"
        return True, "owner-dead"
    if state["held_seconds"] >= stale_after:
        return True, "owner-unknown-stale"
    return False, "owner-unknown-recent"


def release_ledger_lock(args: argparse.Namespace) -> JsonObject:
    """Release a ledger lock only after checking its owner; a live owner is always refused."""
    repo = _repo(args)
    ledger = LifecycleLedger(_state_root(args, repo))
    state = ledger.lock_state()
    if state is None:
        return {"released": False, "lock": None}
    release, reason = _release_verdict(
        state,
        host=socket.gethostname(),
        pid_active=_pid_active,
        stale_after=LEDGER_LOCK_STALE_SECONDS,
    )
    if not release:
        owner = state["owner"]
        holder = (
            f"pid {owner.get('pid')} on {owner.get('host')}"
            if isinstance(owner, dict)
            else "an unrecorded owner"
        )
        remaining = LEDGER_LOCK_STALE_SECONDS - cast(int, state["held_seconds"])
        remedy = {
            "owner-alive": "let the owner process finish its ledger operation and repeat the "
            "command that met the lock; a lock whose owner process is alive is never released",
            "owner-on-another-host": "run 'coordinator.py ledger release-lock' on the owner's "
            "host, where its process can be checked",
            "owner-unknown-recent": "repeat 'coordinator.py ledger release-lock' in "
            f"{remaining} seconds; a lock without a readable owner record is released only once "
            f"it has been held for {LEDGER_LOCK_STALE_SECONDS} seconds",
        }[reason]
        raise CoordinatorError(
            f"ledger lock is not released ({reason}): held by {holder} for "
            f"{state['held_seconds']} seconds",
            remedy=remedy,
        )
    try:
        ledger.break_lock(state)
    except LedgerError as exc:
        raise CoordinatorError(exc.message, remedy=exc.remedy) from exc
    return {"released": True, "reason": reason, "lock": state}
