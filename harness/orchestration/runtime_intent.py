"""Durable intent of a composite local ledger write (``checkpoint``, ``report-submit``).

A composite write changes several ledger records and appends one audit event per record.  Before
the first of them, ``CompositeWrite.commit`` persists the whole plan as a write-once payload in
the runtime state store and records the operation as ``prepared``; the store reaches
``committed`` only after every record and its audit event are read back and verified.  A process
that stops in between leaves an intent that ``runtime_reconcile`` finishes or stops on.

The bridge between the two stores lives here because the runtime state store never imports the
ledger.  Nothing here issues an approval: a composite write carries no decision.
"""

from __future__ import annotations

import copy
import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from harness.errors import INTERNAL_INVARIANT_REMEDY
from harness.orchestration.core import utils
from harness.orchestration.core.runtime_state_contract import RUNTIME_STATE_CONTRACT
from harness.orchestration.core.utils import CoordinatorError, JsonObject, _canonical
from harness.orchestration.ledger.ledger_ops import _ledger_errors
from harness.orchestration.ledger.lifecycle import (
    GENERATIONS,
    LedgerRecordVO,
    LedgerWriteConflict,
    LifecycleLedger,
    audit_details,
)
from harness.orchestration.runtime_state_store import (
    RuntimeStateError,
    RuntimeStateStore,
    sanitize_detail,
)

# Fixed forever: an audit ID derived from it must name the same event in every later process.
AUDIT_NAMESPACE = uuid.UUID("6d1c5b0e-3f4a-4b8e-9a51-2f0c7d9e8a14")
WRITE_MODES = ("create", "create-artifact", "replace")
RECONCILE_REMEDY = "repeat the command: it reconciles the unfinished composite write from its intent first"


def audit_id_for(operation_id: str, index: int) -> str:
    """The deterministic audit event ID of step ``index`` of ``operation_id``."""
    return f"audit-{uuid.uuid5(AUDIT_NAMESPACE, f'{operation_id}:{index}')}"


def idempotency_key(kind: str, dispatch_id: str, request: JsonObject) -> str:
    """The key of a composite write, derived from its request and never from generated IDs."""
    return canonical_sha256({"kind": kind, "dispatch_id": dispatch_id, **request})


def canonical_sha256(value: JsonObject) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def content_bytes(mode: str, content: object) -> bytes:
    """The exact bytes the ledger writes for ``content`` in ``mode``."""
    if mode == "create-artifact":
        return cast(str, content).encode("utf-8")
    return _canonical(cast(JsonObject, content)).encode("utf-8")


def digest(path: Path) -> str | None:
    """The SHA-256 of a ledger file's bytes, ``None`` when it does not exist."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def operation_hashes(steps: list[JsonObject]) -> tuple[str, str]:
    """``expected_before_sha256`` and ``intended_after_sha256`` of a plan's steps."""
    before: JsonObject = {step["path"]: step["before_sha256"] for step in steps}
    after: JsonObject = {step["path"]: step["after_sha256"] for step in steps}
    return canonical_sha256(before), canonical_sha256(after)


def apply_step(ledger: LifecycleLedger, root: Path, step: JsonObject) -> None:
    """Write one planned step through the ledger under its deterministic audit ID; a replace is a
    compare-and-swap against the planned digest."""
    path = root / step["path"]
    audit_id = step["audit"]["audit_id"]
    if step["mode"] == "create":
        ledger.write_immutable(path, step["content"], audit_id=audit_id)
    elif step["mode"] == "create-artifact":
        ledger.write_artifact(path, step["content"], audit_id=audit_id)
    else:
        ledger.replace(
            path,
            step["content"],
            audit_id=audit_id,
            expected_before_sha256=step["before_sha256"],
        )


