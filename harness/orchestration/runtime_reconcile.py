"""Reconcile an unfinished composite ledger write from its durable intent.

``settle`` runs under the ledger lock at the start of every command that performs a composite
write, before the command reads the batch.  An operation the runtime state store holds as
unfinished is checked against its intent payload, read-only, and then rolled forward: a planned
record that is still absent or unchanged is written with a compare-and-swap, a written record
whose audit event is missing gets that event, and the store reaches ``committed`` only after
every record and event is verified.  An operation already committed is left alone, so repeating
``settle`` changes nothing.

An unfinished hand-off (``dispatch send``) is finished from the observation it persisted; without
one, its effect is proved read-only or the reconcile stops with ``ambiguous-effect``.  The
hand-off itself is never repeated, so no second worker starts for the same dispatch.

Reconciliation writes only the paths and audit IDs of the verified intent: it records no
decision or approval, never creates over an existing file and never replaces without the
planned digest.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from harness.orchestration.core import utils
from harness.orchestration.core.constants import DispatchState
from harness.orchestration import operational_hold
from harness.orchestration.core.utils import JsonObject
from harness.errors import HarnessError
from harness.orchestration.ledger.lifecycle import (
    LedgerError,
    LedgerRecordVO,
    LifecycleLedger,
    audit_details,
)
from harness.orchestration.runtime_access import RuntimeAccessStatus
from harness.orchestration.runtime_effect import (
    CHANNELS,
    KIND,
    OUTCOMES,
    EffectResult,
    build_observation,
    dispatch_send_records,
    observation_id,
    query_launch,
    record_paths,
    store_observation,
)
from harness.orchestration.runtime_intent import (
    WRITE_MODES,
    apply_step,
    audit_id_for,
    canonical_sha256,
    content_bytes,
    digest,
    error_detail,
    failure_code,
    idempotency_key,
    mark_ready,
    mark_reconciling,
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

INTERRUPTED = "the composite write stopped before its store commit"

AMBIGUOUS_REMEDY = (
    "stop the worker of {dispatch} or let its adapter answer observe, then run dispatch cancel "
    "--dispatch {dispatch} --runtime-stopped with an approval; the next command reconciles the "
    "hand-off and never starts the worker a second time"
)


class _Stop(Exception):
    """A controlled stop: why an intent cannot be reconciled, with its evidence and the remedy
    a person applies before the next command reconciles again."""

    def __init__(
        self,
        code: str,
        step: str,
        detail: str,
        evidence: JsonObject | None = None,
        *,
        remedy: str = STOP_REMEDY,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.step = step
        self.detail = detail
        self.evidence = evidence or {}
        self.remedy = remedy


def short(sha256: object) -> str | None:
    """A hash shortened for a diagnostic; ``None`` stays ``None`` (an absent file)."""
    return sha256[:12] if isinstance(sha256, str) else None


def settle(repo: Path, root: Path, ledger: LifecycleLedger, batch_id: str) -> bool:
    """Finish the batch's unfinished composite write or hand-off, if there is one; ``True``
    when an operation was rolled forward, so ledger records the caller read may be stale.

    A violated hash, an unknown state or a concurrent writer is a controlled stop: the store
    is suspended with the failure, nothing is retried, and the same stop found again by the
    next read-only check raises without a new revision.  A held batch -- one the ledger
    migration quarantined, or one an operational hold suspended -- is never released here: it
    raises, and its record keeps its revision until ``recovery resume`` releases a hold."""
    store = RuntimeStateStore(repo, root)
    record = store.read(batch_id)
    if record is None:
        return False
    if (held := operational_hold.of_record(batch_id, record)) is not None:
        raise held.error()
    operation = record["operation"]
    if operation is None or operation["phase"] == "committed":
        if record["state"] in ("suspended", "reconciling"):
            mark_ready(store, mark_reconciling(store, record, interrupted=INTERRUPTED))
        return False
    observation: JsonObject | None = None
    try:
        records = ledger.records_root()
        payload = _intent(store, ledger, record)
        if operation["kind"] == KIND:
            hand_off = _HandOff(store, ledger, repo, records, record, payload)
            observation = hand_off.observation()
            steps = observation["steps"]
        else:
            steps = _composite_steps(record, payload)
        pending = _pending(ledger, records, steps)
    except _Stop as stop:
        raise _stopped(store, batch_id, stop) from None
    except LedgerError as exc:
        broken = _Stop("integrity-failure", "reconcile:ledger", exc.message)
        raise _stopped(store, batch_id, broken) from exc
    record = mark_reconciling(store, record, interrupted=INTERRUPTED)
    step_name = "reconcile:intent"
    try:
        if observation is not None:
            step_name = "reconcile:effect-observe"
            record = store_observation(store, record, observation)
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
    mark_ready(store, record)
    return True


def _stopped(store: RuntimeStateStore, batch_id: str, stop: _Stop) -> RuntimeStateError:
    """Record a controlled stop and return the error to raise.  The same failure already
    recorded on a suspended record adds no revision; a ``ready`` record keeps its revision as
    it is, because only a quarantine moves an idle record to ``suspended``."""
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
        stop.code, stop.step, stop.detail, remedy=stop.remedy, durable_revision=durable
    )
    error.diagnostics["evidence"] = stop.evidence
    return error


def _step_name(step: JsonObject) -> str:
    return f"reconcile:{str(step.get('path', '')).split('/')[0] or 'intent'}"


def _intent(
    store: RuntimeStateStore, ledger: LifecycleLedger, record: JsonObject
) -> JsonObject:
    """The unfinished operation's intent payload, once it is bound to the store record by its
    operation, idempotency key and generation; a mismatch stops."""
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
    return payload


def _composite_steps(record: JsonObject, payload: JsonObject) -> list[JsonObject]:
    """A composite write's steps, once every hash binds them to the store record."""
    operation = record["operation"]
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


