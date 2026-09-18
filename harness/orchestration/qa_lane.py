"""Repository-scoped clean-room QA lane.

The lane owns queueing, leasing and gate execution.  It constructs its own ``LifecycleLedger`` for
the state root it is given and uses the ledger's own lock/write/replace/delete primitives directly;
its injected facade (``ops``) still carries coordinator-owned things this module does not own, such
as validation, loading and ``_now()``.
"""

from __future__ import annotations

import hashlib
import os
import socket
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from gate_runner import CleanRoomPolicy, GateRunnerError, run_gate
from ledger import BatchRecord, DispatchStatusRecord, LedgerError, LifecycleLedger


def _state_root(args: Any, repo: Path, ops: Any) -> Path:
    if getattr(args, "state_dir", None):
        raise ops.CoordinatorError("QA lane is repository-scoped and does not support --state-dir")
    return repo / ops.STATE_REL


@contextmanager
def _lock(ledger: LifecycleLedger, ops: Any) -> Iterator[None]:
    """Exclusive ledger lock, translating ``LedgerError`` to ``ops.CoordinatorError`` at this call
    site.  ``ledger.py`` deliberately never imports from ``coordinator.py``, and coordinator's CLI
    boundary only catches ``CoordinatorError``, so every direct ledger call this module makes must
    translate here (mirrors coordinator.py's own ``_ledger_lock``)."""
    try:
        with ledger.lock():
            yield
    except LedgerError as exc:
        raise ops.CoordinatorError(str(exc)) from exc


def _records_root(ledger: LifecycleLedger, ops: Any) -> Path:
    try:
        return ledger.records_root()
    except LedgerError as exc:
        raise ops.CoordinatorError(str(exc)) from exc


def _write_immutable(ledger: LifecycleLedger, ops: Any, path: Path, value: dict[str, Any]) -> None:
    try:
        ledger.write_immutable(path, value)
    except LedgerError as exc:
        raise ops.CoordinatorError(str(exc)) from exc


def _write_artifact(ledger: LifecycleLedger, ops: Any, path: Path, value: str) -> None:
    try:
        ledger.write_artifact(path, value)
    except LedgerError as exc:
        raise ops.CoordinatorError(str(exc)) from exc


def _replace_path(ledger: LifecycleLedger, ops: Any, path: Path, value: dict[str, Any]) -> None:
    try:
        ledger.replace(path, value)
    except LedgerError as exc:
        raise ops.CoordinatorError(str(exc)) from exc


def _replace_record(ledger: LifecycleLedger, ops: Any, record: Any) -> None:
    try:
        ledger.replace_record(record)
    except LedgerError as exc:
        raise ops.CoordinatorError(str(exc)) from exc


def _delete_record(ledger: LifecycleLedger, ops: Any, path: Path, *, reason: str) -> None:
    try:
        ledger.delete(path, reason=reason)
    except LedgerError as exc:
        raise ops.CoordinatorError(str(exc)) from exc


def _lane_path(ledger: LifecycleLedger, ops: Any) -> Path:
    return _records_root(ledger, ops) / "qa-lane" / "lease.json"


def _queue_root(ledger: LifecycleLedger, ops: Any) -> Path:
    return _records_root(ledger, ops) / "qa-lane" / "queue"


def _counter_path(ledger: LifecycleLedger, ops: Any) -> Path:
    return _records_root(ledger, ops) / "qa-lane" / "sequence.json"


def _artifact_path(ledger: LifecycleLedger, checksum: str, ops: Any) -> Path:
    return _records_root(ledger, ops) / "qa-artifacts" / f"{checksum}.log"


def _queue_entries(ledger: LifecycleLedger, ops: Any) -> list[tuple[Path, dict[str, Any]]]:
    entries: list[tuple[Path, dict[str, Any]]] = []
    for path in _queue_root(ledger, ops).glob("*.json"):
        entry = ops._read_object(path, "QA queue entry")
        if set(entry) != ops.QA_QUEUE_FIELDS or not isinstance(entry["sequence"], int) or entry["sequence"] < 1:
            raise ops.CoordinatorError("QA queue entry has an invalid schema")
        ops._safe_id(entry["dispatch_id"], "QA queue dispatch")
        if not ops._non_empty(entry["queued_at"]):
            raise ops.CoordinatorError("QA queue entry has an invalid queued_at value")
        entries.append((path, entry))
    return sorted(entries, key=lambda item: item[1]["sequence"])


def _enqueue(ledger: LifecycleLedger, dispatch_id: str, ops: Any) -> tuple[Path, dict[str, Any]]:
    for path, entry in _queue_entries(ledger, ops):
        if entry["dispatch_id"] == dispatch_id:
            return path, entry
    counter_path = _counter_path(ledger, ops)
    counter = ops._read_object(counter_path, "QA queue sequence") if counter_path.exists() else {"next": 1}
    if set(counter) != {"next"} or not isinstance(counter["next"], int) or counter["next"] < 1:
        raise ops.CoordinatorError("QA queue sequence is invalid")
    entry = {"dispatch_id": dispatch_id, "sequence": counter["next"], "queued_at": ops._now()}
    path = _queue_root(ledger, ops) / f"{entry['sequence']:020d}-{dispatch_id}.json"
    _write_immutable(ledger, ops, path, entry)
    next_counter = {"next": counter["next"] + 1}
    if counter_path.exists():
        _replace_path(ledger, ops, counter_path, next_counter)
    else:
        # The first queue entry in a fresh ledger has no mutable counter yet.  Seed it as an
        # immutable record; subsequent enqueues may use the ledger transition primitive.
        _write_immutable(ledger, ops, counter_path, next_counter)
    return path, entry


