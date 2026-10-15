"""Durable intent and observation of the external effect of ``dispatch send``.

Handing a brief to its transport may start a worker outside both stores; it can be neither rolled
back nor repeated blindly.  ``DispatchSend.run`` persists the effect's plan as a write-once intent
payload and records the operation ``prepared`` before the hand-off.  For a channel that starts a
worker it then marks the effect ``unknown`` right before the call, so a process that stops during
the call leaves durable evidence that the worker may run, and ``not-started`` proves it does not.
The observed outcome and the ledger steps it implies are persisted as a second write-once payload
before any ledger record changes; the steps are then written and verified as a composite write.

An observation always moves the effect to ``confirmed``: the outcome is proved, and the outcome
itself, including that no worker started, is in the observation payload.  The runtime state
contract leads from ``unknown`` only to ``confirmed``; ``not-started`` means the transport was
never called.

Three steps carry an observation: ``build_observation`` plans the payload, ``store_observation``
persists it, and ``query_launch`` asks the adapter or provider when no observation was stored.

``runtime_reconcile`` finishes an operation a process left unfinished, or stops on it; nothing
here or there repeats the hand-off.  Nothing here issues an approval.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

from harness.errors import HarnessError
from harness.orchestration import extensions
from harness.orchestration.core import utils
from harness.orchestration.core.constants import BatchState, DispatchState
from harness.orchestration.core.utils import JsonObject
from harness.orchestration.ledger.ledger_ops import _ledger_errors
from harness.orchestration.ledger.lifecycle import (
    BatchRecord,
    DispatchRecord,
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
# How the brief reaches its worker: ``none`` starts nothing (the session runs the role itself);
# ``adapter`` and ``native`` start a worker outside both stores.  A ``Literal`` keeps the values
# checked without adding a vocabulary to the ``systemFields.v1`` registry.
type EffectChannel = Literal["none", "adapter", "native"]
# What a hand-off changed; ``unchanged`` leaves its ledger records as they were.
type EffectOutcome = Literal["dispatched", "blocked", "unchanged"]
CHANNELS: frozenset[str] = frozenset(get_args(EffectChannel.__value__))
OUTCOMES: frozenset[str] = frozenset(get_args(EffectOutcome.__value__))
UNKNOWN_REMEDY = (
    "repeat the command: it reconciles the hand-off from its durable intent and never starts "
    "the worker again"
)
# An adapter's observe runs under the ledger lock, so its answer is bounded.
OBSERVE_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class EffectResult:
    """The classified outcome of a hand-off: ``evidence`` is what the ledger records about it,
    ``error`` what the command raises once the outcome is committed."""

    outcome: EffectOutcome
    reason: str
    evidence: JsonObject | None = None
    error: HarnessError | None = None

    @property
    def detail(self) -> str:
        """What the observation records about the outcome: its error, else its reason."""
        return self.error.message if self.error is not None else self.reason


def adapter_command(adapter: Path) -> list[str]:
    """The start of a runtime adapter's command line; a ``.py`` adapter runs on this Python."""
    if adapter.suffix.lower() == ".py":
        return [sys.executable, str(adapter)]
    return [str(adapter)]


