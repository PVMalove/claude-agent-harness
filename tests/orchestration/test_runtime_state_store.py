"""Runtime state store (runtimeState.v1): schema, record rules, binding, locking and invariants."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator

import pytest

from harness.json_types import JsonObject
from harness.orchestration import runtime_state_store as store
from harness.orchestration.core.runtime_state_contract import (
    RUNTIME_STATE_CONTRACT,
    RUNTIME_TRANSITIONS,
    RUNTIME_VOCABULARIES,
    transition_problem,
)
from harness.orchestration.core.system_fields import enum_problem, load_registry

HASH = "a" * 64
BATCH = "batch-0123abcd"


def _identity(batch_id: str = BATCH) -> JsonObject:
    return {
        "repo_root": "/repo",
        "git_common_dir": "/repo/.git",
        "state_root": "/repo/.harness/orchestration/state",
        "namespace": "default",
        "batch_id": batch_id,
    }


def _record(**changes: object) -> JsonObject:
    record: JsonObject = {
        "contract": RUNTIME_STATE_CONTRACT,
        "identity": _identity(),
        "revision": 1,
        "updated_at": "2026-10-14T09:00:00+00:00",
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
    record.update(changes)
    return record


def _operation(
    phase: str = "prepared", effect: str = "not-started", operation_id: str = "op-1"
) -> JsonObject:
    return {
        "operation_id": operation_id,
        "kind": "dispatch-send",
        "phase": phase,
        "idempotency_key": "key-1",
        "expected_before_sha256": HASH,
        "intended_after_sha256": None,
        "effect": {"state": effect, "evidence_ref": None, "observed_at": None},
    }


def _next(previous: JsonObject, **changes: object) -> JsonObject:
    record = copy.deepcopy(previous)
    record["revision"] = previous["revision"] + 1
    record.update(changes)
    return record


def _settled_ledger() -> JsonObject:
    return {"generation": "generation-1", "record_hashes": {"batches/b.json": HASH}}


def _system_nodes(node: object) -> Iterator[JsonObject]:
    if isinstance(node, dict):
        if store.SYSTEM_FIELD in node or "enum" in node:
            yield node
        for child in node.values():
            yield from _system_nodes(child)
    elif isinstance(node, list):
        for child in node:
            yield from _system_nodes(child)


# --- Schema and record rules -------------------------------------------------------------------


def test_a_complete_record_is_valid_and_every_nullable_field_takes_null() -> None:
    full = _record(
        stage="developer",
        dispatch_id="dispatch-0abc",
        operation=_operation(),
        policy_snapshot_sha256=HASH,
        input_refs=["brief.json"],
        output_refs=["report.json"],
        last_confirmed_boundary={
            "revision": 1,
            "state": "ready",
            "confirmed_at": "2026-10-14T09:00:00Z",
        },
        failure={"code": "execution-failure", "step": "send", "message": "exit 1"},
    )

    assert store.validate_record(_record(), None) == []
    assert store.validate_record(full, None) == []


def test_every_record_field_is_required() -> None:
    for key in _record():
        record = _record()
        del record[key]
        problems = store.validate_record(record, None)
        assert problems, key
        if key != "contract":
            assert any(key in problem for problem in problems), key


def test_unknown_fields_are_refused_at_every_level() -> None:
    nested = _record(operation=_operation())
    nested["operation"]["effect"]["extra"] = True

    assert store.validate_record(_record(extra=1), None)
    assert store.validate_record(nested, None)
    assert store.validate_record(_record(identity={**_identity(), "host": "x"}), None)


def test_a_missing_or_foreign_contract_is_a_compatibility_problem_without_defaults() -> (
    None
):
    record = _record()
    del record["contract"]

    assert store.compatibility_problem(record) is not None
    assert store.compatibility_problem(_record(contract="runtimeState.v0")) is not None
    assert store.compatibility_problem(_record()) is None
    assert "contract" in store.validate_record(record, None)[0]


@pytest.mark.parametrize(
    ("changes", "field_id"),
    [
        ({"state": "paused"}, "runtime.state"),
        ({"operation": _operation(phase="done")}, "runtime.operation_phase"),
        ({"operation": {**_operation(), "kind": "push"}}, "runtime.operation_kind"),
        ({"operation": _operation(effect="maybe")}, "runtime.effect_state"),
        (
            {"failure": {"code": "oops", "step": "send", "message": "x"}},
            "runtime.failure_code",
        ),
    ],
)
def test_an_unknown_enum_value_names_the_allowed_values(
    changes: JsonObject, field_id: str
) -> None:
    problems = store.validate_record(_record(**changes), None)

    assert len(problems) == 1
    assert all(value in problems[0] for value in RUNTIME_VOCABULARIES[field_id])


def test_an_enum_spelling_variant_is_refused_with_the_exact_spelling() -> None:
    problems = store.validate_record(_record(state="Ready"), None)

    assert problems == [
        enum_problem("record.state", "Ready", RUNTIME_VOCABULARIES["runtime.state"])
    ]
    assert "write exactly 'ready'" in problems[0]


def test_every_schema_enum_spells_its_registry_field() -> None:
    registry = load_registry()
    nodes = list(_system_nodes(store._schema()))

    assert {node[store.SYSTEM_FIELD] for node in nodes} == set(RUNTIME_VOCABULARIES)
    for node in nodes:
        assert sorted(node["enum"]) == sorted(
            registry.fields[node[store.SYSTEM_FIELD]].values
        ), node[store.SYSTEM_FIELD]


def test_the_contract_constant_matches_the_schema() -> None:
    schema = json.loads(store.SCHEMA_PATH.read_text(encoding="utf-8"))

    assert schema["properties"]["contract"] == {"const": RUNTIME_STATE_CONTRACT}
    assert schema["$id"] == RUNTIME_STATE_CONTRACT


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"revision": 2}, "revision 1"),
        ({"state": "running"}, "state 'ready'"),
        ({"operation": _operation(phase="effect-observed")}, "prepared"),
        ({"updated_at": "yesterday"}, "ISO-8601"),
        ({"updated_at": "2026-10-14T09:00:00"}, "timezone"),
    ],
)
def test_creation_rules(changes: JsonObject, fragment: str) -> None:
    problems = store.validate_record(_record(**changes), None)

    assert any(fragment in problem for problem in problems), problems


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (before, after)
        for before in RUNTIME_VOCABULARIES["runtime.state"]
        for after in RUNTIME_VOCABULARIES["runtime.state"]
    ],
)
def test_state_follows_the_transition_table(before: str, after: str) -> None:
    previous = _record(state=before, revision=3)
    problems = store.validate_record(_next(previous, state=after), previous)

    allowed = before != "completed" and (
        after == before or after in RUNTIME_TRANSITIONS[before]
    )
    assert (problems == []) is allowed, problems


def test_the_revision_advances_by_exactly_one_and_identity_is_immutable() -> None:
    previous = _record(revision=2)

    assert store.validate_record(_next(previous, revision=4), previous)
    assert store.validate_record(_next(previous, revision=2), previous)
    assert store.validate_record(
        _next(previous, identity=_identity("batch-ffff")), previous
    )


def test_an_operation_keeps_its_identity_and_follows_its_tables() -> None:
    previous = _record(operation=_operation())
    observed = _next(previous, operation=_operation("effect-observed", "confirmed"))
    changed_key = _next(previous, operation={**_operation(), "idempotency_key": "k2"})
    backwards = _next(observed, operation=_operation("prepared", "confirmed"))
    unknown_back = _next(observed, operation=_operation("effect-observed", "unknown"))

    assert store.validate_record(observed, previous) == []
    assert "operation idempotency_key is immutable" in store.validate_record(
        changed_key, previous
    )
    assert store.validate_record(backwards, observed)
    assert store.validate_record(unknown_back, observed)


def test_a_new_operation_waits_for_the_previous_commit() -> None:
    settled = {"ledger": _settled_ledger()}
    open_op = _record(operation=_operation(), **settled)
    done = _record(operation=_operation("committed", "confirmed"), **settled)
    following = _operation(operation_id="op-2")

    assert store.validate_record(_next(open_op, operation=following), open_op)
    assert store.validate_record(_next(open_op, operation=None), open_op)
    assert store.validate_record(_next(done, operation=following), done) == []
    assert store.validate_record(_next(done, operation=None), done) == []


@pytest.mark.parametrize("phase", ["ledger-committed", "committed"])
def test_legacy_metadata_never_counts_as_a_settled_effect(phase: str) -> None:
    unknown = _record(operation=_operation(phase, "unknown"), ledger=_settled_ledger())
    no_hashes = _record(operation=_operation(phase, "confirmed"))
    settled = _record(
        operation=_operation(phase, "confirmed"), ledger=_settled_ledger()
    )

    assert any("confirmed effect" in p for p in store.validate_record(unknown, None))
    assert any("record hashes" in p for p in store.validate_record(no_hashes, None))
    assert store._settled_problems(settled) == []


@pytest.mark.parametrize(
    "message",
    ["token: ghp_" + "x" * 30, "cannot open /home/user/secret.json", "x" * 300],
)
def test_a_failure_message_must_already_be_sanitized(message: str) -> None:
    failure = {"code": "execution-failure", "step": "send", "message": message}

    assert any(
        "sanitized" in problem
        for problem in store.validate_record(_record(failure=failure), None)
    )


def test_sanitize_detail_masks_secrets_and_absolute_paths_and_is_idempotent() -> None:
    text = (
        "read /var/x/state.json and C:\\Users\\a\\b failed: password=hunter2 ghp_"
        + ("y" * 30)
    )
    cleaned = store.sanitize_detail(text)

    assert "/var/x" not in cleaned and "C:\\Users" not in cleaned
    assert "hunter2" not in cleaned and "ghp_" not in cleaned
    assert store.sanitize_detail(cleaned) == cleaned
    assert store.sanitize_detail("orchestration/runtime-state/x.json") == (
        "orchestration/runtime-state/x.json"
    )


def test_transition_problem_refuses_unknown_tables_and_sources() -> None:
    assert transition_problem("runtime.state", "ready", "ready") is None
    assert transition_problem("runtime.state", "ready", "running") is None
    assert "terminal" in str(transition_problem("runtime.state", "completed", "ready"))
    assert transition_problem("runtime.state", "paused", "ready") is not None
    assert transition_problem("batch.state", "active", "paused") is not None
    assert transition_problem("runtime.effect_state", "unknown", "confirmed") is None
    assert (
        transition_problem("runtime.operation_phase", "committed", "prepared")
        is not None
    )
