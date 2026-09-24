"""Repository-scoped clean-room QA lane.

The lane owns queueing, leasing and gate execution.  It constructs its own ``LifecycleLedger`` for
the state root it is given and uses the ledger's own lock/write/replace/delete primitives directly;
its injected facade (``ops``) still carries coordinator-owned things this module does not own, such
as validation, loading and ``_now()``.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import socket
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, TypeGuard

from ..errors import HarnessError
from ..gate_runner.gate_runner import CleanRoomPolicy, GateRunnerError, run_gate
from .contract import JsonObject
from .ledger import (
    BatchRecord,
    DispatchStatusRecord,
    LedgerError,
    LedgerRecordVO,
    LifecycleLedger,
)


class CoordinatorOps(Protocol):
    """The slice of ``coordinator.py`` this module calls; ``coordinator`` passes itself as ``ops``.

    ``ledger.py`` and this module deliberately never import ``coordinator.py``, so the coordinator's
    own error class and helpers arrive through this injected facade instead.
    """

    @property
    def CoordinatorError(self) -> type[HarnessError]: ...
    @property
    def STATE_REL(self) -> Path: ...
    @property
    def QA_QUEUE_FIELDS(self) -> set[str]: ...
    @property
    def QA_LEASE_FIELDS(self) -> set[str]: ...

    def _read_object(self, path: Path, label: str) -> JsonObject: ...
    def _safe_id(self, value: object, label: str) -> str: ...
    def _non_empty(self, value: object) -> TypeGuard[str]: ...
    def _now(self) -> str: ...
    def _moment(self, value: object, label: str) -> datetime: ...
    def _repo(self, args: argparse.Namespace) -> Path: ...
    def _candidate_commit(self, repo: Path, value: object) -> str: ...
    def _batch_for_ticket_branch(
        self,
        root: Path,
        ticket: str,
        branch: str,
        candidate: str,
        requested_batch: object = None,
    ) -> JsonObject: ...
    def _accepted_qa_for_candidate(
        self, root: Path, batch: JsonObject, candidate: str
    ) -> JsonObject: ...
    def _load_batch(self, root: Path, batch_id: str) -> JsonObject: ...
    def _load_dispatch(self, root: Path, dispatch_id: str) -> JsonObject: ...
    def _load_dispatch_status(self, root: Path, dispatch_id: str) -> JsonObject: ...
    def _validate_batch_integrity(self, root: Path, batch: JsonObject) -> None: ...
    def _validate_dispatch(
        self,
        repo: Path,
        config: JsonObject,
        root: Path,
        batch: JsonObject,
        dispatch: JsonObject,
    ) -> None: ...
    def _config(self, repo: Path) -> JsonObject: ...
    def _role(self, repo: Path, name: str) -> JsonObject: ...
    def _validate_report(
        self,
        report: JsonObject,
        dispatch: JsonObject,
        role: JsonObject,
        repo: Path | None = None,
        base_commit: str | None = None,
    ) -> None: ...
    def _persist_report(
        self,
        ledger: LifecycleLedger,
        root: Path,
        batch: JsonObject,
        dispatch: JsonObject,
        report: JsonObject,
    ) -> Path: ...
    def _approval(self, args: argparse.Namespace) -> dict[str, str]: ...


def _state_root(args: argparse.Namespace, repo: Path, ops: CoordinatorOps) -> Path:
    if getattr(args, "state_dir", None):
        raise ops.CoordinatorError(
            "QA lane is repository-scoped and does not support --state-dir",
            remedy="run the QA lane without --state-dir; it always uses the repository's .harness/orchestration/state",
        )
    return repo / ops.STATE_REL


@contextmanager
def _lock(ledger: LifecycleLedger, ops: CoordinatorOps) -> Iterator[None]:
    """Exclusive ledger lock, translating ``LedgerError`` to ``ops.CoordinatorError`` at this call
    site.  ``ledger.py`` deliberately never imports from ``coordinator.py``, and coordinator's CLI
    boundary only catches ``CoordinatorError``, so every direct ledger call this module makes must
    translate here (mirrors coordinator.py's own ``_ledger_lock``)."""
    try:
        with ledger.lock():
            yield
    except LedgerError as exc:
        raise ops.CoordinatorError(exc.message, remedy=exc.remedy) from exc


def _records_root(ledger: LifecycleLedger, ops: CoordinatorOps) -> Path:
    try:
        return ledger.records_root()
    except LedgerError as exc:
        raise ops.CoordinatorError(exc.message, remedy=exc.remedy) from exc


def _write_immutable(
    ledger: LifecycleLedger, ops: CoordinatorOps, path: Path, value: JsonObject
) -> None:
    try:
        ledger.write_immutable(path, value)
    except LedgerError as exc:
        raise ops.CoordinatorError(exc.message, remedy=exc.remedy) from exc


def _write_artifact(
    ledger: LifecycleLedger, ops: CoordinatorOps, path: Path, value: str
) -> None:
    try:
        ledger.write_artifact(path, value)
    except LedgerError as exc:
        raise ops.CoordinatorError(exc.message, remedy=exc.remedy) from exc


def _replace_path(
    ledger: LifecycleLedger, ops: CoordinatorOps, path: Path, value: JsonObject
) -> None:
    try:
        ledger.replace(path, value)
    except LedgerError as exc:
        raise ops.CoordinatorError(exc.message, remedy=exc.remedy) from exc


def _replace_record(
    ledger: LifecycleLedger, ops: CoordinatorOps, record: LedgerRecordVO
) -> None:
    try:
        ledger.replace_record(record)
    except LedgerError as exc:
        raise ops.CoordinatorError(exc.message, remedy=exc.remedy) from exc


def _delete_record(
    ledger: LifecycleLedger, ops: CoordinatorOps, path: Path, *, reason: str
) -> None:
    try:
        ledger.delete(path, reason=reason)
    except LedgerError as exc:
        raise ops.CoordinatorError(exc.message, remedy=exc.remedy) from exc


def _lane_path(ledger: LifecycleLedger, ops: CoordinatorOps) -> Path:
    return _records_root(ledger, ops) / "qa-lane" / "lease.json"


def _queue_root(ledger: LifecycleLedger, ops: CoordinatorOps) -> Path:
    return _records_root(ledger, ops) / "qa-lane" / "queue"


def _counter_path(ledger: LifecycleLedger, ops: CoordinatorOps) -> Path:
    return _records_root(ledger, ops) / "qa-lane" / "sequence.json"


def _artifact_path(ledger: LifecycleLedger, checksum: str, ops: CoordinatorOps) -> Path:
    return _records_root(ledger, ops) / "qa-artifacts" / f"{checksum}.log"


def _attempt_path(ledger: LifecycleLedger, ops: CoordinatorOps) -> Path:
    return _records_root(ledger, ops) / "qa-lane" / "attempts" / f"{uuid.uuid4()}.json"


def _queue_entries(
    ledger: LifecycleLedger, ops: CoordinatorOps
) -> list[tuple[Path, JsonObject]]:
    entries: list[tuple[Path, JsonObject]] = []
    for path in _queue_root(ledger, ops).glob("*.json"):
        entry = ops._read_object(path, "QA queue entry")
        if (
            set(entry) != ops.QA_QUEUE_FIELDS
            or not isinstance(entry["sequence"], int)
            or entry["sequence"] < 1
        ):
            raise ops.CoordinatorError(
                "QA queue entry has an invalid schema",
                remedy=f"fix {path}: it must hold exactly dispatch_id, sequence (an integer >= 1) and queued_at",
            )
        ops._safe_id(entry["dispatch_id"], "QA queue dispatch")
        if not ops._non_empty(entry["queued_at"]):
            raise ops.CoordinatorError(
                "QA queue entry has an invalid queued_at value",
                remedy=f"set a non-empty queued_at in {path}",
            )
        entries.append((path, entry))
    return sorted(entries, key=lambda item: item[1]["sequence"])


def _enqueue(
    ledger: LifecycleLedger, dispatch_id: str, ops: CoordinatorOps
) -> tuple[Path, JsonObject]:
    for path, entry in _queue_entries(ledger, ops):
        if entry["dispatch_id"] == dispatch_id:
            return path, entry
    counter_path = _counter_path(ledger, ops)
    counter = (
        ops._read_object(counter_path, "QA queue sequence")
        if counter_path.exists()
        else {"next": 1}
    )
    if (
        set(counter) != {"next"}
        or not isinstance(counter["next"], int)
        or counter["next"] < 1
    ):
        raise ops.CoordinatorError(
            "QA queue sequence is invalid",
            remedy=f"fix {counter_path}: it must be a JSON object holding only an integer 'next' >= 1",
        )
    entry = {
        "dispatch_id": dispatch_id,
        "sequence": counter["next"],
        "queued_at": ops._now(),
    }
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


def release_queue(
    ledger: LifecycleLedger, dispatch_ids: list[str], ops: CoordinatorOps
) -> list[str]:
    """Drop the queue entries of dispatches that will never run, so they cannot hold up the lane.

    Only queue entries go: a lease is left to ``clear_stale_lease``, which refuses a live one.
    """
    released = []
    for path, entry in _queue_entries(ledger, ops):
        if entry["dispatch_id"] in dispatch_ids:
            _delete_record(ledger, ops, path, reason="release abandoned QA queue entry")
            released.append(entry["dispatch_id"])
    return released


def _lease(ledger: LifecycleLedger, ops: CoordinatorOps) -> JsonObject | None:
    path = _lane_path(ledger, ops)
    if not path.exists():
        return None
    lease = ops._read_object(path, "QA lease")
    if (
        set(lease) != ops.QA_LEASE_FIELDS
        or not ops._non_empty(lease.get("host"))
        or not isinstance(lease.get("pid"), int)
    ):
        raise ops.CoordinatorError(
            "QA lease has an invalid schema",
            remedy=f"fix {path}: it must hold exactly dispatch_id, host, pid (an integer), acquired_at and expires_at",
        )
    ops._safe_id(lease.get("dispatch_id"), "QA lease dispatch")
    for field in ("acquired_at", "expires_at"):
        if not ops._non_empty(lease.get(field)):
            raise ops.CoordinatorError(
                f"QA lease has an invalid {field}",
                remedy=f"set a non-empty {field} in {path}",
            )
    return lease


def _lease_expired(lease: JsonObject, ops: CoordinatorOps) -> bool:
    return ops._moment(lease["expires_at"], "QA lease expiry") <= datetime.now(UTC)


def qa_evidence(args: argparse.Namespace, ops: CoordinatorOps) -> JsonObject:
    """Verify accepted green QA evidence for one current issue-branch candidate."""
    repo = ops._repo(args)
    root = _state_root(args, repo, ops)
    ticket = args.ticket.strip() if ops._non_empty(args.ticket) else ""
    branch = args.branch.strip() if ops._non_empty(args.branch) else ""
    if not ticket or not branch:
        raise ops.CoordinatorError(
            "QA evidence requires non-empty ticket and branch",
            remedy="pass non-empty --ticket and --branch",
        )
    candidate = ops._candidate_commit(repo, args.candidate_commit)
    ledger = LifecycleLedger(root)
    with _lock(ledger, ops):
        batch = ops._batch_for_ticket_branch(
            root, ticket, branch, candidate, getattr(args, "batch", None)
        )
        report = ops._accepted_qa_for_candidate(root, batch, candidate)
    return {
        "batch_id": batch["batch_id"],
        "ticket": ticket,
        "branch": branch,
        "candidate_commit": candidate,
        "qa_report": report,
    }


def _qa_report(
    dispatch: JsonObject, checks: list[dict[str, str]], artifact: Path, checksum: str
) -> JsonObject:
    failed = any(check["result"] == "fail" for check in checks)
    return {
        "dispatch_id": dispatch["dispatch_id"],
        "ticket": dispatch["ticket"],
        "role": "qa",
        "outcome": "failed" if failed else "completed",
        "output": f"QA gate {'failed' if failed else 'passed'}; full sanitised output: {artifact.as_posix()} (sha256:{checksum})",
        "commit_sha": "not applicable — read-only role",
        "changed_files": [],
        "checks_run": checks,
        "risks": "QA gate failed; inspect immutable evidence" if failed else "none",
        "blockers": "new approved developer retry required" if failed else "none",
        "next_coordinator_action": "create a new approved developer retry"
        if failed
        else "accept or continue",
        "report_language": "ru",
    }


def _record_report(
    ledger: LifecycleLedger,
    root: Path,
    repo: Path,
    dispatch: JsonObject,
    report: JsonObject,
    ops: CoordinatorOps,
) -> Path:
    batch = ops._load_batch(root, dispatch["batch_id"])
    ops._validate_batch_integrity(root, batch)
    ops._validate_dispatch(repo, ops._config(repo), root, batch, dispatch)
    status = ops._load_dispatch_status(root, dispatch["dispatch_id"])
    entry = next(
        (
            item
            for item in batch.get("dispatches", [])
            if item["dispatch_id"] == dispatch["dispatch_id"]
        ),
        None,
    )
    if (
        not entry
        or entry.get("state") != "dispatched"
        or status.get("state") != "working"
    ):
        raise ops.CoordinatorError(
            "QA report requires a running QA dispatch",
            remedy="the batch entry must be dispatched and the dispatch status working; if they are out of sync, abandon the batch and create a new QA dispatch",
        )
    ops._validate_report(
        report, dispatch, ops._role(repo, "qa"), repo, batch.get("base_commit")
    )
    return ops._persist_report(ledger, root, batch, dispatch, report)


def _recover_transient_failure(
    ledger: LifecycleLedger,
    root: Path,
    dispatch: JsonObject,
    queue_path: Path,
    failure: HarnessError,
    stage: str,
    ops: CoordinatorOps,
) -> None:
    """Return one QA dispatch to its retryable state after a non-terminal failure."""
    with _lock(ledger, ops):
        _write_immutable(
            ledger,
            ops,
            _attempt_path(ledger, ops),
            {
                "dispatch_id": dispatch["dispatch_id"],
                "stage": stage,
                "failed_at": ops._now(),
                "message": failure.message,
                "remedy": failure.remedy,
            },
        )
        if queue_path.exists():
            _delete_record(
                ledger, ops, queue_path, reason="release transient QA queue entry"
            )
        lease_path = _lane_path(ledger, ops)
        current = _lease(ledger, ops)
        if current and current["dispatch_id"] == dispatch["dispatch_id"]:
            _delete_record(ledger, ops, lease_path, reason="release transient QA lease")
        batch = ops._load_batch(root, dispatch["batch_id"])
        entry = next(
            item
            for item in batch["dispatches"]
            if item["dispatch_id"] == dispatch["dispatch_id"]
        )
        entry["state"] = "approved"
        ops._safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, ops, BatchRecord.from_dict(batch))
        ops._safe_id(dispatch["dispatch_id"], "dispatch")
        _replace_record(
            ledger,
            ops,
            DispatchStatusRecord.from_dict(
                {
                    "dispatch_id": dispatch["dispatch_id"],
                    "state": "approved",
                    "updated_at": ops._now(),
                }
            ),
        )


