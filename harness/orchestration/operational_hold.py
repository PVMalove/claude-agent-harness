"""Operational holds: a batch kept suspended in the runtime state store (issue #684).

Unknown, missing, legacy-quarantined or incompatible system data never becomes a decision: the
coordinator holds the batch in its runtime state record (``suspended``, failure step
``hold:<reason>``) and raises attention on it, and every command that settles or decides the batch
stops on the hold until ``recovery resume`` re-checks the cause and releases it.  A hold is
operational state only: it writes no lifecycle record, decision or approval, and
``needs_attention`` stays a flag of the batch.  A batch the ledger migration quarantined is held by
the migration's own step and is never released here; a store record that cannot be read at all
holds its batch without any write.
"""

from __future__ import annotations

from dataclasses import dataclass

from harness.orchestration.core.runtime_state_contract import QUARANTINE_STEP
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.runtime_intent import (
    first_record,
    mark_ready,
    mark_reconciling,
    next_revision,
    write_next,
)
from harness.orchestration.runtime_state_store import (
    RuntimeStateError,
    RuntimeStateStore,
    sanitize_detail,
)

HOLD_STEP_PREFIX = "hold:"
# Why a batch is held; ``legacy-quarantine`` is the ledger migration's own quarantine step.
HOLD_REASONS = (
    "unknown-value",
    "missing-value",
    "legacy-quarantine",
    "unreadable-record",
    "runtime-compatibility",
)
# A quarantine keeps its evidence for a human: no recovery command releases it.
PERMANENT_REASONS = frozenset({"legacy-quarantine"})
HOLD_REMEDY = (
    "run 'coordinator.py recovery status --batch {batch}' for the diagnostics, fix the cause, "
    "then run 'coordinator.py recovery resume --batch {batch}'; nothing was abandoned, retried "
    "or decided"
)
QUARANTINE_REMEDY = (
    "run 'coordinator.py ledger inspect --batch {batch}' and 'ledger export --batch {batch}' to "
    "read the quarantined evidence and escalate it to a human; no recovery command releases a "
    "quarantine, and nothing was abandoned"
)
INTERRUPTED = (
    "the operation stopped before its store commit; recovery resume reconciles it"
)


@dataclass(frozen=True)
class Hold:
    """One active hold: its reason, the failure the store keeps and its durable revision
    (``None`` when the store record cannot be read)."""

    batch_id: str
    reason: str
    code: str
    step: str
    message: str
    revision: int | None

    @property
    def releasable(self) -> bool:
        """Whether ``recovery resume`` may release it once its cause is gone."""
        return self.reason not in PERMANENT_REASONS and self.revision is not None

    @property
    def remedy(self) -> str:
        template = (
            QUARANTINE_REMEDY if self.reason in PERMANENT_REASONS else HOLD_REMEDY
        )
        return template.format(batch=self.batch_id)

    def to_dict(self) -> JsonObject:
        return {
            "batch_id": self.batch_id,
            "reason": self.reason,
            "code": self.code,
            "step": self.step,
            "message": self.message,
            "revision": self.revision,
            "releasable": self.releasable,
            "remedy": self.remedy,
        }

    def error(self) -> RuntimeStateError:
        """The refusal every command that would act on the held batch raises."""
        return RuntimeStateError(
            self.code,
            self.step,
            f"batch {self.batch_id} is held ({self.reason}): {self.message}",
            remedy=self.remedy,
            durable_revision=self.revision,
        )


def step_for(reason: str) -> str:
    """The failure step that keeps a hold of ``reason``."""
    if reason not in HOLD_REASONS:
        raise ValueError(f"unknown operational hold reason {reason!r}")
    if reason == "legacy-quarantine":
        return QUARANTINE_STEP
    return HOLD_STEP_PREFIX + reason


def reason_of(step: object) -> str | None:
    """The hold reason a failure step names, or ``None`` for any other failure."""
    if step == QUARANTINE_STEP:
        return "legacy-quarantine"
    if isinstance(step, str) and step.startswith(HOLD_STEP_PREFIX):
        reason = step.removeprefix(HOLD_STEP_PREFIX)
        return reason if reason in HOLD_REASONS else None
    return None


def failure_code(reason: str) -> str:
    """The runtime failure code a hold of ``reason`` records."""
    if reason == "runtime-compatibility":
        return "runtime-compatibility"
    return "integrity-failure"


def of_record(batch_id: str, record: JsonObject) -> Hold | None:
    """The hold a valid store record keeps, ``None`` when it keeps none."""
    failure = record["failure"]
    if record["state"] != "suspended" or failure is None:
        return None
    reason = reason_of(failure["step"])
    if reason is None:
        return None
    return Hold(
        batch_id,
        reason,
        str(failure["code"]),
        str(failure["step"]),
        str(failure["message"]),
        int(record["revision"]),
    )


def current_hold(store: RuntimeStateStore, batch_id: str) -> Hold | None:
    """The active hold of ``batch_id``; a store record that cannot be read holds it too."""
    try:
        record = store.read(batch_id)
    except RuntimeStateError as exc:
        return Hold(
            batch_id,
            "unreadable-record",
            exc.code,
            exc.step,
            str(exc.diagnostics.get("detail") or exc.code),
            None,
        )
    return None if record is None else of_record(batch_id, record)


def require_no_hold(store: RuntimeStateStore, batch_id: str) -> None:
    """Refuse an action on a held batch, with the recovery route as its remedy."""
    held = current_hold(store, batch_id)
    if held is not None:
        raise held.error()


def hold(
    store: RuntimeStateStore, batch_id: str, reason: str, detail: str
) -> Hold | None:
    """Hold ``batch_id`` for ``reason`` and return the hold now on disk.

    A repeated hold adds no revision, a quarantine keeps its own step and a completed record is
    never held (``None``).  A store record that cannot be read cannot be written: it raises."""
    failure: JsonObject = {
        "code": failure_code(reason),
        "step": step_for(reason),
        "message": sanitize_detail(detail) or reason,
    }
    record = store.read(batch_id) or first_record(store, batch_id)
    if record["state"] == "completed":
        return None
    existing = of_record(batch_id, record)
    if existing is not None and (
        existing.reason in PERMANENT_REASONS or record["failure"] == failure
    ):
        return existing
    held = next_revision(record, state="suspended", failure=failure)
    write_next(store, record, held)
    return of_record(batch_id, held)


def release(store: RuntimeStateStore, batch_id: str) -> JsonObject | None:
    """Release a releasable hold and return the record now on disk; without a hold the record
    is returned unchanged.

    An idle record returns to ``ready``.  One with an unfinished operation stays ``suspended`` as
    an interrupted operation, so the reconciliation of ``settle`` finishes or stops on it."""
    held = current_hold(store, batch_id)
    if held is None:
        return store.read(batch_id)
    if not held.releasable:
        raise CoordinatorError(
            f"the {held.reason} hold of {batch_id} is not released by a recovery command",
            remedy=held.remedy,
        )
    record = store.read(batch_id)
    if record is None:
        raise CoordinatorError(
            f"the hold of {batch_id} has no store record",
            remedy=held.remedy,
        )
    operation = record["operation"]
    if operation is not None and operation["phase"] != "committed":
        interrupted = next_revision(
            record,
            failure={
                "code": "execution-failure",
                "step": "reconcile:detect",
                "message": INTERRUPTED,
            },
        )
        return write_next(store, record, interrupted)
    return mark_ready(store, mark_reconciling(store, record, interrupted=INTERRUPTED))
