"""Reconcile an unfinished composite ledger write from its durable intent.

``settle`` runs under the ledger lock at the start of every command that performs a composite
write, before the command reads the batch.  An operation the runtime state store holds as
unfinished is checked against its intent payload, read-only, and then rolled forward: a planned
record that is still absent or unchanged is written with a compare-and-swap, a written record
whose audit event is missing gets that event, and the store reaches ``committed`` only after
every record and event is verified.  An operation already committed is left alone, so repeating
``settle`` changes nothing.

Reconciliation writes only the paths and audit IDs of the verified intent: it records no
decision or approval, never creates over an existing file and never replaces without the
planned digest.
"""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

from harness.orchestration.core.utils import JsonObject
from harness.orchestration.ledger.lifecycle import (
    LedgerError,
    LifecycleLedger,
    audit_details,
)
from harness.orchestration.runtime_intent import (
    WRITE_MODES,
    apply_step,
    audit_id_for,
    content_bytes,
    digest,
    error_detail,
    failure_code,
    next_revision,
    operation_hashes,
    settle_phases,
    step_problem,
)
from harness.orchestration.runtime_state_store import (
    RuntimeStateError,
    RuntimeStateStore,
    sanitize_detail,
)

STOP_REMEDY = (
    "inspect the named ledger record against the intent; restore it to the planned before or "
    "after content and repeat the command, which reconciles again"
)


