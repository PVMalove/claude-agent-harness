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
from harness.orchestration.ledger.ledger_ops import (
    _ledger_errors,
    _ledger_lock,
    _state_root,
    read_state,
)
from harness.orchestration.ledger.lifecycle import (
    FORCE_RELEASE_COMMAND,
    LEDGER_VERSION,
    STORE_LEDGER_VERSION,
    LifecycleLedger,
    lock_environment,
    store_is_authority,
)


def ledger_status(args: argparse.Namespace) -> JsonObject:
    """Report the selected lifecycle-ledger generation without changing it."""
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with read_state(ledger, repo), _ledger_errors():
        return ledger.status()


def validate_ledger(args: argparse.Namespace) -> JsonObject:
    """Read-only generation, audit, record graph and batch-contract validation.

    ``--full`` recomputes the audit chain of the runtime store from its genesis and compares every
    row with its audit records, never using the checkpoint; a file ledger keeps none."""
    from harness.orchestration.workflow.history import _validate_batch_integrity
    from harness.orchestration.core.utils import _read_object

    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    full = bool(getattr(args, "full", False))
    if not root.exists():
        return ledger.validate(full=full)
    with read_state(ledger, repo), _ledger_errors():
        result = ledger.validate(full=full)
        if result["version"] in (LEDGER_VERSION, STORE_LEDGER_VERSION):
            records = ledger.records_root()
            for batch_id in cast(list[str], result["batches"]):
                _validate_batch_integrity(
                    root,
                    _read_object(records / "batches" / f"{batch_id}.json", "batch"),
                )
        return result


# What each command that rewrites the file ledger says in the runtime-store epoch: the store has
# no generation to select, no orphaned dispatch evidence and no state to throw away.
_STORE_EPOCH_REMEDIES = {
    "migrate": "the state was cut over to the runtime store; its history was imported once by "
    "`harness-runtime migrate`, and a schema change of the store is applied by the runtime",
    "clean": "the runtime store writes every record with its foreign keys in one transaction, so it "
    "holds no orphaned dispatch evidence; `coordinator.py ledger validate --full` checks it",
    "reset": "the runtime store is never emptied by a command; restore the state directory from "
    "the snapshot named in AUTHORITY.json if the store must be discarded",
}


def _refuse_in_store_epoch(root: Path, command: str) -> None:
    """A command that rewrites the file ledger never reaches the runtime store."""
    if store_is_authority(root):
        raise CoordinatorError(
            f"`ledger {command}` does not run in the runtime-store epoch",
            remedy=_STORE_EPOCH_REMEDIES[command],
        )


def migrate_ledger(args: argparse.Namespace) -> JsonObject:
    """Explicitly validate legacy state and atomically select its versioned replacement."""
    repo = _repo(args)
    root = _state_root(args, repo)
    _refuse_in_store_epoch(root, "migrate")
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger), _ledger_errors():
        _refuse_upgrade_under_pinned_batches(repo, ledger)
        return ledger.migrate()


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
    _refuse_in_store_epoch(root, "reset")
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger), _ledger_errors():
        return ledger.reset(args.confirm)


def clean_ledger(args: argparse.Namespace) -> JsonObject:
    """Safely remove orphaned dispatch evidence from the ledger state."""
    repo = _repo(args)
    root = _state_root(args, repo)
    _refuse_in_store_epoch(root, "clean")
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger), _ledger_errors():
        return ledger.clean()


ENVIRONMENT_FIELDS = ("pid_namespace", "boot_id")


def _other_environment(owner: JsonObject, environment: JsonObject | None) -> bool:
    """Whether the owner recorded a PID namespace or boot that differs from this process's.

    Only a value both sides know is compared: a legacy record without these fields, or a
    platform without ``/proc``, keeps the pid check."""
    if not environment:
        return False
    return any(
        isinstance(owner.get(key), str)
        and isinstance(environment.get(key), str)
        and owner[key] != environment[key]
        for key in ENVIRONMENT_FIELDS
    )


