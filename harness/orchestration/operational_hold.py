"""Operational holds: a batch held by its recovery record in the runtime store (ADR 0026).

Unknown, missing or unreadable system data never becomes a decision: the coordinator holds the
batch in its recovery record ``recovery/hold/<batch_id>.json`` (``operationalHold.v1``: state
``held``, failure step ``hold:<reason>``), and every command that settles or decides the batch
stops on the hold until ``recovery resume`` re-checks the cause and releases it.  A hold is
operational state only: it writes no lifecycle record, decision or approval, and
``needs_attention`` stays a flag of the batch.  Python decides the hold; the Rust store writes it
(``state.write``, create then replace under the revision of the row) in the store unit of the
command that holds or releases it.  A hold record that cannot be read holds its batch without any
write.  The file-ledger epoch keeps no hold: there ``current_hold`` reads nothing, and ``hold`` and
``release`` refuse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from harness.gate_runner.gate_runner import sanitise
from harness.orchestration.core import state_view
from harness.orchestration.core.constants import TERMINAL_BATCH_STATES
from harness.orchestration.core.utils import CoordinatorError, JsonObject, _now
from harness.orchestration.ledger.ledger_ops import (
    _ledger_errors,
    _read_store_record,
    store_is_authority,
)
from harness.orchestration.ledger.lifecycle import LifecycleLedger

HOLD_CONTRACT = "operationalHold.v1"
HOLD_DIRECTORY = "recovery/hold"
HOLD_STEP_PREFIX = "hold:"
HOLD_STATES = ("held", "released")
# Why a batch is held.
HOLD_REASONS = ("unknown-value", "missing-value", "unreadable-record")
HOLD_REMEDY = (
    "run 'coordinator.py recovery status --batch {batch}' for the diagnostics, fix the cause, "
    "resolve its operational-hold attention with 'coordinator.py batch attention resolve "
    "--batch {batch}' when the batch needs attention, then run 'coordinator.py recovery resume "
    "--batch {batch}'; nothing was abandoned, retried or decided"
)
UNREADABLE_REMEDY = (
    "the hold record recovery/hold/{batch}.json does not verify, so no recovery command "
    "releases it: run 'coordinator.py ledger validate --full' and escalate the record to a "
    "human; nothing was abandoned, retried or decided"
)
FILE_EPOCH_REMEDY = (
    "run 'harness-runtime migrate' once, so the runtime store keeps the recovery records, then "
    "repeat the command"
)
DETAIL_LIMIT = 240
_ABSOLUTE_PATH = re.compile(r"(?<![\w.~-])(?:[A-Za-z]:[\\/]|[\\/])[^\s'\"]*")


def sanitize_detail(text: str) -> str:
    """Secrets masked, absolute paths dropped and length bounded; a fixed point of itself."""
    cleaned = _ABSOLUTE_PATH.sub("<path>", sanitise(text)).strip()
    return cleaned[:DETAIL_LIMIT].strip()


class OperationalHoldError(CoordinatorError):
    """The refusal of an action on a held batch: its failure code, its step and the evidence."""

    def __init__(
        self,
        code: str,
        step: str,
        message: str,
        *,
        remedy: str,
        diagnostics: JsonObject | None = None,
    ) -> None:
        super().__init__(message, remedy=remedy)
        self.code = code
        self.step = step
        self.diagnostics: JsonObject = dict(diagnostics or {})


class UnreadableHold(ValueError):
    """A hold record outside ``operationalHold.v1``: it holds its batch and is never rewritten."""


@dataclass(frozen=True)
class Hold:
    """One active hold: its reason, the failure its record keeps and the revision of the record
    in the store (``None`` when the record cannot be read)."""

    batch_id: str
    reason: str
    code: str
    step: str
    message: str
    revision: int | None

    @property
    def releasable(self) -> bool:
        """Whether ``recovery resume`` may release it once its cause is gone."""
        return self.revision is not None

    @property
    def remedy(self) -> str:
        template = HOLD_REMEDY if self.releasable else UNREADABLE_REMEDY
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

    def error(self) -> OperationalHoldError:
        """The refusal every command that would act on the held batch raises."""
        return OperationalHoldError(
            self.code,
            self.step,
            f"batch {self.batch_id} is held ({self.reason}): {self.message}",
            remedy=self.remedy,
            diagnostics={
                "batch_id": self.batch_id,
                "reason": self.reason,
                "revision": self.revision,
            },
        )


def step_for(reason: str) -> str:
    """The failure step that keeps a hold of ``reason``."""
    if reason not in HOLD_REASONS:
        raise ValueError(f"unknown operational hold reason {reason!r}")
    return HOLD_STEP_PREFIX + reason


def reason_of(step: object) -> str | None:
    """The hold reason a failure step names, or ``None`` for any other failure."""
    if isinstance(step, str) and step.startswith(HOLD_STEP_PREFIX):
        reason = step.removeprefix(HOLD_STEP_PREFIX)
        return reason if reason in HOLD_REASONS else None
    return None


def failure_code(reason: str) -> str:
    """The failure code a hold of ``reason`` records."""
    step_for(reason)
    return "integrity-failure"


def of_record(batch_id: str, record: JsonObject, revision: int | None) -> Hold | None:
    """The hold a record of the store keeps, ``None`` when it keeps none.

    A record outside ``operationalHold.v1`` raises ``UnreadableHold``: it is never read with
    defaults filled in."""
    failure = record.get("failure")
    if (
        record.get("contract") != HOLD_CONTRACT
        or record.get("batch_id") != batch_id
        or record.get("state") not in HOLD_STATES
        or not isinstance(record.get("updated_at"), str)
    ):
        raise UnreadableHold(
            f"recovery/hold/{batch_id}.json is not an {HOLD_CONTRACT} record of the batch"
        )
    if record["state"] == "released":
        if failure is not None:
            raise UnreadableHold(f"the released hold of {batch_id} keeps a failure")
        return None
    if not isinstance(failure, dict) or not all(
        isinstance(failure.get(key), str) for key in ("code", "step", "message")
    ):
        raise UnreadableHold(f"the hold of {batch_id} names no failure code, step and message")
    reason = reason_of(failure["step"])
    if reason is None:
        raise UnreadableHold(f"the hold of {batch_id} names no hold reason: {failure['step']!r}")
    return Hold(
        batch_id,
        reason,
        str(failure["code"]),
        str(failure["step"]),
        str(failure["message"]),
        revision,
    )


def hold_relative(batch_id: str) -> str:
    """The path of the hold record of ``batch_id`` in the store."""
    return f"{HOLD_DIRECTORY}/{batch_id}.json"


def current_hold(root: Path, batch_id: str) -> Hold | None:
    """The active hold of ``batch_id``; a hold record that cannot be read holds it too.

    In the file-ledger epoch there is no hold and nothing is read."""
    if not store_is_authority(root):
        return None
    record, revision = _read_store_record(root, hold_relative(batch_id))
    if record is None:
        return None
    try:
        return of_record(batch_id, record, revision)
    except UnreadableHold as exc:
        reason = "unreadable-record"
        return Hold(
            batch_id,
            reason,
            failure_code(reason),
            step_for(reason),
            sanitize_detail(str(exc)),
            None,
        )


def require_no_hold(root: Path, batch_id: str) -> None:
    """Refuse an action on a held batch, with the recovery route as its remedy."""
    held = current_hold(root, batch_id)
    if held is not None:
        raise held.error()


def _ledger(root: Path) -> LifecycleLedger:
    """The ledger whose store unit writes the hold; only the runtime store keeps one."""
    if not store_is_authority(root):
        raise CoordinatorError(
            "the file ledger keeps no operational hold: the runtime store writes it",
            remedy=FILE_EPOCH_REMEDY,
        )
    unit = state_view.current_unit()
    if unit is None or unit.view.root != root:
        raise CoordinatorError(
            "an operational hold is written only inside the store unit of a command",
            remedy="hold or release the batch inside the command's _ledger_lock block",
        )
    return LifecycleLedger(root)


def _write(root: Path, batch_id: str, record: JsonObject, exists: bool) -> None:
    ledger = _ledger(root)
    path = ledger.records_root() / hold_relative(batch_id)
    with _ledger_errors():
        if exists:
            ledger.replace(path, record)
        else:
            ledger.write_immutable(path, record)


def hold(root: Path, batch_id: str, reason: str, detail: str) -> Hold | None:
    """Hold ``batch_id`` for ``reason`` in the caller's store unit and return the hold it staged.

    A repeated hold of the same failure adds no revision and a terminal batch is never held
    (``None``).  A hold record that cannot be read cannot be written: it raises its refusal."""
    failure: JsonObject = {
        "code": failure_code(reason),
        "step": step_for(reason),
        "message": sanitize_detail(detail) or reason,
    }
    _ledger(root)
    batch, _ = _read_store_record(root, f"batches/{batch_id}.json")
    if batch is not None and batch.get("state") in TERMINAL_BATCH_STATES:
        return None
    existing = current_hold(root, batch_id)
    if existing is not None and not existing.releasable:
        raise existing.error()
    stored, revision = _read_store_record(root, hold_relative(batch_id))
    if existing is not None and stored is not None and stored["failure"] == failure:
        return existing
    record: JsonObject = {
        "contract": HOLD_CONTRACT,
        "batch_id": batch_id,
        "state": "held",
        "failure": failure,
        "updated_at": _now(),
    }
    _write(root, batch_id, record, stored is not None)
    return of_record(batch_id, record, 1 if revision is None else revision + 1)


def release(root: Path, batch_id: str) -> JsonObject | None:
    """Release a releasable hold in the caller's store unit and return the record it staged;
    without a hold the stored record is returned unchanged."""
    _ledger(root)
    held = current_hold(root, batch_id)
    if held is None:
        return _read_store_record(root, hold_relative(batch_id))[0]
    if not held.releasable:
        raise CoordinatorError(
            f"the {held.reason} hold of {batch_id} is not released by a recovery command",
            remedy=held.remedy,
        )
    record: JsonObject = {
        "contract": HOLD_CONTRACT,
        "batch_id": batch_id,
        "state": "released",
        "failure": None,
        "updated_at": _now(),
    }
    _write(root, batch_id, record, True)
    return record