def run(args: argparse.Namespace, ops: CoordinatorOps) -> JsonObject:
    repo = ops._repo(args)
    root = _state_root(args, repo, ops)
    lease_seconds = args.lease_seconds
    if (
        isinstance(lease_seconds, bool)
        or not isinstance(lease_seconds, int)
        or lease_seconds < 1
    ):
        raise ops.CoordinatorError(
            "QA lease-seconds must be a positive integer",
            remedy="pass --lease-seconds as an integer >= 1",
        )
    ledger = LifecycleLedger(root)
    with _lock(ledger, ops):
        dispatch = ops._load_dispatch(root, args.dispatch)
        batch = ops._load_batch(root, dispatch["batch_id"])
        ops._validate_batch_integrity(root, batch)
        ops._validate_dispatch(repo, ops._config(repo), root, batch, dispatch)
        if dispatch["role"] != "qa":
            raise ops.CoordinatorError(
                "clean-room QA runner accepts only QA dispatches",
                remedy="pass --dispatch the ID of a dispatch whose role is qa",
            )
        if not dispatch["verification_commands"]:
            raise ops.CoordinatorError(
                "clean-room QA runner requires configured verification_commands",
                remedy="configure verification_commands for the QA role in the project's orchestration config and create a new dispatch",
            )
        status = ops._load_dispatch_status(root, dispatch["dispatch_id"])
        entry = next(
            (
                item
                for item in batch.get("dispatches", [])
                if item["dispatch_id"] == dispatch["dispatch_id"]
            ),
            None,
        )
        if (
            not entry
            or entry.get("state") != "approved"
            or status.get("state") != "approved"
        ):
            raise ops.CoordinatorError(
                "QA runner requires an approved, unsent dispatch",
                remedy="approve the QA dispatch and run the QA runner before sending it to any agent",
            )
        queue_path, _ = _enqueue(ledger, dispatch["dispatch_id"], ops)
        queue = _queue_entries(ledger, ops)
        position = next(
            index
            for index, (_, item) in enumerate(queue, start=1)
            if item["dispatch_id"] == dispatch["dispatch_id"]
        )
        lease = _lease(ledger, ops)
        if lease is not None:
            if _lease_expired(lease, ops):
                raise ops.CoordinatorError(
                    "QA lease is stale; a coordinator must clear it explicitly before another gate runs",
                    remedy="have a coordinator run 'qa clear-stale-lease' for the expired lease, then run the QA runner again",
                )
            return {
                "dispatch_id": dispatch["dispatch_id"],
                "state": "queued",
                "position": position,
            }
        if position != 1:
            return {
                "dispatch_id": dispatch["dispatch_id"],
                "state": "queued",
                "position": position,
            }
        lease = {
            "dispatch_id": dispatch["dispatch_id"],
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "acquired_at": ops._now(),
            "expires_at": (
                datetime.now(UTC) + timedelta(seconds=lease_seconds)
            ).isoformat(),
        }
        _write_immutable(ledger, ops, _lane_path(ledger, ops), lease)
        entry["state"] = "dispatched"
        ops._safe_id(dispatch["dispatch_id"], "dispatch")
        _replace_record(
            ledger,
            ops,
            DispatchStatusRecord.from_dict(
                {
                    "dispatch_id": dispatch["dispatch_id"],
                    "state": "working",
                    "updated_at": ops._now(),
                }
            ),
        )
        ops._safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, ops, BatchRecord.from_dict(batch))
    try:
        # The first failed deterministic gate is sufficient evidence for a developer retry.  Do
        # not consume CI time and coordinator context collecting unrelated failures afterwards.
        gate = run_gate(
            dispatch["verification_commands"],
            CleanRoomPolicy(repo, dispatch["candidate_commit"]),
            stop_on_failure=True,
        )
    except GateRunnerError as exc:
        failure = ops.CoordinatorError(exc.message, remedy=exc.remedy)
        _recover_transient_failure(
            ledger, root, dispatch, queue_path, failure, "gate-run", ops
        )
        raise failure from exc
    artifact_text, checks = gate.artifact, gate.checks
    checksum = hashlib.sha256(artifact_text.encode("utf-8")).hexdigest()
    artifact = _artifact_path(ledger, checksum, ops)
    try:
        _write_artifact(ledger, ops, artifact, artifact_text)
    except ops.CoordinatorError as exc:
        failure = ops.CoordinatorError(
            "could not persist immutable QA evidence",
            remedy="resolve the underlying ledger error reported as its cause and run the QA runner again",
        )
        _recover_transient_failure(
            ledger, root, dispatch, queue_path, failure, "artifact-persistence", ops
        )
        raise failure from exc
    report = _qa_report(dispatch, checks, artifact, checksum)
    try:
        with _lock(ledger, ops):
            report_path = _record_report(ledger, root, repo, dispatch, report, ops)
    except ops.CoordinatorError as exc:
        _recover_transient_failure(
            ledger, root, dispatch, queue_path, exc, "report-persistence", ops
        )
        raise
    with _lock(ledger, ops):
        _queue_entries(ledger, ops)
        if queue_path.exists():
            _delete_record(ledger, ops, queue_path, reason="complete QA queue entry")
        lease_path = _lane_path(ledger, ops)
        current = _lease(ledger, ops)
        if current and current["dispatch_id"] == dispatch["dispatch_id"]:
            _delete_record(ledger, ops, lease_path, reason="complete QA lease")
    return {
        "dispatch_id": dispatch["dispatch_id"],
        "state": "reported",
        "report": str(report_path),
        "artifact": str(artifact),
        "sha256": checksum,
    }