def _release_verdict(
    state: JsonObject,
    *,
    host: str,
    pid_active: Callable[[int], bool],
    stale_after: int,
    environment: JsonObject | None = None,
) -> tuple[bool, str]:
    """Whether ``ledger release-lock`` may release the lock ``state`` describes, and why.

    A recorded owner is judged by its process: only a process on this host and in this PID
    namespace and boot can be checked, and a live one is never released whatever the lock's age.
    An owner from another environment cannot be checked by its pid, so it is released like a lock
    without a usable owner record: only once it is ``stale_after`` seconds old."""
    owner = state.get("owner")
    pid = owner.get("pid") if isinstance(owner, dict) else None
    owner_host = owner.get("host") if isinstance(owner, dict) else None
    held = cast(int, state["held_seconds"])
    if (
        isinstance(owner, dict)
        and isinstance(pid, int)
        and not isinstance(pid, bool)
        and pid > 0
        and isinstance(owner_host, str)
    ):
        if owner_host != host:
            return False, "owner-on-another-host"
        if _other_environment(owner, environment):
            if held >= stale_after:
                return True, "owner-unknown-stale"
            return False, "owner-in-other-environment"
        if pid_active(pid):
            return False, "owner-alive"
        return True, "owner-dead"
    if held >= stale_after:
        return True, "owner-unknown-stale"
    return False, "owner-unknown-recent"


def _release_verdict_here(state: JsonObject) -> tuple[bool, str]:
    """The verdict of this process on ``state``: its host, PID namespace and boot."""
    return _release_verdict(
        state,
        host=socket.gethostname(),
        pid_active=_pid_active,
        stale_after=LEDGER_LOCK_STALE_SECONDS,
        environment=cast(JsonObject, lock_environment()),
    )


def _store_qa_lease(repo: Path) -> JsonObject | None:
    """The QA lease of the runtime store as ``qa.lane.inspect`` answers it, or ``None`` for a
    free lane; a runtime that cannot answer is shown, not raised."""
    from harness.orchestration import qa_lease
    from harness.orchestration.runtime_client import RuntimeClientError

    try:
        return qa_lease.inspect(repo)
    except RuntimeClientError as exc:
        return {"error": f"{exc.code}: {exc.message}"}


def ledger_lock_status(args: argparse.Namespace) -> JsonObject:
    """Show the ledger lock without taking it, so it works while the ledger is busy.

    ``remaining_seconds`` counts down to the timeout release of an owner that cannot be checked;
    ``release_available`` says whether a plain ``ledger release-lock`` would release it now. In
    the runtime-store epoch ``qa_lease`` adds the QA lease of the store with the verdict of the
    runtime on its owner (``owner-dead`` only by the identity of the owner's process)."""
    repo = _repo(args)
    ledger = LifecycleLedger(_state_root(args, repo))
    shown = _file_lock_status(ledger)
    if store_is_authority(ledger.root):
        shown["qa_lease"] = _store_qa_lease(repo)
    return shown


def _file_lock_status(ledger: LifecycleLedger) -> JsonObject:
    state = ledger.lock_state()
    if state is None:
        return {"locked": False}
    releasable, verdict = _release_verdict_here(state)
    owner = state.get("owner")
    held = cast(int, state["held_seconds"])
    return {
        "locked": True,
        "path": state["path"],
        "acquired_at": state["acquired_at"],
        "held_seconds": held,
        "stale_after_seconds": LEDGER_LOCK_STALE_SECONDS,
        "remaining_seconds": max(0, LEDGER_LOCK_STALE_SECONDS - held),
        "owner": owner,
        "owner_record": state["owner_record"],
        "operation": owner.get("operation") if isinstance(owner, dict) else None,
        "environment": lock_environment(),
        "verdict": verdict,
        "release_available": releasable,
    }


def _force_release(
    args: argparse.Namespace, ledger: LifecycleLedger, state: JsonObject
) -> JsonObject:
    """Release the lock whatever its owner, behind a human approval, a reason and an audit.

    No policy reaches this path: ``approval_policy: auto`` never supplies ``--approved-by``, and a
    ``policy:`` approver is refused like everywhere else."""
    from harness.orchestration.workflow import approval as approvals

    reason = getattr(args, "reason", None)
    if not isinstance(reason, str) or not reason.strip():
        raise CoordinatorError(
            "a forced ledger lock release requires a reason",
            remedy="pass --reason naming why the owner cannot finish, for example the worker "
            "sandbox that was stopped",
        )
    approval = approvals._approval(args)
    _, verdict = _release_verdict_here(state)
    with _ledger_errors():
        audit_id = ledger.force_break_lock(
            state, {**approval, "reason": reason.strip(), "verdict": verdict}
        )
    return {
        "released": True,
        "reason": "forced",
        "verdict": verdict,
        "audit_id": audit_id,
        "lock": state,
    }


