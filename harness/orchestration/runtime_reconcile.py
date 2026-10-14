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
from harness.orchestration.ledger.ledger_ops import _ledger_errors
from harness.orchestration.ledger.lifecycle import LifecycleLedger, audit_details
from harness.orchestration.runtime_intent import (
    WRITE_MODES,
    apply_step,
    audit_id_for,
    content_bytes,
    digest,
    next_revision,
    operation_hashes,
    settle_phases,
    step_problem,
)
from harness.orchestration.runtime_state_store import (
    RuntimeStateError,
    RuntimeStateStore,
)

STOP_REMEDY = (
    "inspect the named ledger record against the intent; restore it to the planned before or "
    "after content and repeat the command, which reconciles again"
)


class _Stop(Exception):
    """Why an intent cannot be reconciled, found before anything is written."""

    def __init__(self, code: str, step: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.step = step
        self.detail = detail


def settle(repo: Path, root: Path, ledger: LifecycleLedger, batch_id: str) -> bool:
    """Finish the batch's unfinished composite write, if there is one; ``True`` when an
    operation was rolled forward, so ledger records the caller read earlier may be stale."""
    store = RuntimeStateStore(repo, root)
    record = store.read(batch_id)
    if record is None:
        return False
    operation = record["operation"]
    if operation is None or operation["phase"] == "committed":
        if record["state"] in ("suspended", "reconciling"):
            _ready(store, _reconciling(store, record))
        return False
    with _ledger_errors():
        try:
            records = ledger.records_root()
            steps = _verified_steps(store, ledger, record)
            pending = _pending(ledger, records, steps)
        except _Stop as stop:
            raise RuntimeStateError(
                stop.code,
                stop.step,
                stop.detail,
                remedy=STOP_REMEDY,
                durable_revision=int(record["revision"]),
            ) from None
        record = _reconciling(store, record)
        for step, status in pending:
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
                raise RuntimeStateError(
                    "integrity-failure",
                    _step_name(step),
                    problem,
                    remedy=STOP_REMEDY,
                    durable_revision=int(record["revision"]),
                )
        _ready(store, settle_phases(store, record, steps))
    return True


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
    payload = store.read_intent(batch_id, refs[0])
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
        event = ledger.read_audit(audit["audit_id"])
        described = event is not None and (
            event.get("action"),
            event.get("details"),
        ) == (audit["action"], audit["details"])
        if event is not None and not described:
            raise _Stop(
                "integrity-failure",
                _step_name(step),
                f"the audit event of {step['path']} records another write",
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
                f"{step['path']} is neither its planned before nor after content",
            )
        if work and status != "pending":
            raise _Stop(
                "integrity-failure",
                _step_name(step),
                f"{step['path']} was written after an earlier planned step was not",
            )
        if status != "done":
            work.append((step, status))
    return work