class _Stop(Exception):
    """A controlled stop: why an intent cannot be reconciled, with its evidence."""

    def __init__(
        self, code: str, step: str, detail: str, evidence: JsonObject | None = None
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.step = step
        self.detail = detail
        self.evidence = evidence or {}


def short(sha256: object) -> str | None:
    """A hash shortened for a diagnostic; ``None`` stays ``None`` (an absent file)."""
    return sha256[:12] if isinstance(sha256, str) else None


def settle(repo: Path, root: Path, ledger: LifecycleLedger, batch_id: str) -> bool:
    """Finish the batch's unfinished composite write, if there is one; ``True`` when an
    operation was rolled forward, so ledger records the caller read earlier may be stale.

    A violated hash, an unknown state or a concurrent writer is a controlled stop: the store
    is suspended with the failure, nothing is retried, and the same stop found again by the
    next read-only check raises without a new revision."""
    store = RuntimeStateStore(repo, root)
    record = store.read(batch_id)
    if record is None:
        return False
    operation = record["operation"]
    if operation is None or operation["phase"] == "committed":
        if record["state"] in ("suspended", "reconciling"):
            _ready(store, _reconciling(store, record))
        return False
    try:
        records = ledger.records_root()
        steps = _verified_steps(store, ledger, record)
        pending = _pending(ledger, records, steps)
    except _Stop as stop:
        raise _stopped(store, batch_id, stop) from None
    except LedgerError as exc:
        broken = _Stop("integrity-failure", "reconcile:ledger", exc.message)
        raise _stopped(store, batch_id, broken) from exc
    record = _reconciling(store, record)
    step_name = "reconcile:intent"
    try:
        for step, status in pending:
            step_name = _step_name(step)
            if status == "pending":
                apply_step(ledger, records, step)
            else:
                audit = step["audit"]
                ledger.append_audit(
                    records / step["path"],
                    audit["action"],
                    audit["details"],
                    audit_id=audit["audit_id"],
                )
        for step in steps:
            if (problem := step_problem(ledger, records, step)) is not None:
                raise _Stop("integrity-failure", _step_name(step), problem)
        step_name = "reconcile:store-commit"
        record = settle_phases(store, record, steps)
    except _Stop as stop:
        raise _stopped(store, batch_id, stop) from None
    except RuntimeStateError:
        raise
    except Exception as exc:
        raise _stopped(
            store, batch_id, _Stop(failure_code(exc), step_name, error_detail(exc))
        ) from exc
    _ready(store, record)
    return True


def _stopped(store: RuntimeStateStore, batch_id: str, stop: _Stop) -> RuntimeStateError:
    """Record a controlled stop and return the error to raise.  The same failure already
    recorded on a suspended record adds no revision; a ``ready`` record cannot take the stop
    (its state table has no edge to ``suspended``), so its revision is kept as it is."""
    record = store.read(batch_id)
    failure: JsonObject = {
        "code": stop.code,
        "step": stop.step,
        "message": sanitize_detail(stop.detail) or stop.code,
    }
    durable = None if record is None else int(record["revision"])
    if (
        record is not None
        and record["state"] in ("running", "reconciling", "suspended")
        and not (record["state"] == "suspended" and record["failure"] == failure)
    ):
        suspended = next_revision(record, state="suspended", failure=failure)
        durable = store.write(
            suspended, expected_revision=int(record["revision"])
        ).revision
    error = RuntimeStateError(
        stop.code, stop.step, stop.detail, remedy=STOP_REMEDY, durable_revision=durable
    )
    error.diagnostics["evidence"] = stop.evidence
    return error


def _reconciling(store: RuntimeStateStore, record: JsonObject) -> JsonObject:
    """Move a stopped or interrupted record to ``reconciling``: an interrupted ``running`` one
    passes through ``suspended``, as the state table requires."""
    if record["state"] == "running":
        record = _write(
            store,
            record,
            next_revision(
                record,
                state="suspended",
                failure={
                    "code": "execution-failure",
                    "step": "reconcile:detect",
                    "message": "the composite write stopped before its store commit",
                },
            ),
        )
    if record["state"] == "suspended":
        record = _write(
            store, record, next_revision(record, state="reconciling", failure=None)
        )
    return record


def _ready(store: RuntimeStateStore, record: JsonObject) -> JsonObject:
    ready = next_revision(record, state="ready", failure=None)
    ready["last_confirmed_boundary"] = {
        "revision": ready["revision"],
        "state": "ready",
        "confirmed_at": ready["updated_at"],
    }
    return _write(store, record, ready)


def _write(
    store: RuntimeStateStore, current: JsonObject, following: JsonObject
) -> JsonObject:
    store.write(following, expected_revision=int(current["revision"]))
    return following


def _step_name(step: JsonObject) -> str:
    return f"reconcile:{str(step.get('path', '')).split('/')[0] or 'intent'}"


def _verified_steps(
    store: RuntimeStateStore, ledger: LifecycleLedger, record: JsonObject
) -> list[JsonObject]:
    """The intent payload's steps, once the payload is bound to the store record by its
    operation, idempotency key, generation and every hash; a mismatch stops."""
    if record["state"] == "ready":
        raise _Stop(
            "integrity-failure",
            "reconcile:state",
            "a ready runtime state holds an unfinished operation: the state is unknown",
        )
    refs = record["input_refs"]
    if len(refs) != 1:
        raise _Stop(
            "integrity-failure",
            "reconcile:intent",
            "the unfinished operation does not name exactly one intent payload",
        )
    batch_id = record["identity"]["batch_id"]
    try:
        payload = store.read_intent(batch_id, refs[0])
    except RuntimeStateError as exc:
        raise _Stop(exc.code, "reconcile:intent", exc.diagnostics["detail"]) from exc
    operation = record["operation"]
    pointer = ledger.pointer()
    bindings = (
        ("operation_id", operation["operation_id"]),
        ("idempotency_key", operation["idempotency_key"]),
        ("kind", operation["kind"]),
        ("batch_id", batch_id),
        ("dispatch_id", record["dispatch_id"]),
        ("generation", record["ledger"]["generation"]),
    )
    for key, expected in bindings:
        if payload.get(key) != expected:
            raise _Stop(
                "integrity-failure",
                "reconcile:intent",
                f"the intent payload's {key} does not match the runtime state record",
            )
    if pointer is None or pointer.get("generation") != payload["generation"]:
        raise _Stop(
            "integrity-failure",
            "reconcile:generation",
            "the selected ledger generation is not the one the intent was planned in",
            {
                "expected": payload["generation"],
                "actual": None if pointer is None else pointer.get("generation"),
            },
        )
    steps = payload.get("steps")
    if not isinstance(steps, list) or not steps:
        raise _Stop(
            "integrity-failure", "reconcile:intent", "the intent payload has no steps"
        )
    for index, step in enumerate(steps):
        if (
            problem := _step_shape_problem(operation["operation_id"], index, step)
        ) is not None:
            raise _Stop("integrity-failure", "reconcile:intent", problem)
    if {step["path"]: step["after_sha256"] for step in steps} != record["ledger"][
        "record_hashes"
    ] or operation_hashes(steps) != (
        operation["expected_before_sha256"],
        operation["intended_after_sha256"],
    ):
        raise _Stop(
            "integrity-failure",
            "reconcile:intent",
            "the intent payload's hashes do not match the runtime state record",
        )
    return steps


def _step_shape_problem(operation_id: str, index: int, step: object) -> str | None:
    """Why one payload step is not a step the composite write planned, or ``None``."""
    if not isinstance(step, dict) or step.get("index") != index:
        return f"intent step {index} is malformed"
    path = step.get("path")
    parts = PurePosixPath(path).parts if isinstance(path, str) else ()
    if (
        step.get("mode") not in WRITE_MODES
        or len(parts) != 2
        or ".." in parts
        or parts[0] == "audit"
        or PurePosixPath(str(path)).is_absolute()
    ):
        return f"intent step {index} names an invalid mode or path"
    content = step.get("content")
    if not isinstance(content, str if step["mode"] == "create-artifact" else dict):
        return f"intent step {index} carries content of the wrong type"
    if (step["mode"] == "replace") == (step.get("before_sha256") is None):
        return f"intent step {index} has an invalid before hash"
    after = hashlib.sha256(content_bytes(step["mode"], content)).hexdigest()
    if after != step.get("after_sha256"):
        return f"intent step {index} content does not match its planned hash"
    audit = step.get("audit")
    if not isinstance(audit, dict) or audit.get("audit_id") != audit_id_for(
        operation_id, index
    ):
        return f"intent step {index} has an unexpected audit ID"
    details = audit.get("details")
    action, expected = audit_details(
        step["mode"],
        step["path"],
        after,
        before_sha256=step["before_sha256"],
        before={"state": details.get("from")} if isinstance(details, dict) else None,
        after=content if isinstance(content, dict) else None,
    )
    if (audit.get("action"), details) != (action, expected):
        return f"intent step {index} has an audit event that does not describe it"
    return None


def _pending(
    ledger: LifecycleLedger, records: Path, steps: list[JsonObject]
) -> list[tuple[JsonObject, str]]:
    """The steps still to do, each ``pending`` (write it) or ``audit`` (append its event);
    a record that is neither as planned before nor after, or a gap in the order, stops."""
    work: list[tuple[JsonObject, str]] = []
    for step in steps:
        current = digest(records / step["path"])
        audit = step["audit"]
        evidence: JsonObject = {
            "path": step["path"],
            "audit_id": audit["audit_id"],
            "expected_before": short(step["before_sha256"]),
            "expected_after": short(step["after_sha256"]),
            "actual": short(current),
        }
        try:
            event = ledger.read_audit(audit["audit_id"])
        except LedgerError as exc:
            raise _Stop(
                "integrity-failure", _step_name(step), exc.message, evidence
            ) from exc
        described = event is not None and (
            event.get("action"),
            event.get("details"),
        ) == (audit["action"], audit["details"])
        if event is not None and not described:
            raise _Stop(
                "integrity-failure",
                _step_name(step),
                f"the audit event of {step['path']} records another write",
                evidence,
            )
        if current == step["after_sha256"] and (
            described or step["before_sha256"] != step["after_sha256"]
        ):
            status = "done" if described else "audit"
        elif current == step["before_sha256"] and not described:
            status = "pending"
        else:
            raise _Stop(
                "integrity-failure",
                _step_name(step),
                f"{step['path']} holds {evidence['actual'] or 'no file'}, neither the "
                f"planned before {evidence['expected_before'] or '(absent)'} nor after "
                f"{evidence['expected_after']}",
                evidence,
            )
        if work and status != "pending":
            raise _Stop(
                "integrity-failure",
                _step_name(step),
                f"{step['path']} was written after an earlier planned step was not",
                evidence,
            )
        if status != "done":
            work.append((step, status))
    return work
