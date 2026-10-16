"""The `ledger` CLI commands: inspect, migrate, reset and clean the lifecycle state, and release
a ledger lock its owner left behind.

These are the only commands that operate on the ledger as a whole rather than on a batch, and the
only ones allowed to discard state -- each behind its own explicit subcommand.
"""

from __future__ import annotations

import argparse
import json
import socket
from collections.abc import Callable
from pathlib import Path
from typing import cast

from harness.cleanup import _pid_active
from harness.orchestration.core.constants import (
    LEDGER_LOCK_STALE_SECONDS,
    TERMINAL_BATCH_STATES,
)
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _repo,
    _safe_id,
)
from harness.orchestration.core.workspace import _runtime_matches
from harness.orchestration.ledger.generation_migration import export, inspection
from harness.orchestration.ledger.ledger_ops import (
    _ledger_errors,
    _ledger_lock,
    _state_root,
)
from harness.orchestration.ledger.lifecycle import (
    FORCE_RELEASE_COMMAND,
    LEDGER_VERSION,
    BatchRecord,
    LedgerError,
    LifecycleLedger,
    lock_environment,
)
from harness.orchestration.ledger.strict_read import require_canonical
from harness.orchestration.runtime_migration import migrate_system_fields
from harness.orchestration.runtime_state_store import (
    RuntimeStateError,
    RuntimeStateStore,
    sanitize_detail,
)


def ledger_status(args: argparse.Namespace) -> JsonObject:
    """Report the selected lifecycle-ledger generation without changing it."""
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger), _ledger_errors():
        return ledger.status()


def validate_ledger(args: argparse.Namespace) -> JsonObject:
    """Read-only generation, audit, record graph, canonical machine field, recorded batch
    transition and batch-contract validation; history no migration vouches for names the
    'ledger migrate' route."""
    from harness.orchestration.workflow.history import _validate_batch_integrity
    from harness.orchestration.core.utils import _read_object

    root = _state_root(args, _repo(args))
    ledger = LifecycleLedger(root)
    if not root.exists():
        return ledger.validate()
    with _ledger_lock(ledger), _ledger_errors():
        result = ledger.validate()
        if result["version"] == LEDGER_VERSION:
            records = ledger.records_root()
            require_canonical(records)
            for batch_id in cast(list[str], result["batches"]):
                name = f"{batch_id}.json"
                batch = _read_object(records / "batches" / name, "batch")
                LifecycleLedger.check_batch_transitions(records, batch, name)
                _validate_batch_integrity(root, batch)
        return result


def migrate_ledger(args: argparse.Namespace) -> JsonObject:
    """Explicitly validate legacy state and atomically select its versioned replacement, then
    migrate the selected generation to the canonical system fields behind a durable intent."""
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger), _ledger_errors():
        _refuse_upgrade_under_pinned_batches(repo, ledger)
        upgraded = ledger.migrate()
        system = migrate_system_fields(repo, root, ledger)
        pointer = ledger.pointer()
    return {
        "version": 0 if pointer is None else pointer["version"],
        "generation": None if pointer is None else pointer["generation"],
        "migrated": bool(upgraded["migrated"] or system["migrated"]),
        "schema_upgraded": upgraded["migrated"],
        "system_fields": system,
    }


def inspect_ledger(args: argparse.Namespace) -> JsonObject:
    """Read-only inspection of the selected generation, its migration and its quarantine, or of
    the records of a legacy ledger no migration selected yet (``generation`` ``None``), and of
    the runtime state that denies a batch.  It takes no lock, loads no config and never reads a
    record through the ledger's validating loader, so a quarantined or unreadable batch is shown
    as a finding instead of stopping it.  The refusal the strict read's check of recorded
    transitions gives an operational batch is shown as its ``transition_problem`` instead of
    stopping it too."""
    repo = _repo(args)
    root = _state_root(args, repo)
    records, generation = _lenient_records(LifecycleLedger(root))
    report: JsonObject = (
        {"generation": None, "migration": None, "unowned": [], "batches": []}
        if records is None
        else {**inspection(records), "generation": generation}
    )
    batches = cast(list[JsonObject], report["batches"])
    if (batch_id := getattr(args, "batch", None)) is not None:
        batches = [batch for batch in batches if batch["batch_id"] == batch_id]
        if not batches:
            raise _no_batch(batch_id, generation)
    for batch in batches:
        batch["findings"] = [
            {
                **finding,
                "value": sanitize_detail(repr(finding["value"])),
                "reason": sanitize_detail(str(finding["reason"])),
            }
            for finding in batch["findings"]
        ]
        batch["transition_problem"] = (
            None if records is None else _transition_problem(records, batch)
        )
    report["batches"] = batches
    try:
        store: RuntimeStateStore | None = RuntimeStateStore(repo, root)
    except RuntimeStateError as exc:
        store, report["runtime_state"] = None, {"available": False, "code": exc.code}
    if store is not None:
        report["runtime_state"] = {
            "available": True,
            "ledger_scope": _runtime(store, None),
        }
        for batch in batches:
            batch["runtime"] = _runtime(store, str(batch["batch_id"]))
    return report


