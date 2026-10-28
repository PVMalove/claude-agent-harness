"""Repository-scoped clean-room QA lane.

The lane owns queueing, leasing and gate execution.  It constructs its own ``LifecycleLedger`` for
the state root it is given and uses the ledger's own lock/write/replace/delete primitives directly;
its workflow adapter carries only batch validation, loading and report persistence. Queue and
lease operations depend directly on the ledger and core primitives, never the CLI facade.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import socket
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from ..errors import INTERNAL_INVARIANT_REMEDY, HarnessError
from ..storage import storage_path
from . import hook_policy, operation_access, operational_guards, qa_lease, verification_plan
from .core import state_view
from .pinned_configuration import read_snapshot, capture_unaccepted
from .core.config import _execution_policy, _qa_preparation_commands
from .core.constants import (
    QA_NOT_RUN_RESULT,
    QA_OWNER_FIELDS,
    QA_QUEUE_FIELDS,
    QA_LEASE_FIELDS,
    STATE_REL,
    QaOwnerKind,
)
from .core.utils import (
    CoordinatorError,
    _read_object,
    _safe_id,
    _non_empty,
    _now,
    _moment,
    _repo,
)
from ..gate_runner.gate_runner import (
    CleanRoomPolicy,
    EvidenceBundle,
    EvidenceContext,
    GateRunnerError,
    format_evidence_trailer,
    parse_command_log,
    run_gate,
    run_qa_stages,
)
from .contract import JsonObject
from .ledger import (
    BatchRecord,
    DispatchStatusRecord,
    LedgerError,
    LedgerRecordVO,
    LifecycleLedger,
)
from .ledger.ledger_ops import (
    LedgerBusyError,
    _ledger_lock,
    persist_writes,
    read_state,
)


class QaWorkflowOps(Protocol):
    """Batch QA workflow adapter; queue and lease operations require no adapter."""

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


def _state_root(args: argparse.Namespace, repo: Path) -> Path:
    if getattr(args, "state_dir", None):
        raise CoordinatorError(
            "QA lane is repository-scoped and does not support --state-dir",
            remedy="run the QA lane without --state-dir; it always uses the repository's .harness/orchestration/state",
        )
    return repo / STATE_REL


@contextmanager
def _ledger_errors() -> Iterator[None]:
    """Translate ``LedgerError`` to ``CoordinatorError`` at this module's ledger call sites.

    ``ledger.py`` deliberately never imports from ``coordinator.py``, and coordinator's CLI boundary
    only catches ``CoordinatorError``, so every direct ledger call this module makes must translate
    here (mirrors coordinator.py's own ``_ledger_lock``)."""
    try:
        yield
    except LedgerError as exc:
        raise CoordinatorError(exc.message, remedy=exc.remedy) from exc


@contextmanager
def _lock(ledger: LifecycleLedger) -> Iterator[None]:
    """Exclusive ledger lock, or in the runtime-store epoch the store unit of the command; a
    ``LedgerError`` inside it becomes ``CoordinatorError``."""
    with _ledger_lock(ledger):
        yield


# How long a lane release after ``qa run`` waits for a busy ledger, and how often it retries.
RELEASE_LOCK_WAIT_SECONDS = 30.0
RELEASE_LOCK_POLL_SECONDS = 0.25


@contextmanager
def _lock_waiting(ledger: LifecycleLedger, dispatch_id: str) -> Iterator[None]:
    """Exclusive ledger lock for releasing this dispatch's lane, waiting a bounded time for it.

    A release that gave up on the first busy answer would leave the lease and the queue entry
    held until the lease expires and every later QA run waiting; a lock still busy at the
    deadline names the lane state left behind."""
    deadline = time.monotonic() + RELEASE_LOCK_WAIT_SECONDS
    while True:
        entered = False
        try:
            with _ledger_lock(ledger):
                entered = True
                yield
            return
        except LedgerBusyError as exc:
            if entered:
                raise
            if time.monotonic() >= deadline:
                raise _lane_not_released(ledger, dispatch_id) from exc
            time.sleep(RELEASE_LOCK_POLL_SECONDS)


def _lane_not_released(ledger: LifecycleLedger, dispatch_id: str) -> CoordinatorError:
    lease = _lease(ledger)
    if lease:
        kind, identity = _owner(lease)
        lane = f"the QA lease belongs to {kind} {identity} and expires at {lease['expires_at']}"
    else:
        lane = "the QA lane holds no lease"
    return CoordinatorError(
        f"QA dispatch {dispatch_id} was not released: the ledger stayed locked for "
        f"{RELEASE_LOCK_WAIT_SECONDS:g} seconds; {lane}, and the dispatch stays working",
        remedy="run 'coordinator.py ledger lock-status' to find the operation holding the ledger; "
        "after the lease expires, 'qa clear-stale-lease' clears the lease and its queue entry "
        "and returns the dispatch to approved; then run 'qa run' for it again",
    )


def _records_root(ledger: LifecycleLedger) -> Path:
    with _ledger_errors():
        return ledger.records_root()


def _write_immutable(ledger: LifecycleLedger, path: Path, value: JsonObject) -> None:
    with _ledger_errors():
        ledger.write_immutable(path, value)


def _write_artifact(ledger: LifecycleLedger, path: Path, value: str) -> None:
    with _ledger_errors():
        ledger.write_artifact(path, value)


def _replace_path(ledger: LifecycleLedger, path: Path, value: JsonObject) -> None:
    with _ledger_errors():
        ledger.replace(path, value)


def _replace_record(ledger: LifecycleLedger, record: LedgerRecordVO) -> None:
    with _ledger_errors():
        ledger.replace_record(record)


def _delete_record(ledger: LifecycleLedger, path: Path, *, reason: str) -> None:
    with _ledger_errors():
        ledger.delete(path, reason=reason)


def _lane_path(ledger: LifecycleLedger) -> Path:
    return _records_root(ledger) / "qa-lane" / "lease.json"


def _queue_root(ledger: LifecycleLedger) -> Path:
    return _records_root(ledger) / "qa-lane" / "queue"


def _counter_path(ledger: LifecycleLedger) -> Path:
    return _records_root(ledger) / "qa-lane" / "sequence.json"


def _artifact_path(ledger: LifecycleLedger, checksum: str) -> Path:
    return _records_root(ledger) / "qa-artifacts" / f"{checksum}.log"


def evidence_context(
    repo: Path, candidate: str, dispatch: JsonObject | None = None
) -> EvidenceContext:
    """The identity a QA run's stage evidence carries: candidate, plan, image and environment.

    A dispatch brief supplies its verification plan and pinned images; without one (a local QA
    run) only the candidate and the environment fingerprint of that commit are known.
    """
    brief = dispatch or {}
    plan = brief.get("verification_plan")
    images = brief.get("runtime_images")
    fingerprint: str | None = None
    if isinstance(plan, dict) and plan.get("stages"):
        fingerprint = plan["stages"][0]["environment_fingerprint"]
    else:
        try:
            fingerprint = verification_plan.environment_fingerprint(
                repo, candidate, images if isinstance(images, dict) else None
            )
        except HarnessError:
            # The fingerprint is optional evidence: an unreadable manifest must not stop QA.
            fingerprint = None
    return EvidenceContext(
        candidate_sha=candidate,
        plan_sha256=verification_plan.plan_sha256(plan)
        if isinstance(plan, dict)
        else None,
        image_digest=operational_guards.runtime_images_digest(images)
        if isinstance(images, dict)
        else None,
        environment_fingerprint=fingerprint,
    )


def persist_stage_evidence(ledger: LifecycleLedger, bundle: EvidenceBundle) -> str:
    """Store the run's output streams and its stage-evidence document as immutable qa-artifacts.

    Returns the trailer line that links the QA log to the document: the log, the document and the
    streams then form one chain of sha256 references.
    """
    root = _records_root(ledger)
    for relative, text in bundle.files.items():
        _write_artifact(ledger, root / relative, text)
    document = bundle.document.dumps()
    checksum = hashlib.sha256(document.encode("utf-8")).hexdigest()
    relative = f"qa-artifacts/{checksum}.stage-evidence.json"
    _write_artifact(ledger, root / relative, document)
    return format_evidence_trailer(relative, checksum)


def _attempt_path(ledger: LifecycleLedger) -> Path:
    return _records_root(ledger) / "qa-lane" / "attempts" / f"{uuid.uuid4()}.json"


def _queue_entries(ledger: LifecycleLedger) -> list[tuple[Path, JsonObject]]:
    entries: list[tuple[Path, JsonObject]] = []
    for path in state_view.glob(_queue_root(ledger), "*.json"):
        entry = _read_object(path, "QA queue entry")
        if (
            not _valid_owner_schema(entry, QA_QUEUE_FIELDS)
            or not isinstance(entry["sequence"], int)
            or entry["sequence"] < 1
        ):
            raise CoordinatorError(
                "QA queue entry has an invalid schema",
                remedy=f"fix {path}: it must hold exactly dispatch_id, sequence (an integer >= 1) and queued_at",
            )
        _validate_owner(entry)
        if not _non_empty(entry["queued_at"]):
            raise CoordinatorError(
                "QA queue entry has an invalid queued_at value",
                remedy=f"set a non-empty queued_at in {path}",
            )
        entries.append((path, entry))
    return sorted(entries, key=lambda item: item[1]["sequence"])


def _owner(value: JsonObject) -> tuple[str, str]:
    """Normalize legacy dispatch records without rewriting their history."""
    return (
        str(value.get("owner_kind", QaOwnerKind.DISPATCH)),
        str(value.get("owner_id", value.get("dispatch_id"))),
    )


def _owner_document(kind: str, identity: str) -> JsonObject:
    return (
        {"dispatch_id": identity}
        if kind == QaOwnerKind.DISPATCH
        else {"owner_kind": kind, "owner_id": identity}
    )


def _valid_owner_schema(value: JsonObject, legacy_fields: set[str]) -> bool:
    return (
        set(value) == legacy_fields
        or set(value) == (legacy_fields - {"dispatch_id"}) | QA_OWNER_FIELDS
    )


def _validate_owner(value: JsonObject) -> None:
    kind, identity = _owner(value)
    if kind not in tuple(QaOwnerKind):
        raise CoordinatorError(
            "QA owner kind is invalid", remedy="use a dispatch or local-qa owner"
        )
    if kind == QaOwnerKind.DISPATCH:
        _safe_id(identity, "QA owner")
    elif re.fullmatch(r"local-qa-[0-9a-f]{32}", identity) is None:
        raise CoordinatorError(
            "local QA owner ID is invalid",
            remedy="use the request_id returned by integration local-qa",
        )


def _require_lease_seconds(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CoordinatorError(
            "QA lease-seconds must be a positive integer",
            remedy="pass --lease-seconds as an integer >= 1",
        )
    return value


def _batch_entry(batch: JsonObject, dispatch_id: str) -> JsonObject | None:
    """The batch's entry for one dispatch, or ``None`` when the batch does not list it."""
    return next(
        (
            item
            for item in batch.get("dispatches", [])
            if item["dispatch_id"] == dispatch_id
        ),
        None,
    )


def acquire(
    ledger: LifecycleLedger,
    owner_id: str,
    *,
    owner_kind: str = QaOwnerKind.DISPATCH,
    lease_seconds: int,
) -> JsonObject:
    """Enqueue and acquire the one FIFO lane. Caller holds the short ledger lock."""
    _require_lease_seconds(lease_seconds)
    _validate_owner(_owner_document(owner_kind, owner_id))
    queue_path, _ = _enqueue(ledger, owner_id, owner_kind=owner_kind)
    queue = _queue_entries(ledger)
    position = next(
        index
        for index, (_, entry) in enumerate(queue, start=1)
        if _owner(entry) == (owner_kind, owner_id)
    )
    lease = _lease(ledger)
    if qa_lease.active(ledger):
        # The runtime owns the lease: an expired one was reaped before the admission, if its
        # owner was provably dead, and the runtime grants the lane only while it is free.
        if lease or position != 1:
            return {"state": "queued", "position": position}
        return {
            "state": "acquired",
            "position": position,
            "lease": qa_lease.acquire(
                ledger, owner_id, owner_kind=owner_kind, lease_seconds=lease_seconds
            ),
            "queue_path": str(queue_path),
        }
    if lease and _lease_expired(lease):
        raise CoordinatorError(
            "QA lease is stale; a coordinator must clear it explicitly before another gate runs",
            remedy="have a coordinator run 'qa clear-stale-lease' for the expired lease, then run the QA runner again",
        )
    if lease or position != 1:
        return {"state": "queued", "position": position}
    lease = {
        **_owner_document(owner_kind, owner_id),
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "acquired_at": _now(),
        "expires_at": (
            datetime.now(UTC) + timedelta(seconds=lease_seconds)
        ).isoformat(),
    }
    _write_immutable(ledger, _lane_path(ledger), lease)
    return {
        "state": "acquired",
        "position": position,
        "lease": lease,
        "queue_path": str(queue_path),
    }


def release(ledger: LifecycleLedger, claimed: JsonObject) -> None:
    """Release exactly the acquired lease and its queue entry. Caller holds the lock."""
    if qa_lease.active(ledger):
        # The runtime releases the lease only for its owner and fencing token.
        for path, entry in _queue_entries(ledger):
            if _owner(entry) == _owner(claimed):
                _delete_record(ledger, path, reason="complete QA queue entry")
        qa_lease.release(ledger, claimed, reason="complete QA lease")
        return
    current = _lease(ledger)
    if current != claimed:
        raise CoordinatorError(
            "QA lease owner changed; refusing release",
            remedy="inspect 'qa status' and retain the current owner's lease",
        )
    for path, entry in _queue_entries(ledger):
        if _owner(entry) == _owner(claimed):
            _delete_record(ledger, path, reason="complete QA queue entry")
    _delete_record(ledger, _lane_path(ledger), reason="complete QA lease")


def withdraw(ledger: LifecycleLedger, owner_id: str, *, owner_kind: str) -> None:
    """Remove a waiting request whose pair became unusable; never remove a lease."""
    current = _lease(ledger)
    if current and _owner(current) == (owner_kind, owner_id):
        return
    for path, entry in _queue_entries(ledger):
        if _owner(entry) == (owner_kind, owner_id):
            _delete_record(ledger, path, reason="withdraw unavailable QA request")


def running_owner(
    ledger: LifecycleLedger, owner_id: str, *, owner_kind: str
) -> JsonObject | None:
    """Observe an already running owner without changing its admission or retry budget."""
    current = _lease(ledger)
    if not current or _owner(current) != (owner_kind, owner_id):
        return None
    # In the store epoch the runtime decides whether an expired lease's owner lives.
    if not qa_lease.active(ledger) and _lease_expired(current):
        raise CoordinatorError(
            "QA lease is stale; a coordinator must clear it explicitly before another gate runs",
            remedy="have a coordinator run 'qa clear-stale-lease' for the expired lease",
        )
    position = next(
        (
            index
            for index, (_, entry) in enumerate(_queue_entries(ledger), start=1)
            if _owner(entry) == (owner_kind, owner_id)
        ),
        None,
    )
    if position is None:
        # A release that failed between its queue and lease deletions leaves an orphan lease.
        raise CoordinatorError(
            "QA lease has no queue entry for its owner",
            remedy="run 'qa status'; after the lease expires, have a coordinator run 'qa clear-stale-lease'",
        )
    return {"state": "queued", "position": position, "running": True}


def _enqueue(
    ledger: LifecycleLedger,
    dispatch_id: str,
    *,
    owner_kind: str = QaOwnerKind.DISPATCH,
) -> tuple[Path, JsonObject]:
    for path, entry in _queue_entries(ledger):
        if _owner(entry) == (owner_kind, dispatch_id):
            return path, entry
    counter_path = _counter_path(ledger)
    counter = (
        _read_object(counter_path, "QA queue sequence")
        if state_view.exists(counter_path)
        else {"next": 1}
    )
    if (
        set(counter) != {"next"}
        or not isinstance(counter["next"], int)
        or counter["next"] < 1
    ):
        raise CoordinatorError(
            "QA queue sequence is invalid",
            remedy=f"fix {counter_path}: it must be a JSON object holding only an integer 'next' >= 1",
        )
    entry = {
        **_owner_document(owner_kind, dispatch_id),
        "sequence": counter["next"],
        "queued_at": _now(),
    }
    path = _queue_root(ledger) / f"{entry['sequence']:020d}-{dispatch_id}.json"
    _write_immutable(ledger, path, entry)
    next_counter = {"next": counter["next"] + 1}
    if state_view.exists(counter_path):
        _replace_path(ledger, counter_path, next_counter)
    else:
        # The first queue entry in a fresh ledger has no mutable counter yet.  Seed it as an
        # immutable record; subsequent enqueues may use the ledger transition primitive.
        _write_immutable(ledger, counter_path, next_counter)
    return path, entry


def release_queue(ledger: LifecycleLedger, dispatch_ids: list[str]) -> list[str]:
    """Drop the queue entries of dispatches that will never run, so they cannot hold up the lane.

    Only queue entries go: a lease is left to ``clear_stale_lease``, which refuses a live one.
    """
    released = []
    for path, entry in _queue_entries(ledger):
        kind, identity = _owner(entry)
        if kind == QaOwnerKind.DISPATCH and identity in dispatch_ids:
            _delete_record(ledger, path, reason="release abandoned QA queue entry")
            released.append(identity)
    return released


def _lease(ledger: LifecycleLedger) -> JsonObject | None:
    path = _lane_path(ledger)
    if not state_view.exists(path):
        return None
    lease = _read_object(path, "QA lease")
    if qa_lease.active(ledger):
        # The runtime wrote the lease; the coordinator shows it in the shape of the file lane.
        return qa_lease.normalize(lease)
    if (
        not _valid_owner_schema(lease, QA_LEASE_FIELDS)
        or not _non_empty(lease.get("host"))
        or not isinstance(lease.get("pid"), int)
    ):
        raise CoordinatorError(
            "QA lease has an invalid schema",
            remedy=f"fix {path}: it must hold exactly dispatch_id, host, pid (an integer), acquired_at and expires_at",
        )
    _validate_owner(lease)
    for field in ("acquired_at", "expires_at"):
        if not _non_empty(lease.get(field)):
            raise CoordinatorError(
                f"QA lease has an invalid {field}",
                remedy=f"set a non-empty {field} in {path}",
            )
    return lease


def lane_summary(ledger: LifecycleLedger) -> JsonObject | None:
    """Who holds the QA lane and whether its owner missed its heartbeat, for ``dispatch status``;
    ``None`` for a free lane. The caller reads the state."""
    return qa_lease.summary(_lease(ledger))


def _lease_expired(lease: JsonObject) -> bool:
    return _moment(lease["expires_at"], "QA lease expiry") <= datetime.now(UTC)


def qa_evidence(args: argparse.Namespace, ops: QaWorkflowOps) -> JsonObject:
    """Verify accepted green QA evidence for one current issue-branch candidate."""
    repo = ops._repo(args)
    root = _state_root(args, repo)
    ticket = args.ticket.strip() if _non_empty(args.ticket) else ""
    branch = args.branch.strip() if _non_empty(args.branch) else ""
    if not ticket or not branch:
        raise CoordinatorError(
            "QA evidence requires non-empty ticket and branch",
            remedy="pass non-empty --ticket and --branch",
        )
    candidate = ops._candidate_commit(repo, args.candidate_commit)
    ledger = LifecycleLedger(root)
    with _lock(ledger):
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
    dispatch: JsonObject,
    checks: list[dict[str, str]],
    artifact: Path,
    checksum: str,
    stages: JsonObject | None = None,
) -> JsonObject:
    """The QA completion report.

    ``checks_run`` always lists every approved command: one the run never reached is ``not_run``.
    Without ``stages`` (a verification-only configuration) a failed gate command fails the report.
    With them, the failing stage decides the outcome: a failed gate or a confirmed project-file defect is a
    ``failed`` report for the developer; an infrastructure or unknown preparation failure is a
    ``blocked`` one, whose ``checks_run`` records the commands never reached as ``not_run`` rather
    than as failed code checks.
    """
    failed = any(check["result"] == "fail" for check in checks)
    reference = f"full sanitised output: {artifact.as_posix()} (sha256:{checksum})"
    report: JsonObject = {
        "dispatch_id": dispatch["dispatch_id"],
        "ticket": dispatch["ticket"],
        "role": "qa",
        "outcome": "failed" if failed else "completed",
        "output": f"QA gate {'failed' if failed else 'passed'}; {reference}",
        "commit_sha": "not applicable — read-only role",
        "changed_files": [],
        "checks_run": checks,
        "risks": {
            "status": "present",
            "details": "QA gate failed; inspect immutable evidence",
        }
        if failed
        else {"status": "none", "details": ""},
        "blockers": {
            "status": "present",
            "details": "new approved developer retry required",
        }
        if failed
        else {"status": "none", "details": ""},
        "next_coordinator_action": "create a new approved developer retry"
        if failed
        else "accept or continue",
        "report_language": "ru",
    }
    # A command the run never reached is not_run, with or without a preparation stage: the report
    # must list every approved command (ADR 0023, section 5).
    report["checks_run"] = [
        *checks,
        *(
            {
                "command": command,
                "result": QA_NOT_RUN_RESULT,
                "evidence": "not run: an earlier QA stage failed before this command",
            }
            for command in dispatch["verification_commands"][len(checks) :]
        ),
    ]
    if stages is None:
        return report
    report["qa_stages"] = stages
    if stages["failed_stage"] != "preparation":
        return report
    broken = next(stage for stage in stages["stages"] if stage["result"] == "fail")
    diagnosis = stages["diagnosis"]
    category = diagnosis["category"]
    report["output"] = (
        f"QA environment preparation failed at `{broken['command']}` "
        f"(exit {broken['exit_code']}, cause: {category}); code was not verified, "
        f"no code check started; {reference}"
    )
    if category == "project-defect":
        report.update(
            outcome="failed",
            risks={
                "status": "present",
                "details": "the candidate's project files break environment preparation; code was not verified",
            },
            blockers={
                "status": "present",
                "details": "new approved developer retry required to fix the project-file defect",
            },
            next_coordinator_action="create a new approved developer retry",
        )
    elif category == "infrastructure":
        report.update(
            outcome="blocked",
            risks={
                "status": "present",
                "details": "infrastructure failure before code checks; the candidate code was not verified",
            },
            blockers={
                "status": "present",
                "details": "QA infrastructure failure; no developer retry is needed",
            },
            next_coordinator_action="restore the QA environment, then decide a same-SHA QA retry",
        )
    else:
        report.update(
            outcome="blocked",
            risks={
                "status": "present",
                "details": "cause of the preparation failure is unknown; the candidate code was not verified",
            },
            blockers={
                "status": "present",
                "details": "coordinator triage required; no automatic retry",
            },
            next_coordinator_action="triage the preparation failure, then decide the retry route",
        )
    return report


def _verify_stages_against_artifact(
    stages: JsonObject, artifact: Path, checksum: str
) -> None:
    """Prove the recorded stages against the persisted immutable artifact, read back from disk.

    The artifact must still hash to its recorded checksum, and its command blocks must be exactly
    the stage entries' executed commands and exit codes, in order.
    """
    try:
        data = artifact.read_bytes()
    except OSError as exc:
        raise CoordinatorError(
            f"could not read back the immutable QA evidence {artifact}: {exc}",
            remedy="restore access to the ledger's QA evidence and run the QA runner again",
        ) from exc
    if hashlib.sha256(data).hexdigest() != checksum:
        raise CoordinatorError(
            "immutable QA evidence does not match its recorded checksum",
            remedy="the persisted evidence changed after it was written; run the QA runner again",
        )
    logged = parse_command_log(data.decode("utf-8").splitlines())
    recorded = [
        (stage.get("executed_command", stage["command"]), stage["exit_code"])
        for stage in stages["stages"]
    ]
    if logged != recorded:
        raise CoordinatorError(
            "the QA stage record does not match the immutable evidence",
            remedy="the stage commands and exit codes must equal the artifact's command blocks; run the QA runner again",
        )


def _record_report(
    ledger: LifecycleLedger,
    root: Path,
    repo: Path,
    dispatch: JsonObject,
    report: JsonObject,
    ops: QaWorkflowOps,
) -> Path:
    batch = ops._load_batch(root, dispatch["batch_id"])
    try:
        ops._validate_batch_integrity(root, batch)
        policy = read_snapshot(dispatch)
        ops._validate_dispatch(repo, policy["config"], root, batch, dispatch)
    except CoordinatorError as exc:
        path = capture_unaccepted(root, dispatch, report, exc)
        raise CoordinatorError(
            exc.message,
            remedy=f"QA evidence captured without acceptance at {path}; {exc.remedy}",
        ) from exc
    status = ops._load_dispatch_status(root, dispatch["dispatch_id"])
    entry = _batch_entry(batch, dispatch["dispatch_id"])
    if (
        not entry
        or entry.get("state") != "dispatched"
        or status.get("state") != "working"
    ):
        raise CoordinatorError(
            "QA report requires a running QA dispatch",
            remedy="the batch entry must be dispatched and the dispatch status working; if they are out of sync, abandon the batch and create a new QA dispatch",
        )
    # QA is a read-only role: its report lists no changed files, so this base measures nothing.
    ops._validate_report(
        report,
        dispatch,
        policy["role"],
        repo,
        batch.get("integration_base_commit") or batch.get("base_commit"),
    )
    return ops._persist_report(ledger, root, batch, dispatch, report)


def _recover_transient_failure(
    ledger: LifecycleLedger,
    root: Path,
    dispatch: JsonObject,
    queue_path: Path,
    lease: JsonObject,
    failure: HarnessError,
    stage: str,
    ops: QaWorkflowOps,
) -> None:
    """Return one QA dispatch to its retryable state after a non-terminal failure."""
    with _lock_waiting(ledger, dispatch["dispatch_id"]):
        _write_immutable(
            ledger,
            _attempt_path(ledger),
            {
                "dispatch_id": dispatch["dispatch_id"],
                "stage": stage,
                "failed_at": _now(),
                "message": failure.message,
                "remedy": failure.remedy,
            },
        )
        if state_view.exists(queue_path):
            _delete_record(
                ledger, queue_path, reason="release transient QA queue entry"
            )
        lease_path = _lane_path(ledger)
        current = _lease(ledger)
        # An expired lease may have been cleared and the lane taken by another owner, including a
        # local QA run without a dispatch_id; only this dispatch's own lease is released.
        if current and _owner(current) == (
            QaOwnerKind.DISPATCH,
            dispatch["dispatch_id"],
        ):
            mine = (
                current.get("fencing_token") == lease.get("fencing_token")
                if qa_lease.active(ledger)
                else current == lease
            )
            if not mine:
                # A newer run of this dispatch took the lane after its lease was cleared; that run
                # owns the lease and the dispatch state.
                return
            if qa_lease.active(ledger):
                qa_lease.release(ledger, lease, reason="release transient QA lease")
            else:
                _delete_record(ledger, lease_path, reason="release transient QA lease")
        _restore_approved(ledger, root, dispatch, ops)


def _restore_approved(
    ledger: LifecycleLedger, root: Path, dispatch: JsonObject, ops: QaWorkflowOps
) -> None:
    """Return a QA dispatch whose run ended without a report to ``approved``. Caller holds the lock."""
    batch = ops._load_batch(root, dispatch["batch_id"])
    entry = next(
        item
        for item in batch["dispatches"]
        if item["dispatch_id"] == dispatch["dispatch_id"]
    )
    entry["state"] = "approved"
    _safe_id(batch["batch_id"], "batch")
    _replace_record(ledger, BatchRecord.from_dict(batch))
    _safe_id(dispatch["dispatch_id"], "dispatch")
    _replace_record(
        ledger,
        DispatchStatusRecord.from_dict(
            {
                "dispatch_id": dispatch["dispatch_id"],
                "state": "approved",
                "updated_at": _now(),
            }
        ),
    )


def _running_dispatch(
    root: Path, ledger: LifecycleLedger, owner: tuple[str, str], ops: QaWorkflowOps
) -> JsonObject | None:
    """The cleared lease's dispatch when its run left it running, or ``None``.

    A run that could not release its lane leaves its dispatch ``working`` without a report, and
    the QA runner refuses it until it is ``approved`` again. Another state, a local QA lease and
    a dispatch without records are left as they are."""
    kind, dispatch_id = owner
    if kind != "dispatch":
        return None
    status_path = (
        _records_root(ledger)
        / DispatchStatusRecord.directory
        / f"{_safe_id(dispatch_id, 'dispatch')}.json"
    )
    if not state_view.exists(status_path):
        return None
    dispatch = ops._load_dispatch(root, dispatch_id)
    entry = _batch_entry(ops._load_batch(root, dispatch["batch_id"]), dispatch_id)
    status = ops._load_dispatch_status(root, dispatch_id)
    if (
        not entry
        or entry.get("state") != "dispatched"
        or status.get("state") != "working"
    ):
        return None
    return dispatch


def _release_after_failure(
    ledger: LifecycleLedger,
    root: Path,
    dispatch: JsonObject,
    queue_path: Path | None,
    lease: JsonObject,
    exc: BaseException,
    stage: str,
    ops: QaWorkflowOps,
) -> None:
    """Undo an acquired lane after any failure and raise a coordinator error for an unexpected one.

    A held lease, a queue entry or a ``dispatched`` state that outlives a failed run would block
    every later QA run until a human clears it. A harness error keeps its own message and remedy.
    """
    if queue_path is None:
        return
    if isinstance(exc, HarnessError):
        failure: HarnessError = exc
    elif isinstance(exc, Exception):
        failure = CoordinatorError(
            f"QA run failed unexpectedly at {stage}: {exc}",
            remedy="inspect the cause above; the QA lane and the dispatch were released, so run the QA runner again",
        )
    else:
        failure = CoordinatorError(
            f"QA run was interrupted at {stage}",
            remedy="the QA lane and the dispatch were released; run the QA runner again",
        )
    _recover_transient_failure(
        ledger, root, dispatch, queue_path, lease, failure, stage, ops
    )
    if failure is not exc and isinstance(exc, Exception):
        raise failure from exc


def _verify_access(
    ledger: LifecycleLedger,
    repo: Path,
    dispatch: JsonObject,
    ops: QaWorkflowOps,
) -> None:
    """Prove the QA operation's access before the lane is acquired; record a refusal as an attempt.

    A refusal changes nothing else: there is no queue entry or lease to undo, the dispatch stays
    approved, and every earlier attempt and artifact is untouched.
    """
    try:
        operation_access.require(
            repo,
            ops._config(repo),
            "qa",
            brief=dispatch,
            checkout=storage_path(repo, "runs", "qa"),
        )
    except operation_access.OperationAccessError as exc:
        _write_immutable(
            ledger,
            _attempt_path(ledger),
            {
                "dispatch_id": dispatch["dispatch_id"],
                "stage": "access",
                "failed_at": _now(),
                "message": exc.message,
                "remedy": exc.remedy,
                "evidence": exc.evidence,
            },
        )
        # The attempt outlives the refusal that follows, as it did on the file ledger.
        persist_writes(ledger)
        raise


def run(args: argparse.Namespace, ops: QaWorkflowOps) -> JsonObject:
    repo = ops._repo(args)
    root = _state_root(args, repo)
    lease_seconds = _require_lease_seconds(
        args.lease_seconds
        if args.lease_seconds is not None
        else _execution_policy(ops._config(repo))["qa_lease_seconds"],
    )
    ledger = LifecycleLedger(root)
    held: BaseException | None = None
    queue_path: Path | None = None
    reap_dead_owner(ledger, root, ops)
    with _lock(ledger):
        dispatch = ops._load_dispatch(root, args.dispatch)
        batch = ops._load_batch(root, dispatch["batch_id"])
        ops._validate_batch_integrity(root, batch)
        ops._validate_dispatch(repo, ops._config(repo), root, batch, dispatch)
        if dispatch["role"] != "qa":
            raise CoordinatorError(
                "clean-room QA runner accepts only QA dispatches",
                remedy="pass --dispatch the ID of a dispatch whose role is qa",
            )
        if not dispatch["verification_commands"]:
            raise CoordinatorError(
                "clean-room QA runner requires configured verification_commands",
                remedy="configure verification_commands for the QA role in the project's orchestration config and create a new dispatch",
            )
        status = ops._load_dispatch_status(root, dispatch["dispatch_id"])
        entry = _batch_entry(batch, dispatch["dispatch_id"])
        if (
            not entry
            or entry.get("state") != "approved"
            or status.get("state") != "approved"
        ):
            raise CoordinatorError(
                "QA runner requires an approved, unsent dispatch",
                remedy="approve the QA dispatch and run the QA runner before sending it to any agent",
            )
        _verify_access(ledger, repo, dispatch, ops)
        # Strict fails closed before the lease, by the policy the brief pinned (ADR 0024, 2a).
        hook_policy.check_strict_boundary(
            repo,
            hook_policy.dispatch_policy(dispatch),
            role="qa",
            boundary="qa run",
            batch=batch,
            candidate=str(dispatch.get("candidate_commit")),
            plan=dispatch.get("verification_plan"),
        )
        admission = acquire(
            ledger, dispatch["dispatch_id"], lease_seconds=lease_seconds
        )
        if admission["state"] == "queued":
            return {"dispatch_id": dispatch["dispatch_id"], **admission}
        queue_path = Path(admission["queue_path"])
        lease = admission["lease"]
        try:
            entry["state"] = "dispatched"
            _safe_id(dispatch["dispatch_id"], "dispatch")
            _replace_record(
                ledger,
                DispatchStatusRecord.from_dict(
                    {
                        "dispatch_id": dispatch["dispatch_id"],
                        "state": "working",
                        "updated_at": _now(),
                    }
                ),
            )
            _safe_id(batch["batch_id"], "batch")
            _replace_record(ledger, BatchRecord.from_dict(batch))
        except BaseException as exc:
            # The lock is still held here; release it first, then undo the lane and the state.
            held = exc
    if held is not None:
        _release_after_failure(
            ledger, root, dispatch, queue_path, lease, held, "state-transition", ops
        )
        raise held
    if queue_path is None:  # an unqueued run returned above
        raise CoordinatorError(
            "QA run reached the gate without an acquired lane",
            remedy=INTERNAL_INVARIANT_REMEDY,
        )
    try:
        # The first failed deterministic gate is sufficient evidence for a developer retry.  Do
        # not consume CI time and coordinator context collecting unrelated failures afterwards.
        policy = CleanRoomPolicy(repo, dispatch["candidate_commit"])
        from .infrastructure_retry import pinned

        config = read_snapshot(dispatch)["config"]
        pinned_commands = pinned(dispatch)
        if pinned_commands is not None:
            config = {**config, **pinned_commands}
        preparation = _qa_preparation_commands(config)
        context = evidence_context(repo, dispatch["candidate_commit"], dispatch)
        if preparation:
            staged = run_qa_stages(
                preparation,
                dispatch["verification_commands"],
                policy,
                environment_probes=_qa_preparation_commands(
                    config, "qa_environment_probes"
                ),
                project_file_checks=_qa_preparation_commands(
                    config, "qa_project_file_checks"
                ),
                evidence=context,
            )
            artifact_text, checks = staged.artifact, staged.gate_checks
            bundle = staged.evidence
        else:
            staged = None
            gate = run_gate(
                dispatch["verification_commands"],
                policy,
                stop_on_failure=True,
                evidence=context,
            )
            artifact_text, checks = gate.artifact, gate.checks
            bundle = gate.evidence
    except GateRunnerError as exc:
        failure = CoordinatorError(exc.message, remedy=exc.remedy)
        _recover_transient_failure(
            ledger, root, dispatch, queue_path, lease, failure, "gate-run", ops
        )
        raise failure from exc
    except BaseException as exc:
        _release_after_failure(
            ledger, root, dispatch, queue_path, lease, exc, "gate-run", ops
        )
        raise
    try:
        if bundle is not None:
            artifact_text += persist_stage_evidence(ledger, bundle)
        checksum = hashlib.sha256(artifact_text.encode("utf-8")).hexdigest()
        artifact = _artifact_path(ledger, checksum)
        _write_artifact(ledger, artifact, artifact_text)
    except CoordinatorError as exc:
        failure = CoordinatorError(
            "could not persist immutable QA evidence",
            remedy="resolve the underlying ledger error reported as its cause and run the QA runner again",
        )
        _recover_transient_failure(
            ledger,
            root,
            dispatch,
            queue_path,
            lease,
            failure,
            "artifact-persistence",
            ops,
        )
        raise failure from exc
    except BaseException as exc:
        _release_after_failure(
            ledger, root, dispatch, queue_path, lease, exc, "artifact-persistence", ops
        )
        raise
    stages = staged.record() if staged is not None else None
    if stages is not None:
        try:
            _verify_stages_against_artifact(stages, artifact, checksum)
        except CoordinatorError as exc:
            _recover_transient_failure(
                ledger,
                root,
                dispatch,
                queue_path,
                lease,
                exc,
                "stage-verification",
                ops,
            )
            raise
        except BaseException as exc:
            _release_after_failure(
                ledger,
                root,
                dispatch,
                queue_path,
                lease,
                exc,
                "stage-verification",
                ops,
            )
            raise
    try:
        report = _qa_report(dispatch, checks, artifact, checksum, stages)
        with _lock(ledger):
            report_path = _record_report(ledger, root, repo, dispatch, report, ops)
    except CoordinatorError as exc:
        _recover_transient_failure(
            ledger, root, dispatch, queue_path, lease, exc, "report-persistence", ops
        )
        raise
    except BaseException as exc:
        _release_after_failure(
            ledger, root, dispatch, queue_path, lease, exc, "report-persistence", ops
        )
        raise
    with _lock_waiting(ledger, dispatch["dispatch_id"]):
        release(ledger, lease)
    return {
        "dispatch_id": dispatch["dispatch_id"],
        "state": "reported",
        "report": str(report_path),
        "artifact": str(artifact),
        "sha256": checksum,
    }


def status(args: argparse.Namespace) -> JsonObject:
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with read_state(ledger, repo):
        queue, lease = _queue_entries(ledger), _lease(ledger)
        stale = _lease_expired(lease) if lease else False
        return {
            "lease": lease,
            "lease_stale": stale,
            # The owner of an expired lease missed its heartbeat. In the store epoch the runtime
            # keeps such a lease while its owner may live, and reaps only a dead owner's lease.
            "stale_heartbeat": stale,
            "queue": [entry for _, entry in queue],
        }


def _recover_cleared_owner(
    ledger: LifecycleLedger,
    root: Path,
    lease: JsonObject,
    ops: QaWorkflowOps,
    *,
    approval: JsonObject | None,
    reason: str,
    verdict: str | None = None,
) -> str | None:
    """The recovery of the owner of a cleared lease: a ``qa-lane/recoveries`` record, its queue
    entry removed and its QA dispatch, if the run left it working, back to ``approved``. Returns
    the restored state. Caller holds the lock."""
    running = _running_dispatch(root, ledger, _owner(lease), ops)
    restored = "approved" if running else None
    recovery: JsonObject = {
        "cleared_dispatch_id": _owner(lease)[1],
        "lease": lease,
        "approval": approval,
        "reason": reason,
        "cleared_at": _now(),
        "restored_dispatch_state": restored,
    }
    if verdict is not None:
        recovery["verdict"] = verdict
    _write_immutable(
        ledger,
        _records_root(ledger) / "qa-lane" / "recoveries" / f"{uuid.uuid4()}.json",
        recovery,
    )
    owner = next(
        (
            (path, entry)
            for path, entry in _queue_entries(ledger)
            if _owner(entry) == _owner(lease)
        ),
        None,
    )
    if owner is not None:
        _delete_record(ledger, owner[0], reason="clear stale QA lease queue entry")
    if running:
        _restore_approved(ledger, root, running, ops)
    return restored


def reap_dead_owner(ledger: LifecycleLedger, root: Path, ops: QaWorkflowOps) -> None:
    """In the store epoch, before an admission: reap an expired lease whose owner the runtime
    proves dead, with the recovery of ``qa clear-stale-lease`` and no ``--force``. A live owner
    keeps its lease: the command ends with ``[lease_held]`` and ``stale_heartbeat``."""

    def recover(lease: JsonObject) -> None:
        _recover_cleared_owner(
            ledger,
            root,
            lease,
            ops,
            approval=None,
            reason="the runtime reaped the expired QA lease of a dead owner",
            verdict="owner-dead",
        )

    qa_lease.reap_expired(ledger, _lock, _lease, recover)


def clear_stale_lease(args: argparse.Namespace, ops: QaWorkflowOps) -> JsonObject:
    repo = ops._repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _lock(ledger):
        lease = _lease(ledger)
        if lease is None:
            raise CoordinatorError(
                "there is no QA lease to clear",
                remedy="run 'qa status' to confirm the lane holds no lease; there is nothing to clear",
            )
        if not _lease_expired(lease):
            raise CoordinatorError(
                "a live QA lease cannot be force-unlocked",
                remedy="wait until the lease expires (see 'qa status'), then run 'qa clear-stale-lease' again",
            )
        expected = {
            "host": args.expected_host,
            "pid": args.expected_pid,
            "expires_at": args.expected_expiry,
        }
        if any(lease[field] != value for field, value in expected.items()):
            raise CoordinatorError(
                "QA lease changed; coordinator must validate the current owner again",
                remedy="run 'qa status' and pass the current lease's host, pid and expiry as --expected-host, --expected-pid and --expected-expiry",
            )
        approval = ops._approval(args)
        if qa_lease.active(ledger):
            qa_lease.clear(ledger, lease, approval=approval, reason=args.reason.strip())
        else:
            _delete_record(ledger, _lane_path(ledger), reason="clear stale QA lease")
        restored = _recover_cleared_owner(
            ledger, root, lease, ops, approval=approval, reason=args.reason.strip()
        )
    return {
        "state": "cleared",
        **_owner_document(*_owner(lease)),
        "restored_dispatch_state": restored,
    }