def _release_store_lease(
    args: argparse.Namespace, repo: Path, ledger: LifecycleLedger
) -> JsonObject:
    """Release the QA lease of the runtime store when no file lock is held.

    Without ``--force`` only an expired lease of a provably dead owner (``owner-dead``) is reaped,
    with the recovery of ``qa clear-stale-lease``; a live owner is refused with ``[lease_held]``
    and the verdict. ``--force`` clears it whatever the verdict, behind a human approval and a
    reason, and the runtime audits the decision."""
    from harness.orchestration import qa_lane, qa_lease
    from harness.orchestration.workflow import approval as approvals
    from harness.orchestration.workflow import qa_integration

    lease = qa_lease.inspect(repo)
    if lease is None:
        return {"released": False, "lock": None, "qa_lease": None}
    if getattr(args, "force", False):
        reason = getattr(args, "reason", None)
        if not isinstance(reason, str) or not reason.strip():
            raise CoordinatorError(
                "a forced QA lease release requires a reason",
                remedy="pass --reason naming why the owner cannot finish, for example the worker "
                "sandbox that was stopped",
            )
        restored = qa_lane.clear_lease(
            ledger,
            ledger.root,
            qa_integration._ops(),
            approval=approvals._approval(args),
            reason=reason.strip(),
        )
        return {
            "released": True,
            "reason": "forced",
            "verdict": lease["verdict"],
            "lock": None,
            "qa_lease": lease,
            "restored_dispatch_state": restored,
        }
    if not lease["release_available"]:
        owner = lease.get("owner")
        holder = (
            f"{owner.get('owner_kind')} {owner.get('owner_id')}"
            if isinstance(owner, dict)
            else "an unrecorded owner"
        )
        stale = str(lease["stale_heartbeat"]).lower()
        raise CoordinatorError(
            f"the QA lease is not released ({lease['verdict']}): held by {holder} until "
            f"{lease['expires_at']}",
            remedy=f"[lease_held] stale_heartbeat={stale} verdict={lease['verdict']}; the "
            "runtime releases only an expired lease whose owner is provably dead (owner-dead), "
            "and a live owner is never released; an operator who knows the owner is gone can "
            f"release it with '{FORCE_RELEASE_COMMAND}'",
        )
    qa_lane.reap_dead_owner(ledger, ledger.root, qa_integration._ops())
    return {"released": True, "reason": "owner-dead", "lock": None, "qa_lease": lease}


def release_ledger_lock(args: argparse.Namespace) -> JsonObject:
    """Release a ledger lock only after checking its owner; a live owner is always refused.

    ``--force`` releases it whatever the verdict, behind a human approval and an audit record.
    In the runtime-store epoch, when no file lock is held, the QA lease of the store is the lock
    this releases (``_release_store_lease``)."""
    repo = _repo(args)
    ledger = LifecycleLedger(_state_root(args, repo))
    state = ledger.lock_state()
    if state is None:
        if store_is_authority(ledger.root):
            return _release_store_lease(args, repo, ledger)
        return {"released": False, "lock": None}
    if getattr(args, "force", False):
        return _force_release(args, ledger, state)
    release, reason = _release_verdict_here(state)
    if not release:
        owner = state["owner"]
        holder = (
            f"pid {owner.get('pid')} on {owner.get('host')}"
            if isinstance(owner, dict)
            else "an unrecorded owner"
        )
        remaining = LEDGER_LOCK_STALE_SECONDS - cast(int, state["held_seconds"])
        forced = f"; an operator can release it now with '{FORCE_RELEASE_COMMAND}'"
        remedy = {
            "owner-alive": "let the owner process finish its ledger operation and repeat the "
            "command that met the lock; a lock whose owner process is alive is never released. "
            "If the recorded pid belongs to a stopped sandboxed worker and names an unrelated "
            f"process here, an operator can release the lock with '{FORCE_RELEASE_COMMAND}'",
            "owner-on-another-host": "run 'coordinator.py ledger release-lock' on the owner's "
            "host, where its process can be checked" + forced,
            "owner-in-other-environment": "repeat 'coordinator.py ledger release-lock' in "
            f"{remaining} seconds; an owner from another PID namespace or boot cannot be checked "
            f"by its pid and is released once the lock has been held for "
            f"{LEDGER_LOCK_STALE_SECONDS} seconds" + forced,
            "owner-unknown-recent": "repeat 'coordinator.py ledger release-lock' in "
            f"{remaining} seconds; a lock without a readable owner record is released only once "
            f"it has been held for {LEDGER_LOCK_STALE_SECONDS} seconds" + forced,
        }[reason]
        raise CoordinatorError(
            f"ledger lock is not released ({reason}): held by {holder} for "
            f"{state['held_seconds']} seconds",
            remedy=remedy,
        )
    with _ledger_errors():
        ledger.break_lock(state)
    return {"released": True, "reason": reason, "lock": state}