def step_problem(ledger: LifecycleLedger, root: Path, step: JsonObject) -> str | None:
    """Why a step that should be applied is not: its record bytes or its audit event differ
    from the plan; ``None`` when both are exactly as planned."""
    if digest(root / step["path"]) != step["after_sha256"]:
        return f"{step['path']} does not hold the planned content"
    audit = step["audit"]
    event = ledger.read_audit(audit["audit_id"])
    if event is None:
        return f"the audit event of {step['path']} is missing"
    if (
        event.get("action") != audit["action"]
        or event.get("details") != audit["details"]
    ):
        return f"the audit event of {step['path']} records another write"
    return None


def next_revision(record: JsonObject, **changes: object) -> JsonObject:
    """``record`` one revision later with ``changes`` applied."""
    following = copy.deepcopy(record)
    following.update(changes)
    following["revision"] = int(record["revision"]) + 1
    following["updated_at"] = utils._now()
    return following


def with_phase(record: JsonObject, phase: str, **changes: object) -> JsonObject:
    operation = copy.deepcopy(record["operation"])
    operation["phase"] = phase
    return next_revision(record, operation=operation, **changes)


def settle_phases(
    store: RuntimeStateStore, record: JsonObject, steps: list[JsonObject]
) -> JsonObject:
    """Advance a verified operation through the remaining phases up to ``committed``; the store
    commit is the last write.  Returns the record now on disk."""
    if record["operation"]["phase"] == "prepared":
        effect: JsonObject = {
            "state": "confirmed",
            "evidence_ref": f"{GENERATIONS}/{record['ledger']['generation']}",
            "observed_at": utils._now(),
        }
        record = write_next(
            store, record, with_phase(record, "effect-observed"), effect
        )
    if record["operation"]["phase"] == "effect-observed":
        record = write_next(store, record, with_phase(record, "ledger-committed"))
    if record["operation"]["phase"] == "ledger-committed":
        outputs = sorted(
            [step["path"] for step in steps]
            + [f"audit/{step['audit']['audit_id']}.json" for step in steps]
        )
        committed = with_phase(record, "committed", output_refs=outputs)
        committed["last_confirmed_boundary"] = {
            "revision": committed["revision"],
            "state": committed["state"],
            "confirmed_at": committed["updated_at"],
        }
        record = write_next(store, record, committed)
    return record


def write_next(
    store: RuntimeStateStore,
    current: JsonObject,
    following: JsonObject,
    effect: JsonObject | None = None,
) -> JsonObject:
    if effect is not None:
        following["operation"]["effect"] = effect
    store.write(following, expected_revision=int(current["revision"]))
    return following


def failure_code(exc: BaseException) -> str:
    """The runtime failure code of an error that stopped a composite write."""
    if isinstance(exc, LedgerWriteConflict):
        return "concurrency-conflict"
    if isinstance(exc, RuntimeStateError):
        return exc.code
    return "execution-failure"


def error_detail(exc: BaseException) -> str:
    return sanitize_detail(
        getattr(exc, "message", None) or str(exc) or type(exc).__name__
    )


def suspend(store: RuntimeStateStore, batch_id: str, failure: JsonObject) -> int | None:
    """Best effort: move an unfinished operation to ``suspended`` with ``failure`` and return
    the durable revision; a store that cannot take the write keeps its last revision."""
    try:
        record = store.read(batch_id)
        if record is None:
            return None
        operation = record["operation"]
        if (
            operation is None
            or operation["phase"] == "committed"
            or record["state"] not in ("running", "reconciling")
        ):
            return int(record["revision"])
        suspended = next_revision(record, state="suspended", failure=failure)
        return store.write(
            suspended, expected_revision=int(record["revision"])
        ).revision
    except Exception:  # noqa: BLE001 -- the original failure is the one to report
        return None


def suspended_error(
    store: RuntimeStateStore,
    batch_id: str,
    step: str,
    exc: BaseException,
    *,
    code: str | None = None,
    detail: str | None = None,
    remedy: str = RECONCILE_REMEDY,
) -> RuntimeStateError:
    """Suspend the unfinished operation with the failure ``exc`` caused at ``step`` and return
    the error to raise; ``code`` and ``detail`` replace the ones derived from ``exc``."""
    code = code or failure_code(exc)
    detail = sanitize_detail(detail) if detail else error_detail(exc)
    durable = suspend(
        store, batch_id, {"code": code, "step": step, "message": detail or code}
    )
    return RuntimeStateError(
        code, step, detail, remedy=remedy, durable_revision=durable
    )