def status(args: argparse.Namespace, ops: CoordinatorOps) -> JsonObject:
    repo = ops._repo(args)
    root = _state_root(args, repo, ops)
    ledger = LifecycleLedger(root)
    with _lock(ledger, ops):
        queue, lease = _queue_entries(ledger, ops), _lease(ledger, ops)
        return {
            "lease": lease,
            "lease_stale": _lease_expired(lease, ops) if lease else False,
            "queue": [entry for _, entry in queue],
        }


def clear_stale_lease(args: argparse.Namespace, ops: CoordinatorOps) -> JsonObject:
    repo = ops._repo(args)
    root = _state_root(args, repo, ops)
    ledger = LifecycleLedger(root)
    with _lock(ledger, ops):
        lease = _lease(ledger, ops)
        if lease is None:
            raise ops.CoordinatorError(
                "there is no QA lease to clear",
                remedy="run 'qa status' to confirm the lane holds no lease; there is nothing to clear",
            )
        if not _lease_expired(lease, ops):
            raise ops.CoordinatorError(
                "a live QA lease cannot be force-unlocked",
                remedy="wait until the lease expires (see 'qa status'), then run 'qa clear-stale-lease' again",
            )
        expected = {
            "host": args.expected_host,
            "pid": args.expected_pid,
            "expires_at": args.expected_expiry,
        }
        if any(lease[field] != value for field, value in expected.items()):
            raise ops.CoordinatorError(
                "QA lease changed; coordinator must validate the current owner again",
                remedy="run 'qa status' and pass the current lease's host, pid and expiry as --expected-host, --expected-pid and --expected-expiry",
            )
        recovery = {
            "cleared_dispatch_id": lease["dispatch_id"],
            "lease": lease,
            "approval": ops._approval(args),
            "reason": args.reason.strip(),
            "cleared_at": ops._now(),
        }
        _write_immutable(
            ledger,
            ops,
            _records_root(ledger, ops)
            / "qa-lane"
            / "recoveries"
            / f"{uuid.uuid4()}.json",
            recovery,
        )
        owner = next(
            (
                (path, entry)
                for path, entry in _queue_entries(ledger, ops)
                if entry["dispatch_id"] == lease["dispatch_id"]
            ),
            None,
        )
        if owner is not None:
            _delete_record(
                ledger, ops, owner[0], reason="clear stale QA lease queue entry"
            )
        _delete_record(
            ledger, ops, _lane_path(ledger, ops), reason="clear stale QA lease"
        )
    return {"state": "cleared", "dispatch_id": lease["dispatch_id"]}
