"""Durable intent and observation of the external effect of ``dispatch send``.

Handing a brief to its transport may start a worker outside both stores; it can be neither rolled
back nor repeated blindly.  ``DispatchSend.run`` persists the effect's plan as a write-once intent
payload and records the operation ``prepared`` before the hand-off.  For a channel that starts a
worker it then marks the effect ``unknown`` right before the call, so a process that stops during
the call leaves durable evidence that the worker may run, and ``not-started`` proves it does not.
The observed outcome and the ledger steps it implies are persisted as a second write-once payload
before any ledger record changes; the steps are then written and verified as a composite write.

``runtime_reconcile`` finishes an operation a process left unfinished, or stops on it; nothing
here or there repeats the hand-off.  Nothing here issues an approval.
"""

from __future__ import annotations

import copy
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from harness.errors import HarnessError
from harness.orchestration.core import utils
from harness.orchestration.core.utils import JsonObject
from harness.orchestration.ledger.ledger_ops import _ledger_errors
from harness.orchestration.ledger.lifecycle import (
    BatchRecord,
    DispatchStatusRecord,
    LedgerRecordVO,
    LifecycleLedger,
)
from harness.orchestration.runtime_intent import (
    AUDIT_NAMESPACE,
    CompositeWrite,
    audit_id_for,
    canonical_sha256,
    digest,
    failure_code,
    finish,
    idempotency_key,
    next_revision,
    suspended_error,
    with_phase,
    write_next,
)
from harness.orchestration.runtime_state_store import (
    RuntimeStateStore,
    sanitize_detail,
)

KIND = "dispatch-send"
# How the brief reaches its worker: ``none`` starts nothing (the session runs the role itself).
CHANNELS = ("none", "adapter", "native")
OUTCOMES = ("dispatched", "blocked", "unchanged")
UNKNOWN_REMEDY = (
    "repeat the command: it reconciles the hand-off from its durable intent and never starts "
    "the worker again"
)


@dataclass(frozen=True)
class EffectResult:
    """The classified outcome of a hand-off: ``evidence`` is what the ledger records about it,
    ``error`` what the command raises once the outcome is committed."""

    outcome: str
    reason: str
    evidence: JsonObject | None = None
    error: HarnessError | None = None


def adapter_command(adapter: Path) -> list[str]:
    """The start of a runtime adapter's command line; a ``.py`` adapter runs on this Python."""
    if adapter.suffix.lower() == ".py":
        return [sys.executable, str(adapter)]
    return [str(adapter)]


def record_paths(batch_id: str, dispatch_id: str) -> list[str]:
    """The ledger records a hand-off may change, relative to the generation."""
    return [
        f"{BatchRecord.directory}/{batch_id}.json",
        f"{DispatchStatusRecord.directory}/{dispatch_id}.json",
    ]


def observation_id(operation_id: str) -> str:
    """The deterministic ID the observation payload of ``operation_id`` is stored under."""
    return str(uuid.uuid5(AUDIT_NAMESPACE, f"{operation_id}:observation"))


def dispatch_send_records(
    outcome: str,
    batch: JsonObject,
    status: JsonObject,
    effect: JsonObject,
    evidence: JsonObject | None,
    moment: str,
) -> list[LedgerRecordVO]:
    """The ledger records an observed hand-off changes: the batch first and the status last,
    because the worker writes only its status."""
    if outcome == "unchanged":
        return []
    batch, status = copy.deepcopy(batch), copy.deepcopy(status)
    entry = next(
        item
        for item in batch["dispatches"]
        if item["dispatch_id"] == status["dispatch_id"]
    )
    if outcome == "dispatched":
        entry["state"] = "dispatched"
        status.update(
            {
                "state": "dispatched",
                "updated_at": moment,
                "heartbeat_at": moment,
                "runtime_access": evidence,
                "worker_worktree": effect["worker_worktree"],
                "worker_snapshot_commit": effect["worker_snapshot_commit"],
            }
        )
    else:
        entry["state"] = batch["state"] = "blocked"
        status.update(
            {"state": "blocked", "updated_at": moment, "runtime_failure": evidence}
        )
    return [BatchRecord.from_dict(batch), DispatchStatusRecord.from_dict(status)]


def observation(
    ledger: LifecycleLedger,
    store: RuntimeStateStore,
    payload: JsonObject,
    outcome: str,
    reason: str,
    detail: str,
    changes: list[LedgerRecordVO],
    moment: str,
) -> JsonObject:
    """The observation payload of the intent ``payload``: the outcome and the checked ledger
    steps it implies, under the operation's deterministic audit IDs, and the hash every record
    the hand-off may change holds once they are written."""
    planner = CompositeWrite(
        ledger,
        store,
        batch_id=payload["batch_id"],
        dispatch_id=payload["dispatch_id"],
        kind=KIND,
        idempotency_key=payload["idempotency_key"],
    )
    for record in changes:
        planner.replace_record(record)
    with _ledger_errors():
        _, root, steps = planner.plan()
    for index, step in enumerate(steps):
        step["audit"]["audit_id"] = audit_id_for(payload["operation_id"], index)
    hashes: JsonObject = {path: digest(root / path) for path in payload["before"]}
    hashes.update({step["path"]: step["after_sha256"] for step in steps})
    return {
        "operation_id": payload["operation_id"],
        "outcome": outcome,
        "reason": reason,
        "evidence": sanitize_detail(detail),
        "observed_at": moment,
        "generation": payload["generation"],
        "steps": steps,
        "record_hashes": hashes,
    }