def _lease(ledger: LifecycleLedger, ops: Any) -> dict[str, Any] | None:
    path = _lane_path(ledger, ops)
    if not path.exists():
        return None
    lease = ops._read_object(path, "QA lease")
    if set(lease) != ops.QA_LEASE_FIELDS or not ops._non_empty(lease.get("host")) or not isinstance(lease.get("pid"), int):
        raise ops.CoordinatorError("QA lease has an invalid schema")
    ops._safe_id(lease.get("dispatch_id"), "QA lease dispatch")
    for field in ("acquired_at", "expires_at"):
        if not ops._non_empty(lease.get(field)):
            raise ops.CoordinatorError(f"QA lease has an invalid {field}")
    return lease


def _lease_expired(lease: dict[str, Any], ops: Any) -> bool:
    return ops._moment(lease["expires_at"], "QA lease expiry") <= datetime.now(timezone.utc)


def qa_evidence(args: Any, ops: Any) -> dict[str, Any]:
    """Verify accepted green QA evidence for one current issue-branch candidate."""
    repo = ops._repo(args)
    root = _state_root(args, repo, ops)
    ticket = args.ticket.strip() if ops._non_empty(args.ticket) else ""
    branch = args.branch.strip() if ops._non_empty(args.branch) else ""
    if not ticket or not branch:
        raise ops.CoordinatorError("QA evidence requires non-empty ticket and branch")
    candidate = ops._candidate_commit(repo, args.candidate_commit)
    ledger = LifecycleLedger(root)
    with _lock(ledger, ops):
        batch = ops._batch_for_ticket_branch(root, ticket, branch, candidate, getattr(args, "batch", None))
        report = ops._accepted_qa_for_candidate(root, batch, candidate)
    return {"batch_id": batch["batch_id"], "ticket": ticket, "branch": branch, "candidate_commit": candidate, "qa_report": report}


def _qa_report(dispatch: dict[str, Any], checks: list[dict[str, str]], artifact: Path, checksum: str) -> dict[str, Any]:
    failed = any(check["result"] == "fail" for check in checks)
    return {
        "dispatch_id": dispatch["dispatch_id"], "ticket": dispatch["ticket"], "role": "qa",
        "outcome": "failed" if failed else "completed",
        "output": f"QA gate {'failed' if failed else 'passed'}; full sanitised output: {artifact.as_posix()} (sha256:{checksum})",
        "commit_sha": "not applicable — read-only role", "changed_files": [], "checks_run": checks,
        "risks": "QA gate failed; inspect immutable evidence" if failed else "none",
        "blockers": "new approved developer retry required" if failed else "none",
        "next_coordinator_action": "create a new approved developer retry" if failed else "accept or continue",
        "report_language": "ru",
    }


def _record_report(
    ledger: LifecycleLedger, root: Path, repo: Path, dispatch: dict[str, Any], report: dict[str, Any], ops: Any,
) -> Path:
    batch = ops._load_batch(root, dispatch["batch_id"])
    ops._validate_batch_integrity(root, batch)
    ops._validate_dispatch(repo, ops._config(repo), root, batch, dispatch)
    status = ops._load_dispatch_status(root, dispatch["dispatch_id"])
    entry = next((item for item in batch.get("dispatches", []) if item["dispatch_id"] == dispatch["dispatch_id"]), None)
    if not entry or entry.get("state") != "dispatched" or status.get("state") != "working":
        raise ops.CoordinatorError("QA report requires a running QA dispatch")
    ops._validate_report(report, dispatch, ops._role(repo, "qa"), repo, batch.get("base_commit"))
    return ops._persist_report(ledger, root, batch, dispatch, report)