def query_launch(
    repo: Path, records: Path, payload: JsonObject
) -> extensions.LaunchObservation | None:
    """Ask the adapter or the native provider that took an unconfirmed hand-off whether its
    worker was launched and still runs; ``None`` when nothing answers in a provable way.

    Only an observation is requested: neither the adapter's ``dispatch`` verb nor a provider's
    ``handoff`` is ever called again."""
    effect, dispatch_id = payload["effect"], str(payload["dispatch_id"])
    brief = records / DispatchRecord.directory / f"{dispatch_id}.json"
    answer: object = None
    if effect["channel"] == "adapter" and isinstance(effect.get("adapter"), str):
        command = [
            *adapter_command(Path(effect["adapter"])),
            "observe",
            *effect["adapter_args"],
            "--repo",
            str(repo),
            "--brief",
            str(brief),
        ]
        try:
            result = subprocess.run(
                command,
                cwd=effect["worker_worktree"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=OBSERVE_TIMEOUT_SECONDS,
            )
            if result.returncode == 0:
                answer = _adapter_answer(json.loads(result.stdout))
        except (OSError, subprocess.TimeoutExpired, ValueError):
            answer = None
    elif effect["channel"] == "native":
        answer = _provider_answer(brief, effect)
    if (
        not isinstance(answer, extensions.LaunchObservation)
        or answer.dispatch_id != dispatch_id
        or not isinstance(answer.launched, bool)
        or not isinstance(answer.live, bool)
        or not (
            answer.launch_id is None
            or (isinstance(answer.launch_id, str) and answer.launch_id)
        )
    ):
        return None
    return answer


def _adapter_answer(value: object) -> extensions.LaunchObservation | None:
    """An adapter's JSON answer with exactly the observation fields, or ``None``."""
    fields = {"dispatch_id", "launched", "live", "launch_id"}
    if (
        not isinstance(value, dict)
        or not {"dispatch_id", "launched", "live"} <= set(value) <= fields
    ):
        return None
    return extensions.LaunchObservation(**value)


def _provider_answer(brief_path: Path, effect: JsonObject) -> object:
    """The native provider's optional ``observe_launch`` answer for the brief the hand-off
    sent; a changed brief, a missing method or any failure of the provider is no answer."""
    brief = LifecycleLedger.read_record_lenient(brief_path)
    if brief is None or canonical_sha256(brief) != effect["brief_sha256"]:
        return None
    plan = brief.get("runtime_access")
    name = plan.get("runtime_extension") if isinstance(plan, dict) else None
    if not isinstance(name, str):
        return None
    try:
        provider = extensions.runtime_access(name)
        observe = getattr(provider, "observe_launch", None)
        return observe(brief) if callable(observe) else None
    except Exception:  # noqa: BLE001 -- a project-supplied provider proves nothing by failing
        return None


def record_paths(batch_id: str, dispatch_id: str) -> list[str]:
    """The ledger records a hand-off may change, relative to the generation."""
    return [
        f"{BatchRecord.directory}/{batch_id}.json",
        f"{DispatchStatusRecord.directory}/{dispatch_id}.json",
    ]


def observation_id(operation_id: str) -> str:
    """The deterministic ID the observation payload of ``operation_id`` is stored under."""
    return str(uuid.uuid5(AUDIT_NAMESPACE, f"{operation_id}:observation"))


def unfinished_hand_off(
    store: RuntimeStateStore, batch_id: str, dispatch_id: str
) -> str | None:
    """How far the batch's unfinished hand-off of ``dispatch_id`` got, read only: ``observed``
    once its outcome is stored, else its effect state (``unknown`` while its worker may run,
    ``not-started`` before the call); ``None`` when the batch holds no such hand-off."""
    record = store.read(batch_id)
    operation = None if record is None else record["operation"]
    if (
        record is None
        or operation is None
        or record["dispatch_id"] != dispatch_id
        or operation["kind"] != KIND
        or operation["phase"] == "committed"
    ):
        return None
    ref = store.intent_ref(batch_id, observation_id(operation["operation_id"]))
    if (
        operation["effect"]["state"] == "confirmed"
        or (store.location.directory / ref).is_file()
    ):
        return "observed"
    return str(operation["effect"]["state"])


def dispatch_send_records(
    outcome: EffectOutcome,
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
        entry["state"] = DispatchState.DISPATCHED
        status.update(
            {
                "state": DispatchState.DISPATCHED,
                "updated_at": moment,
                "heartbeat_at": moment,
                "runtime_access": evidence,
                "worker_worktree": effect["worker_worktree"],
                "worker_snapshot_commit": effect["worker_snapshot_commit"],
            }
        )
    else:
        entry["state"] = DispatchState.BLOCKED
        batch["state"] = BatchState.BLOCKED
        status.update(
            {
                "state": DispatchState.BLOCKED,
                "updated_at": moment,
                "runtime_failure": evidence,
            }
        )
    return [BatchRecord.from_dict(batch), DispatchStatusRecord.from_dict(status)]


def build_observation(
    ledger: LifecycleLedger,
    store: RuntimeStateStore,
    payload: JsonObject,
    result: EffectResult,
    changes: list[LedgerRecordVO],
    moment: str,
) -> JsonObject:
    """The observation payload of the intent ``payload``, built but not persisted: the
    hand-off's ``result``, the checked ledger steps of ``changes`` under the operation's
    deterministic audit IDs, and the hash every record the hand-off may change holds once they
    are written."""
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
        "outcome": result.outcome,
        "reason": result.reason,
        "evidence": sanitize_detail(result.detail),
        "observed_at": moment,
        "generation": payload["generation"],
        "steps": steps,
        "record_hashes": hashes,
    }


def store_observation(
    store: RuntimeStateStore, record: JsonObject, observation: JsonObject
) -> JsonObject:
    """Persist ``observation`` and move the operation to ``effect-observed`` with a confirmed
    effect, unless the record already is there; returns the record now on disk."""
    batch_id = record["identity"]["batch_id"]
    ref = store.write_intent(
        batch_id, observation_id(observation["operation_id"]), observation
    )
    if record["operation"]["phase"] != "prepared":
        return record
    following = with_phase(
        record,
        "effect-observed",
        ledger={
            "generation": observation["generation"],
            "record_hashes": observation["record_hashes"],
        },
    )
    return write_next(
        store,
        record,
        following,
        {
            "state": "confirmed",
            "evidence_ref": ref,
            "observed_at": observation["observed_at"],
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
        current, generation, root, _ = writer.begin()
        before = {
            path: digest(root / path)
            for path in record_paths(self.batch_id, self.dispatch_id)
        }
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
            observation = build_observation(
                self.ledger, self.store, payload, result, changes, moment
            )
            record = store_observation(self.store, record, observation)
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
        finish(self.ledger, self.store, root, record, observation["steps"])
        if result.error is not None:
            raise result.error
        return result
