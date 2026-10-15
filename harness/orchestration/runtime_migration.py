"""The system-fields migration of the ledger behind a durable intent (``ledger migrate``).

``generation_migration`` builds and checks a migrated generation; this module selects it through
the ledger-scope record of the runtime state store, so a process that stops at any point leaves an
intent the next ``ledger migrate`` finishes or stops on.  Under the ledger lock it:

1. reconciles an unfinished migration of the ledger scope;
2. does nothing when the selected generation is already migrated under the current rules;
3. refuses while a batch has an unfinished operation: its intent names the old generation;
4. builds and checks the target in a staging directory -- a refusal changes neither store;
5. records the write-once intent and the ``prepared`` operation;
6. renames the staging directory to the target and switches the pointer by compare-and-swap;
7. verifies the selected target, then records ``effect-observed`` and ``ledger-committed``;
8. denies every quarantined batch in the store;
9. commits the operation.

A reconcile finds the pointer on the target and continues from step 7, or on the unchanged
source and rebuilds the target from the current source -- after a stop the lock was released, so
the source may have changed.  A pointer on any other generation is a controlled stop.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import cast

from harness.orchestration.core.runtime_state_contract import QUARANTINE_STEP
from harness.orchestration.core.utils import JsonObject
from harness.orchestration.ledger.field_migration import checked_registry, rules_sha256
from harness.orchestration.ledger.generation_migration import (
    MANIFEST,
    STAGING_PREFIX,
    build,
    current_manifest,
    discard,
    verify,
)
from harness.orchestration.ledger.ledger_ops import _ledger_errors
from harness.orchestration.ledger.lifecycle import (
    GENERATIONS,
    MIGRATION_DIRECTORY,
    POINTER_NAME,
    LifecycleLedger,
)
from harness.orchestration.runtime_intent import (
    audit_id_for,
    canonical_sha256,
    commit_operation,
    first_record,
    mark_ready,
    mark_reconciling,
    next_revision,
    observe_effect,
    suspended_error,
    with_phase,
    write_next,
)
from harness.orchestration.runtime_state_store import (
    RuntimeStateError,
    RuntimeStateStore,
    sanitize_detail,
)

KIND = "migrate"
RESUME_REMEDY = (
    "repeat 'coordinator.py ledger migrate': it reconciles the unfinished migration from its "
    "intent first"
)
STOP_REMEDY = (
    "inspect ledger.json and the generations the evidence names; the migration stays stopped "
    "until the pointer selects its source or its target again"
)
QUARANTINE_MESSAGE = (
    "the system-fields ledger migration quarantined this batch; read it with "
    "'coordinator.py ledger inspect --batch {batch}'"
)
INTERRUPTED = "the ledger migration stopped before its store commit"


class _Stop(Exception):
    """A controlled stop of a reconcile: the failure it records and its remedy."""

    def __init__(self, code: str, step: str, detail: str, remedy: str = STOP_REMEDY):
        super().__init__(detail)
        self.code = code
        self.step = step
        self.detail = detail
        self.remedy = remedy


def migrate_system_fields(
    repo: Path, root: Path, ledger: LifecycleLedger
) -> JsonObject:
    """Migrate the selected generation to the canonical system fields, or finish a migration a
    stopped process left; returns what the migration of the selected generation recorded.  The
    caller holds the ledger lock."""
    store = RuntimeStateStore(repo, root)
    with _ledger_errors():
        rules = rules_sha256(checked_registry())
    record = store.read(None)
    resumed = record is not None and _unfinished(record)
    if record is not None and resumed:
        record = _resume(store, ledger, record)
    elif record is not None and record["state"] in ("suspended", "reconciling"):
        record = mark_ready(
            store, mark_reconciling(store, record, interrupted=INTERRUPTED)
        )
    with _ledger_errors():
        pointer = ledger.pointer()
        if pointer is None:
            return _summary(None, rules, migrated=False)
        source = str(pointer["generation"])
        manifest = current_manifest(ledger, source)
    if manifest is not None:
        return _summary(manifest, rules, migrated=resumed)
    _refuse_unfinished_batches(store)
    for leftover in sorted((ledger.root / GENERATIONS).glob(f"{STAGING_PREFIX}*")):
        with _ledger_errors():
            discard(ledger, leftover.name)
    operation_id = str(uuid.uuid4())
    before = _pointer_sha256(ledger)
    staging = f"{STAGING_PREFIX}{operation_id}"
    with _ledger_errors():
        build(
            ledger,
            source,
            ledger.root / GENERATIONS / staging,
            source_pointer_sha256=before,
            audit_ids=(audit_id_for(operation_id, 0), audit_id_for(operation_id, 1)),
        )
    payload: JsonObject = {
        "operation_id": operation_id,
        "idempotency_key": _key(source, before, rules),
        "kind": KIND,
        "batch_id": None,
        "dispatch_id": None,
        "generation": source,
        "target_generation": _target_name(ledger),
        "staging": staging,
        "source_pointer_sha256": before,
        "rules_sha256": rules,
    }
    record = _prepare(store, record or first_record(store, None), payload)
    step = "migrate:select"
    try:
        _select(ledger, payload)
        step = "migrate:commit"
        _, manifest = _commit(store, ledger, record, payload, rules)
    except Exception as exc:
        raise _suspended(store, step, exc) from exc
    return _summary(manifest, rules, migrated=True)


def _suspended(
    store: RuntimeStateStore, step: str, exc: Exception
) -> RuntimeStateError:
    """Suspend the unfinished migration with the failure ``exc`` caused at ``step``; a
    controlled stop keeps its own code, step and remedy."""
    if isinstance(exc, _Stop):
        return suspended_error(
            store,
            None,
            exc.step,
            exc,
            code=exc.code,
            detail=exc.detail,
            remedy=exc.remedy,
        )
    return suspended_error(store, None, step, exc, remedy=RESUME_REMEDY)


def _unfinished(record: JsonObject) -> bool:
    operation = record["operation"]
    return operation is not None and operation["phase"] != "committed"


def _key(source: str, before: str, rules: str) -> str:
    return canonical_sha256(
        {
            "kind": KIND,
            "generation": source,
            "source_pointer_sha256": before,
            "rules_sha256": rules,
        }
    )


def _pointer_sha256(ledger: LifecycleLedger) -> str:
    return hashlib.sha256(ledger.pointer_path.read_bytes()).hexdigest()


def _selected(ledger: LifecycleLedger) -> str | None:
    """The generation the pointer names, read without validation, ``None`` without one."""
    try:
        pointer: object = json.loads(ledger.pointer_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    generation = pointer.get("generation") if isinstance(pointer, dict) else None
    return generation if isinstance(generation, str) else None


def _target_name(ledger: LifecycleLedger) -> str:
    while True:
        name = f"generation-{uuid.uuid4().hex[:8]}"
        if not (ledger.root / GENERATIONS / name).exists():
            return name


def _summary(manifest: JsonObject | None, rules: str, *, migrated: bool) -> JsonObject:
    quarantine = (
        [] if manifest is None else cast(list[JsonObject], manifest["quarantine"])
    )
    return {
        "migrated": migrated,
        "rules_sha256": rules if manifest is None else manifest["rules_sha256"],
        "mapped": 0 if manifest is None else len(manifest["mapped"]),
        "quarantined": [entry["batch_id"] for entry in quarantine],
        "source_generation": None
        if manifest is None
        else manifest["source_generation"],
    }


def _refuse_unfinished_batches(store: RuntimeStateStore) -> None:
    """A batch operation's intent is bound to the generation it was planned in: a switch under
    it would stop its reconcile, so the migration waits for it."""
    busy = []
    for batch_id in store.batch_ids():
        record = store.read(batch_id)
        if record is not None and (
            _unfinished(record) or record["state"] == "reconciling"
        ):
            busy.append(batch_id)
    if busy:
        raise RuntimeStateError(
            "concurrency-conflict",
            "migrate:batches",
            "batches with an unfinished operation: " + ", ".join(busy),
            remedy="finish or reconcile the listed batches with their next command, then "
            "repeat 'coordinator.py ledger migrate'",
        )


def _prepare(
    store: RuntimeStateStore, current: JsonObject, payload: JsonObject
) -> JsonObject:
    """Persist the write-once intent, then record its operation ``prepared`` over ``current``."""
    ref = store.write_intent(None, payload["operation_id"], payload)
    prepared = next_revision(
        current,
        state="running",
        dispatch_id=None,
        operation={
            "operation_id": payload["operation_id"],
            "kind": KIND,
            "phase": "prepared",
            "idempotency_key": payload["idempotency_key"],
            "expected_before_sha256": payload["source_pointer_sha256"],
            "intended_after_sha256": None,
            "effect": {
                "state": "not-started",
                "evidence_ref": None,
                "observed_at": None,
            },
        },
        ledger={
            "generation": payload["generation"],
            "record_hashes": {POINTER_NAME: payload["source_pointer_sha256"]},
        },
        input_refs=[ref],
        output_refs=[],
        failure=None,
    )
    return write_next(store, current, prepared)


def _select(ledger: LifecycleLedger, payload: JsonObject) -> None:
    """Promote the checked staging directory to the target and select it, while the pointer
    still holds the source bytes."""
    generations = ledger.root / GENERATIONS
    (generations / payload["staging"]).rename(
        generations / payload["target_generation"]
    )
    with _ledger_errors():
        ledger.select_generation(
            payload["target_generation"],
            expected_pointer_sha256=payload["source_pointer_sha256"],
        )


def _commit(
    store: RuntimeStateStore,
    ledger: LifecycleLedger,
    record: JsonObject,
    payload: JsonObject,
    rules: str,
) -> tuple[JsonObject, JsonObject]:
    """Verify the selected target, advance the operation to ``committed`` with every quarantined
    batch denied before the commit; returns the record on disk and the target's manifest."""
    target = payload["target_generation"]
    if _selected(ledger) != target:
        raise _Stop(
            "integrity-failure",
            "migrate:verify",
            f"the ledger pointer does not select the migrated generation {target}",
        )
    path = ledger.root / GENERATIONS / target
    with _ledger_errors():
        manifest = verify(ledger, path, rules)
    if (manifest["source_generation"], manifest["source_pointer_sha256"]) != (
        payload["generation"],
        payload["source_pointer_sha256"],
    ):
        raise _Stop(
            "integrity-failure",
            "migrate:verify",
            f"the migrated generation {target} records another source",
        )
    manifest_ref = f"{GENERATIONS}/{target}/{MIGRATION_DIRECTORY}/{MANIFEST}"
    if record["operation"]["phase"] == "prepared":
        record = observe_effect(store, record, f"{GENERATIONS}/{target}")
    if record["operation"]["phase"] == "effect-observed":
        hashes = {
            POINTER_NAME: _pointer_sha256(ledger),
            manifest_ref: hashlib.sha256(
                (ledger.root / manifest_ref).read_bytes()
            ).hexdigest(),
        }
        record = write_next(
            store,
            record,
            with_phase(
                record,
                "ledger-committed",
                ledger={"generation": target, "record_hashes": hashes},
            ),
        )
    for entry in cast(list[JsonObject], manifest["quarantine"]):
        _deny(store, str(entry["batch_id"]))
    return commit_operation(store, record, [manifest_ref]), manifest