def run(args: Any, ops: Any) -> dict[str, Any]:
    repo = ops._repo(args)
    root = _state_root(args, repo, ops)
    lease_seconds = args.lease_seconds
    if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds < 1:
        raise ops.CoordinatorError("QA lease-seconds must be a positive integer")
    ledger = LifecycleLedger(root)
    with _lock(ledger, ops):
        dispatch = ops._load_dispatch(root, args.dispatch)
        batch = ops._load_batch(root, dispatch["batch_id"])
        ops._validate_batch_integrity(root, batch)
        ops._validate_dispatch(repo, ops._config(repo), root, batch, dispatch)
        if dispatch["role"] != "qa":
            raise ops.CoordinatorError("clean-room QA runner accepts only QA dispatches")
        if not dispatch["verification_commands"]:
            raise ops.CoordinatorError("clean-room QA runner requires configured verification_commands")
        status = ops._load_dispatch_status(root, dispatch["dispatch_id"])
        entry = next((item for item in batch.get("dispatches", []) if item["dispatch_id"] == dispatch["dispatch_id"]), None)
        if not entry or entry.get("state") != "approved" or status.get("state") != "approved":
            raise ops.CoordinatorError("QA runner requires an approved, unsent dispatch")
        queue_path, _ = _enqueue(ledger, dispatch["dispatch_id"], ops)
        queue = _queue_entries(ledger, ops)
        position = next(index for index, (_, item) in enumerate(queue, start=1) if item["dispatch_id"] == dispatch["dispatch_id"])
        lease = _lease(ledger, ops)
        if lease is not None:
            if _lease_expired(lease, ops):
                raise ops.CoordinatorError("QA lease is stale; a coordinator must clear it explicitly before another gate runs")
            return {"dispatch_id": dispatch["dispatch_id"], "state": "queued", "position": position}
        if position != 1:
            return {"dispatch_id": dispatch["dispatch_id"], "state": "queued", "position": position}
        lease = {
            "dispatch_id": dispatch["dispatch_id"], "host": socket.gethostname(), "pid": os.getpid(),
            "acquired_at": ops._now(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat(),
        }
        _write_immutable(ledger, ops, _lane_path(ledger, ops), lease)
        entry["state"] = "dispatched"
        ops._safe_id(dispatch["dispatch_id"], "dispatch")
        _replace_record(ledger, ops, DispatchStatusRecord.from_dict(
            {"dispatch_id": dispatch["dispatch_id"], "state": "working", "updated_at": ops._now()}
        ))
        ops._safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, ops, BatchRecord.from_dict(batch))
    try:
        # The first failed deterministic gate is sufficient evidence for a developer retry.  Do
        # not consume CI time and coordinator context collecting unrelated failures afterwards.
        gate = run_gate(dispatch["verification_commands"], CleanRoomPolicy(repo, dispatch["candidate_commit"]), stop_on_failure=True)
    except GateRunnerError as exc:
        raise ops.CoordinatorError(str(exc)) from exc
    artifact_text, checks = gate.artifact, gate.checks
    checksum = hashlib.sha256(artifact_text.encode("utf-8")).hexdigest()
    artifact = _artifact_path(ledger, checksum, ops)
    try:
        _write_artifact(ledger, ops, artifact, artifact_text)
    except ops.CoordinatorError as exc:
        raise ops.CoordinatorError("could not persist immutable QA evidence") from exc
    report = _qa_report(dispatch, checks, artifact, checksum)
    with _lock(ledger, ops):
        report_path = _record_report(ledger, root, repo, dispatch, report, ops)
        _queue_entries(ledger, ops)
        if queue_path.exists():
            _delete_record(ledger, ops, queue_path, reason="complete QA queue entry")
        lease_path = _lane_path(ledger, ops)
        current = _lease(ledger, ops)
        if current and current["dispatch_id"] == dispatch["dispatch_id"]:
            _delete_record(ledger, ops, lease_path, reason="complete QA lease")
    return {"dispatch_id": dispatch["dispatch_id"], "state": "reported", "report": str(report_path), "artifact": str(artifact), "sha256": checksum}


def status(args: Any, ops: Any) -> dict[str, Any]:
    repo = ops._repo(args)
    root = _state_root(args, repo, ops)
    ledger = LifecycleLedger(root)
    with _lock(ledger, ops):
        queue, lease = _queue_entries(ledger, ops), _lease(ledger, ops)
        return {"lease": lease, "lease_stale": _lease_expired(lease, ops) if lease else False, "queue": [entry for _, entry in queue]}


def clear_stale_lease(args: Any, ops: Any) -> dict[str, Any]:
    repo = ops._repo(args)
    root = _state_root(args, repo, ops)
    ledger = LifecycleLedger(root)
    with _lock(ledger, ops):
        lease = _lease(ledger, ops)
        if lease is None:
            raise ops.CoordinatorError("there is no QA lease to clear")
        if not _lease_expired(lease, ops):
            raise ops.CoordinatorError("a live QA lease cannot be force-unlocked")
        expected = {"host": args.expected_host, "pid": args.expected_pid, "expires_at": args.expected_expiry}
        if any(lease[field] != value for field, value in expected.items()):
            raise ops.CoordinatorError("QA lease changed; coordinator must validate the current owner again")
        recovery = {"cleared_dispatch_id": lease["dispatch_id"], "lease": lease, "approval": ops._approval(args), "reason": args.reason.strip(), "cleared_at": ops._now()}
        _write_immutable(ledger, ops, _records_root(ledger, ops) / "qa-lane" / "recoveries" / f"{uuid.uuid4()}.json", recovery)
        owner = next(((path, entry) for path, entry in _queue_entries(ledger, ops) if entry["dispatch_id"] == lease["dispatch_id"]), None)
        if owner is not None:
            _delete_record(ledger, ops, owner[0], reason="clear stale QA lease queue entry")
        _delete_record(ledger, ops, _lane_path(ledger, ops), reason="clear stale QA lease")
    return {"state": "cleared", "dispatch_id": lease["dispatch_id"]}