@dataclass(frozen=True)
class _HandOff:
    """An unfinished hand-off under reconciliation: the stores, the repository its adapter runs
    in, the selected generation's records, and the store record with its checked intent."""

    store: RuntimeStateStore
    ledger: LifecycleLedger
    repo: Path
    records: Path
    record: JsonObject
    payload: JsonObject

    @property
    def dispatch_id(self) -> str:
        return str(self.payload["dispatch_id"])

    def observation(self) -> JsonObject:
        """The hand-off's observation: the one it stored, or one this read-only check proves;
        an effect nothing proves stops, and the hand-off is never repeated."""
        before = self._intended_before()
        current: JsonObject = {path: digest(self.records / path) for path in before}
        stored = self._stored_observation(current)
        if stored is not None:
            return stored
        changed = [path for path in before if current[path] != before[path]]
        _, status_path = record_paths(self.payload["batch_id"], self.dispatch_id)
        status = LifecycleLedger.read_record_lenient(self.records / status_path) or {}
        state = self.record["operation"]["effect"]["state"]
        if state == "not-started" and (
            not changed or status.get("state") == DispatchState.CANCELLED
        ):
            return self._proved(EffectResult("unchanged", "not-started"))
        if state == "unknown" and (not changed or _retired(status)):
            return self._queried(status, changed=bool(changed))
        path = changed[0] if changed else next(iter(before))
        raise _Stop(
            "integrity-failure",
            "reconcile:effect",
            f"the {state} hand-off of {self.dispatch_id} has no observation, and {path} "
            "does not prove its outcome",
            {
                "path": path,
                "expected_before": short(before[path]),
                "actual": short(current[path]),
            },
        )

    def _intended_before(self) -> JsonObject:
        """The record hashes the hand-off was planned over, once its intent payload is bound
        to the store record; a mismatch stops."""
        operation = self.record["operation"]
        effect, before = self.payload.get("effect"), self.payload.get("before")
        if (
            not isinstance(effect, dict)
            or effect.get("channel") not in CHANNELS
            or not isinstance(before, dict)
            or sorted(before)
            != sorted(record_paths(self.payload["batch_id"], self.dispatch_id))
            or idempotency_key(KIND, self.dispatch_id, {"effect": effect})
            != operation["idempotency_key"]
            or canonical_sha256(before) != operation["expected_before_sha256"]
        ):
            raise _Stop(
                "integrity-failure",
                "reconcile:intent",
                "the hand-off intent payload does not match the runtime state record",
            )
        return before

    def _queried(self, status: JsonObject, *, changed: bool) -> JsonObject:
        """The observation of a hand-off whose worker may have started, as its adapter or
        provider answers; a live worker against the outcome, or no answer, stops."""
        effect, operation = self.payload["effect"], self.record["operation"]
        launch = query_launch(self.repo, self.records, self.payload)
        adapter = effect["channel"] == "adapter"
        source = "adapter-observe" if adapter else "provider-observe"
        if launch is not None and launch.launched and not changed:
            access: JsonObject = {
                "status": RuntimeAccessStatus.LEGACY_INHERIT
                if adapter
                else RuntimeAccessStatus.UNVERIFIED,
                "reconciled_launch": {"source": source, "launch_id": launch.launch_id},
            }
            batch_path, _ = record_paths(self.payload["batch_id"], self.dispatch_id)
            changes = dispatch_send_records(
                "dispatched",
                _ledger_record(self.records, batch_path),
                status,
                effect,
                access,
                utils._now(),
            )
            return self._proved(EffectResult("dispatched", source, access), changes)
        evidence: JsonObject = {
            "dispatch_id": self.dispatch_id,
            "channel": effect["channel"],
            "marked_at": operation["effect"]["observed_at"],
            "observation": None if launch is None else asdict(launch),
        }
        remedy = AMBIGUOUS_REMEDY.format(dispatch=self.dispatch_id)
        if launch is not None and launch.live:
            raise _Stop(
                "concurrency-conflict",
                "reconcile:effect",
                f"a live worker of {self.dispatch_id} contradicts the observed outcome",
                evidence,
                remedy=remedy,
            )
        if changed:
            return self._proved(EffectResult("unchanged", "human-retired"))
        if launch is not None:
            return self._proved(EffectResult("unchanged", source))
        raise _Stop(
            "ambiguous-effect",
            "reconcile:effect",
            f"the hand-off of {self.dispatch_id} may have started a worker and nothing "
            "proves whether it did",
            evidence,
            remedy=remedy,
        )

    def _proved(
        self, result: EffectResult, changes: list[LedgerRecordVO] | None = None
    ) -> JsonObject:
        """The observation this reconcile proved; a ledger that refuses its steps stops."""
        try:
            return build_observation(
                self.ledger,
                self.store,
                self.payload,
                result,
                changes or [],
                utils._now(),
            )
        except HarnessError as exc:
            raise _Stop("integrity-failure", "reconcile:effect", exc.message) from exc

    def _stored_observation(self, current: JsonObject) -> JsonObject | None:
        """The observation the hand-off stored, bound to its intent and the store record, or
        ``None`` when none was stored; a mismatch stops."""
        operation, payload = self.record["operation"], self.payload
        batch_id = str(payload["batch_id"])
        ref = self.store.intent_ref(batch_id, observation_id(operation["operation_id"]))
        effect = operation["effect"]
        if effect["evidence_ref"] not in (None, ref):
            raise _Stop(
                "integrity-failure",
                "reconcile:observation",
                "the confirmed effect names another observation payload",
            )
        if (
            effect["state"] != "confirmed"
            and not (self.store.location.directory / ref).is_file()
        ):
            return None
        try:
            observation = self.store.read_intent(batch_id, ref)
        except RuntimeStateError as exc:
            raise _Stop(
                exc.code, "reconcile:observation", exc.diagnostics["detail"]
            ) from exc
        steps, hashes = observation.get("steps"), observation.get("record_hashes")
        if (
            observation.get("operation_id") != payload["operation_id"]
            or observation.get("generation") != payload["generation"]
            or observation.get("outcome") not in OUTCOMES
            or not isinstance(steps, list)
            or not isinstance(hashes, dict)
            or sorted(hashes) != sorted(current)
            or (
                operation["phase"] != "prepared"
                and self.record["ledger"]["record_hashes"] != hashes
            )
        ):
            raise _Stop(
                "integrity-failure",
                "reconcile:observation",
                "the observation payload does not match the hand-off intent and runtime "
                "state",
            )
        for index, step in enumerate(steps):
            problem = _step_shape_problem(operation["operation_id"], index, step)
            if problem is None and (
                step["mode"] != "replace"
                or payload["before"].get(step["path"]) != step["before_sha256"]
                or hashes[step["path"]] != step["after_sha256"]
            ):
                problem = (
                    f"observation step {index} does not follow the hand-off intent"
                )
            if problem is not None:
                raise _Stop("integrity-failure", "reconcile:observation", problem)
        planned = {step["path"] for step in steps}
        for path, sha256 in hashes.items():
            if path not in planned and current[path] != sha256:
                raise _Stop(
                    "integrity-failure",
                    "reconcile:observation",
                    f"{path} changed after the hand-off was observed",
                    {
                        "path": path,
                        "expected": short(sha256),
                        "actual": short(current[path]),
                    },
                )
        return observation


def _retired(status: JsonObject) -> bool:
    """Whether a person cancelled the dispatch and confirmed its worker runtime stopped."""
    cancellation = status.get("cancellation")
    return (
        status.get("state") == DispatchState.CANCELLED
        and isinstance(cancellation, dict)
        and cancellation.get("runtime_stopped") is True
        and isinstance(cancellation.get("approved_by"), str)
        and bool(cancellation["approved_by"].strip())
    )


def _ledger_record(records: Path, path: str) -> JsonObject:
    record = LifecycleLedger.read_record_lenient(records / path)
    if record is None:
        raise _Stop("integrity-failure", "reconcile:effect", f"{path} cannot be read")
    return record


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