def _deny(store: RuntimeStateStore, batch_id: str) -> None:
    """Hold a quarantined batch ``suspended`` in the store, so no command settles or writes it;
    a completed batch is left alone and a repeated denial adds no revision."""
    failure: JsonObject = {
        "code": "integrity-failure",
        "step": QUARANTINE_STEP,
        "message": sanitize_detail(QUARANTINE_MESSAGE.format(batch=batch_id)),
    }
    record = store.read(batch_id) or first_record(store, batch_id)
    if record["state"] == "completed" or (
        record["state"] == "suspended" and record["failure"] == failure
    ):
        return
    write_next(store, record, next_revision(record, state="suspended", failure=failure))


def _resume(
    store: RuntimeStateStore, ledger: LifecycleLedger, record: JsonObject
) -> JsonObject:
    """Finish the unfinished migration ``record`` holds: from its selected target, or by building
    the target again from the unchanged source; returns the record on disk.  The checks run
    before any write, so a stop found again adds no revision."""
    try:
        payload = _intent(store, record)
        selected = _selected(ledger)
        rebuild = selected != payload["target_generation"]
        if rebuild and (
            not ledger.pointer_path.is_file()
            or _pointer_sha256(ledger) != payload["source_pointer_sha256"]
        ):
            raise _Stop(
                "integrity-failure",
                "reconcile:generation",
                f"the ledger pointer selects {selected}, neither the migration source "
                f"{payload['generation']} nor its target {payload['target_generation']}",
            )
    except _Stop as stop:
        raise _suspended(store, stop.step, stop) from None
    if rebuild:
        _refuse_unfinished_batches(store)
    record = mark_reconciling(store, record, interrupted=INTERRUPTED)
    step = "reconcile:commit"
    try:
        rules = payload["rules_sha256"]
        if rebuild:
            step = "reconcile:rebuild"
            for name in (payload["staging"], payload["target_generation"]):
                with _ledger_errors():
                    discard(ledger, name)
            with _ledger_errors():
                rules = rules_sha256(checked_registry())
                build(
                    ledger,
                    payload["generation"],
                    ledger.root / GENERATIONS / payload["staging"],
                    source_pointer_sha256=payload["source_pointer_sha256"],
                    audit_ids=(
                        audit_id_for(payload["operation_id"], 0),
                        audit_id_for(payload["operation_id"], 1),
                    ),
                )
            _select(ledger, payload)
            step = "reconcile:commit"
        record, _ = _commit(store, ledger, record, payload, rules)
        return mark_ready(store, record)
    except Exception as exc:
        raise _suspended(store, step, exc) from exc