def observed(
    store: RuntimeStateStore, record: JsonObject, payload: JsonObject
) -> JsonObject:
    """Persist the observation ``payload`` and move the operation to ``effect-observed`` with a
    confirmed effect, unless the record already is there; returns the record now on disk."""
    batch_id = record["identity"]["batch_id"]
    ref = store.write_intent(batch_id, observation_id(payload["operation_id"]), payload)
    if record["operation"]["phase"] != "prepared":
        return record
    following = with_phase(
        record,
        "effect-observed",
        ledger={
            "generation": payload["generation"],
            "record_hashes": payload["record_hashes"],
        },
    )
    return write_next(
        store,
        record,
        following,
        {
            "state": "confirmed",
            "evidence_ref": ref,
            "observed_at": payload["observed_at"],
        },
    )


class DispatchSend:
    """Hand one approved brief to its transport behind a durable intent.

    ``run`` runs under the ledger lock the caller already holds, after the caller reconciled the
    batch and validated the dispatch; ``batch`` and ``status`` are the records it read."""

    def __init__(
        self,
        ledger: LifecycleLedger,
        store: RuntimeStateStore,
        *,
        batch: JsonObject,
        status: JsonObject,
        effect: JsonObject,
    ) -> None:
        self.ledger = ledger
        self.store = store
        self.batch = batch
        self.status = status
        self.effect = effect
        self.batch_id = str(batch["batch_id"])
        self.dispatch_id = str(status["dispatch_id"])

    def run(self, hand_off: Callable[[], EffectResult]) -> EffectResult:
        """Persist the intent, call ``hand_off`` once, persist what it observed and commit the
        ledger records it implies; raises the outcome's error once it is committed.

        A failure before the hand-off leaves the ledger unchanged and the hand-off uncalled; an
        outcome that cannot be classified suspends the operation with ``ambiguous-effect``."""
        key = idempotency_key(KIND, self.dispatch_id, {"effect": self.effect})
        writer = CompositeWrite(
            self.ledger,
            self.store,
            batch_id=self.batch_id,
            dispatch_id=self.dispatch_id,
            kind=KIND,
            idempotency_key=key,
        )
        existing = writer.current_record()
        with _ledger_errors():
            generation, root, _ = writer.plan()
        before = {
            path: digest(root / path)
            for path in record_paths(self.batch_id, self.dispatch_id)
        }
        current = writer.first_record() if existing is None else existing
        payload: JsonObject = {
            "operation_id": str(uuid.uuid4()),
            "idempotency_key": key,
            "kind": KIND,
            "batch_id": self.batch_id,
            "dispatch_id": self.dispatch_id,
            "generation": generation,
            "effect": self.effect,
            "before": before,
        }
        record = writer.prepare(current, payload, canonical_sha256(before), None, {})
        starts_worker = self.effect["channel"] != "none"
        if starts_worker:
            marked = next_revision(record)
            marked["operation"]["effect"] = {
                "state": "unknown",
                "evidence_ref": None,
                "observed_at": utils._now(),
            }
            record = write_next(self.store, record, marked)
        try:
            result = hand_off()
        except Exception as exc:
            raise suspended_error(
                self.store,
                self.batch_id,
                "effect",
                exc,
                code="ambiguous-effect" if starts_worker else failure_code(exc),
                remedy=UNKNOWN_REMEDY,
            ) from exc
        moment = utils._now()
        try:
            changes = dispatch_send_records(
                result.outcome,
                self.batch,
                self.status,
                self.effect,
                result.evidence,
                moment,
            )
            detail = result.error.message if result.error else result.reason
            seen = observation(
                self.ledger,
                self.store,
                payload,
                result.outcome,
                result.reason,
                detail,
                changes,
                moment,
            )
            record = observed(self.store, record, seen)
        except Exception as exc:
            state = record["operation"]["effect"]["state"]
            raise suspended_error(
                self.store,
                self.batch_id,
                "effect-observe",
                exc,
                detail=f"the hand-off returned {result.outcome}, but its observation is not "
                f"durable; the store keeps the effect {state}",
                remedy=UNKNOWN_REMEDY,
            ) from exc
        finish(self.ledger, self.store, root, record, seen["steps"])
        if result.error is not None:
            raise result.error
        return result