def finish(
    ledger: LifecycleLedger,
    store: RuntimeStateStore,
    root: Path,
    record: JsonObject,
    steps: list[JsonObject],
) -> JsonObject:
    """Write every planned step of the operation ``record`` holds, verify them and commit the
    store; a failure suspends the operation for the next command to reconcile."""
    step_name = "intent"
    try:
        for step in steps:
            step_name = f"ledger:{step['path'].split('/')[0]}"
            apply_step(ledger, root, step)
        step_name = "verify"
        for step in steps:
            if (problem := step_problem(ledger, root, step)) is not None:
                raise RuntimeStateError(
                    "integrity-failure",
                    step_name,
                    problem,
                    remedy="inspect the ledger record and its audit event",
                )
        step_name = "store-commit"
        return settle_phases(store, record, steps)
    except Exception as exc:
        batch_id = record["identity"]["batch_id"]
        raise suspended_error(store, batch_id, step_name, exc) from exc


@dataclass(frozen=True)
class _Pending:
    mode: str
    path: Path
    content: JsonObject | str


class CompositeWrite:
    """Plan a composite ledger write, then perform it behind a durable intent.

    The write methods mirror the ledger operations they replace and only collect the plan;
    nothing is written before ``commit``.  ``commit`` runs under the ledger lock the caller
    already holds."""

    def __init__(
        self,
        ledger: LifecycleLedger,
        store: RuntimeStateStore,
        *,
        batch_id: str,
        dispatch_id: str,
        kind: str,
        idempotency_key: str,
    ) -> None:
        self.ledger = ledger
        self.store = store
        self.batch_id = batch_id
        self.dispatch_id = dispatch_id
        self.kind = kind
        self.idempotency_key = idempotency_key
        self._pending: list[_Pending] = []

    def write_record(self, record: LedgerRecordVO) -> None:
        with _ledger_errors():
            self._add("create", self.ledger.record_path(record), record.to_dict())

    def write_immutable(self, path: Path, value: JsonObject) -> None:
        self._add("create", path, value)

    def write_artifact(self, path: Path, value: str) -> None:
        self._add("create-artifact", path, value)

    def replace_record(self, record: LedgerRecordVO) -> None:
        with _ledger_errors():
            self._add("replace", self.ledger.record_path(record), record.to_dict())

    def _add(self, mode: str, path: Path, content: JsonObject | str) -> None:
        if any(pending.path == path for pending in self._pending):
            raise CoordinatorError(
                f"a composite write plans {path.name} twice",
                remedy=INTERNAL_INVARIANT_REMEDY,
            )
        self._pending.append(_Pending(mode, path, content))

    def commit(self) -> None:
        """Persist the intent, write every planned step, verify them and commit the store.

        A refusal before the intent is recorded leaves both stores unchanged; a failure after it
        suspends the operation with its failure, for the next command to reconcile."""
        current, generation, root, steps = self.begin()
        operation_id = str(uuid.uuid4())
        for index, step in enumerate(steps):
            step["audit"]["audit_id"] = audit_id_for(operation_id, index)
        before, after = operation_hashes(steps)
        payload: JsonObject = {
            "operation_id": operation_id,
            "idempotency_key": self.idempotency_key,
            "kind": self.kind,
            "batch_id": self.batch_id,
            "dispatch_id": self.dispatch_id,
            "generation": generation,
            "steps": steps,
        }
        record = self.prepare(
            current,
            payload,
            before,
            after,
            {step["path"]: step["after_sha256"] for step in steps},
        )
        finish(self.ledger, self.store, root, record, steps)

    def begin(self) -> tuple[JsonObject, str, Path, list[JsonObject]]:
        """The store record a new operation follows, the selected generation, its directory and
        the checked steps.  The batch's first store record is created only once the plan passed,
        so a refusal leaves both stores unchanged."""
        existing = self._current_record()
        with _ledger_errors():
            generation, root, steps = self.plan()
        current = self._first_record() if existing is None else existing
        return current, generation, root, steps

    def prepare(
        self,
        current: JsonObject,
        payload: JsonObject,
        before: str,
        after: str | None,
        record_hashes: JsonObject,
    ) -> JsonObject:
        """Persist the intent ``payload``, then record its operation ``prepared`` over
        ``current``; returns the record now on disk."""
        ref = self.store.write_intent(self.batch_id, payload["operation_id"], payload)
        prepared = next_revision(
            current,
            state="running",
            dispatch_id=self.dispatch_id,
            operation={
                "operation_id": payload["operation_id"],
                "kind": self.kind,
                "phase": "prepared",
                "idempotency_key": self.idempotency_key,
                "expected_before_sha256": before,
                "intended_after_sha256": after,
                "effect": {
                    "state": "not-started",
                    "evidence_ref": None,
                    "observed_at": None,
                },
            },
            ledger={
                "generation": payload["generation"],
                "record_hashes": record_hashes,
            },
            input_refs=[ref],
            output_refs=[],
            failure=None,
        )
        return write_next(self.store, current, prepared)

    def _current_record(self) -> JsonObject | None:
        """The store record a new operation follows, ``None`` before the first one; an
        unfinished operation must be reconciled before another one starts."""
        record = self.store.read(self.batch_id)
        operation = None if record is None else record["operation"]
        if record is not None and (
            (operation is not None and operation["phase"] != "committed")
            or record["state"] not in ("ready", "running")
        ):
            raise RuntimeStateError(
                "concurrency-conflict",
                "intent",
                f"batch runtime state is {record['state']!r} with an unfinished operation",
                remedy=RECONCILE_REMEDY,
                durable_revision=int(record["revision"]),
            )
        return record

    def _first_record(self) -> JsonObject:
        """Create the batch's store record, ``ready`` and without an operation."""
        first: JsonObject = {
            "contract": RUNTIME_STATE_CONTRACT,
            "identity": self.store.location.identity(self.batch_id),
            "revision": 1,
            "updated_at": utils._now(),
            "state": "ready",
            "stage": None,
            "dispatch_id": None,
            "operation": None,
            "ledger": {"generation": None, "record_hashes": {}},
            "policy_snapshot_sha256": None,
            "input_refs": [],
            "output_refs": [],
            "last_confirmed_boundary": None,
            "failure": None,
        }
        self.store.write(first, expected_revision=0)
        return first

    def plan(self) -> tuple[str, Path, list[JsonObject]]:
        """The selected generation, its directory and the checked steps; refuses exactly what
        the ledger writes would refuse, before anything is written."""
        pointer = self.ledger.pointer()
        if pointer is None:
            raise RuntimeStateError(
                "integrity-failure",
                "intent",
                "the ledger has no selected generation",
                remedy="initialize the ledger before a composite write",
            )
        generation = str(pointer["generation"])
        root = self.ledger.records_root()
        steps: list[JsonObject] = []
        for index, pending in enumerate(self._pending):
            self.ledger.check_step(pending.mode, pending.path, pending.content)
            relative = pending.path.resolve().relative_to(root).as_posix()
            before_sha256 = digest(pending.path) if pending.mode == "replace" else None
            after_sha256 = hashlib.sha256(
                content_bytes(pending.mode, pending.content)
            ).hexdigest()
            before_record = (
                LifecycleLedger.read_record_lenient(pending.path)
                if pending.mode == "replace"
                else None
            )
            action, details = audit_details(
                pending.mode,
                relative,
                after_sha256,
                before_sha256=before_sha256,
                before=before_record,
                after=pending.content if isinstance(pending.content, dict) else None,
            )
            steps.append(
                {
                    "index": index,
                    "mode": pending.mode,
                    "path": relative,
                    "before_sha256": before_sha256,
                    "after_sha256": after_sha256,
                    "content": pending.content,
                    "audit": {"audit_id": "", "action": action, "details": details},
                }
            )
        return generation, root, steps