def _intent(store: RuntimeStateStore, record: JsonObject) -> JsonObject:
    """The unfinished migration's intent payload, once its operation, key and generation bind it
    to the ledger-scope record; a mismatch stops."""
    operation = record["operation"]
    refs = record["input_refs"]
    if record["state"] == "ready" or operation["kind"] != KIND or len(refs) != 1:
        raise _Stop(
            "integrity-failure",
            "reconcile:intent",
            "the ledger-scope record holds an operation that is not an unfinished migration",
        )
    try:
        payload = store.read_intent(None, refs[0])
    except RuntimeStateError as exc:
        raise _Stop(exc.code, "reconcile:intent", exc.diagnostics["detail"]) from exc
    bindings = (
        ("operation_id", operation["operation_id"]),
        ("idempotency_key", operation["idempotency_key"]),
        ("kind", KIND),
        ("batch_id", None),
        ("dispatch_id", None),
        ("source_pointer_sha256", operation["expected_before_sha256"]),
        # The record names the source until the ledger is committed, then the target.
        (
            "target_generation"
            if operation["phase"] == "ledger-committed"
            else "generation",
            record["ledger"]["generation"],
        ),
    )
    for key, expected in bindings:
        if payload.get(key) != expected:
            raise _Stop(
                "integrity-failure",
                "reconcile:intent",
                f"the intent payload's {key} does not match the ledger-scope record",
            )
    names = (payload.get("staging"), payload.get("target_generation"))
    if (
        not isinstance(names[0], str)
        or names[0] != f"{STAGING_PREFIX}{operation['operation_id']}"
        or not isinstance(names[1], str)
        or not names[1].startswith("generation-")
        or "/" in names[1]
        or "\\" in names[1]
        or _key(
            payload["generation"],
            payload["source_pointer_sha256"],
            payload.get("rules_sha256", ""),
        )
        != operation["idempotency_key"]
    ):
        raise _Stop(
            "integrity-failure",
            "reconcile:intent",
            "the intent payload names invalid generations or rules",
        )
    return payload