def export_ledger(args: argparse.Namespace) -> JsonObject:
    """Read-only export of one batch, byte for byte: its graph and reports, or its quarantined
    graph and evidence.  It takes no lock and writes nothing; the JSON goes to stdout."""
    repo = _repo(args)
    batch_id = _safe_id(args.batch, "batch")
    records, generation = _lenient_records(LifecycleLedger(_state_root(args, repo)))
    exported = None if records is None else export(records, batch_id)
    if exported is None:
        raise _no_batch(batch_id, generation)
    return {**exported, "generation": generation}


def _transition_problem(records: Path, batch: JsonObject) -> str | None:
    """The bounded refusal the strict read gives the recorded transitions of ``batch``, as
    inspection shows it; ``None`` when they pass or nothing is there to check: a quarantined
    batch, or a batch record its findings already show unreadable."""
    path = records / BatchRecord.directory / f"{batch['batch_id']}.json"
    if batch["area"] != "operational" or not path.is_file():
        return None
    try:
        record: object = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    try:
        LifecycleLedger.check_batch_transitions(
            records, cast(JsonObject, record), path.name
        )
    except LedgerError as exc:
        return sanitize_detail(exc.message)
    return None


def _lenient_records(ledger: LifecycleLedger) -> tuple[Path | None, str | None]:
    """The directory of the records the pointer names and its generation, never validated.  A
    legacy ledger that no migration selected yet keeps its records in the state directory itself,
    outside any generation; ``(None, None)`` without records."""
    path = ledger.records_root_lenient()
    if path is None or path == ledger.root:
        return path, None
    return path, path.name


def _no_batch(batch_id: str, generation: str | None) -> CoordinatorError:
    where = "the ledger" if generation is None else generation
    return CoordinatorError(
        f"{where} holds no batch {batch_id}, neither operational nor quarantined",
        remedy="list the batches with 'coordinator.py ledger inspect'",
    )


def _runtime(store: RuntimeStateStore, batch_id: str | None) -> JsonObject | None:
    """The runtime state of ``batch_id`` (``None``: the ledger scope) as inspection shows it."""
    try:
        record = store.read(batch_id)
    except RuntimeStateError as exc:
        return {"error": exc.code}
    if record is None:
        return None
    operation = record["operation"]
    return {
        "state": record["state"],
        "failure": record["failure"],
        "operation": None
        if operation is None
        else {"kind": operation["kind"], "phase": operation["phase"]},
    }


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
    with _ledger_lock(ledger), _ledger_errors():
        return ledger.reset(args.confirm)


def clean_ledger(args: argparse.Namespace) -> JsonObject:
    """Safely remove orphaned dispatch evidence from the ledger state."""
    repo = _repo(args)
    root = _state_root(args, repo)
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


def ledger_lock_status(args: argparse.Namespace) -> JsonObject:
    """Show the ledger lock without taking it, so it works while the ledger is busy.

    ``remaining_seconds`` counts down to the timeout release of an owner that cannot be checked;
    ``release_available`` says whether a plain ``ledger release-lock`` would release it now."""
    ledger = LifecycleLedger(_state_root(args, _repo(args)))
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


def release_ledger_lock(args: argparse.Namespace) -> JsonObject:
    """Release a ledger lock only after checking its owner; a live owner is always refused.

    ``--force`` releases it whatever the verdict, behind a human approval and an audit record."""
    repo = _repo(args)
    ledger = LifecycleLedger(_state_root(args, repo))
    state = ledger.lock_state()
    if state is None:
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
