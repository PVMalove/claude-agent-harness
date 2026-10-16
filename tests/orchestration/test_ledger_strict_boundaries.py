"""Strict canonical-value boundaries of the lifecycle ledger: a write accepts only exact machine
values, so free text, another letter case and synonyms never reach a record, a report or an audit
event, while explanations stay in their own text fields."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from harness.json_types import JsonObject
from harness.orchestration.core.utils import CoordinatorError
from harness.orchestration.ledger.field_migration import (
    FIELD_BINDINGS,
    MIGRATION_RULES,
    FieldBinding,
    write_problems,
    write_values,
)
from harness.orchestration.ledger.lifecycle import (
    AUDIT_ACTIONS,
    BatchRecord,
    LedgerError,
    verified_audit,
)
from harness.orchestration.runtime_intent import CompositeWrite
from harness.orchestration.workflow import reports
from tests.orchestration.test_ledger_migration import (
    BATCH_A,
    DISPATCH_A,
    REPORT,
    Scene,
    make_scene,
    tree,
)


@pytest.fixture
def scene(tmp_path: Path) -> Scene:
    return make_scene(tmp_path)


def _record(location: str, value: object) -> JsonObject:
    """A record that holds ``value`` at the bound ``location`` and nothing else."""
    node: object = value
    for part in reversed(location.split(".")):
        key = part.removesuffix("[]")
        node = {key: [node] if part != key else node}
    assert isinstance(node, dict)
    return node


def _variants(binding: FieldBinding) -> list[object]:
    """Values a boundary must refuse at ``binding``: another letter case, surrounding spaces, a
    synonym, free text, a retired legacy spelling and values of other JSON types."""
    canonical = sorted(write_values(binding.field_id))[0]
    candidates: list[object] = [
        canonical.upper(),
        f" {canonical}",
        f"{canonical} ",
        canonical.replace("-", " ").replace("_", " ") + " ",
        "done",
        "ok",
        "всё готово, подробности в output",
        "",
        None,
        1,
        True,
        [canonical],
        {"value": canonical},
        *(rule.legacy for rule in MIGRATION_RULES if rule.field_id == binding.field_id),
    ]
    allowed = write_values(binding.field_id)
    return [
        value
        for value in candidates
        if not (isinstance(value, str) and value in allowed)
    ]


def _binding_id(binding: FieldBinding) -> str:
    return f"{binding.directory}:{binding.location}"


def _audit_count(scene: Scene) -> int:
    return len(list((scene.generation / "audit").glob("*.json")))


# --- The write boundary of every bound machine field (DoD 1) ------------------------------------


@pytest.mark.parametrize("binding", FIELD_BINDINGS, ids=_binding_id)
def test_every_canonical_value_of_a_bound_field_is_written(
    binding: FieldBinding,
) -> None:
    for value in sorted(write_values(binding.field_id)):
        assert write_problems(binding.directory, _record(binding.location, value)) == []


@pytest.mark.parametrize("binding", FIELD_BINDINGS, ids=_binding_id)
def test_a_variant_of_a_bound_field_is_refused_with_the_exact_values(
    binding: FieldBinding,
) -> None:
    allowed = sorted(write_values(binding.field_id))
    for value in _variants(binding):
        [(problem, remedy)] = write_problems(
            binding.directory, _record(binding.location, value)
        )
        assert problem.startswith(f"{binding.directory} ")
        assert ", ".join(allowed) in problem and ", ".join(allowed) in remedy


def test_a_retired_legacy_spelling_is_never_written_again() -> None:
    for rule in MIGRATION_RULES:
        assert rule.legacy not in write_values(rule.field_id)
    assert write_values("report.check_result") == {"pass", "fail", "not_run"}


def test_a_missing_bound_key_is_neither_refused_nor_defaulted() -> None:
    for directory in {binding.directory for binding in FIELD_BINDINGS}:
        assert write_problems(directory, {}) == []
    assert write_problems("reports", {"checks_run": "free text"}) == []


def test_explanations_stay_in_their_own_text_fields(scene: Scene) -> None:
    note = "тесты упали на CI, подробности в output; Completed?"
    report: JsonObject = {
        **REPORT,
        "output": note,
        "blockers": {"status": "present", "details": note},
        "checks_run": [{"command": "make test", "result": "fail", "note": note}],
    }
    path = scene.generation / f"reports/{DISPATCH_A}.json"

    scene.ledger.write_immutable(path, report)

    assert path.is_file()
    with pytest.raises(LedgerError, match="reports outcome"):
        scene.ledger.write_immutable(
            scene.generation / "reports/dispatch-000000a2.json",
            {**report, "outcome": note},
        )


# --- Every ledger write refuses before anything is written (DoD 1) ------------------------------


def _batch(state: str = "awaiting-approval", **changes: object) -> JsonObject:
    return {
        "batch_id": BATCH_A,
        "state": state,
        "approval_policy": "auto",
        "dispatches": [],
        "coordinator_approval": None,
        **changes,
    }


def test_write_immutable_and_write_record_refuse_a_variant(scene: Scene) -> None:
    before = tree(scene.root)
    report_path = scene.generation / f"reports/{DISPATCH_A}.json"
    variant = {**REPORT, "checks_run": [{"command": "make test", "result": "passed"}]}

    with pytest.raises(LedgerError, match="checks_run\\[0\\].result"):
        scene.ledger.write_immutable(report_path, variant)
    with pytest.raises(LedgerError, match="approval_policy"):
        scene.ledger.write_record(BatchRecord.from_dict(_batch(approval_policy="Auto")))
    with pytest.raises(LedgerError, match="purpose"):
        scene.ledger.write_immutable(
            scene.generation / f"dispatches/{DISPATCH_A}.json",
            {"dispatch_id": DISPATCH_A, "state": "approved", "purpose": "fix"},
        )

    assert tree(scene.root) == before


def test_replace_and_replace_record_refuse_a_variant(scene: Scene) -> None:
    scene.ledger.write_immutable(
        scene.generation / f"plans/{BATCH_A}.json", {"batch_id": BATCH_A}
    )
    scene.ledger.write_record(BatchRecord.from_dict(_batch()))
    entry: JsonObject = {
        "dispatch_id": DISPATCH_A,
        "role": "Developer",
        "state": "proposed",
    }
    before = tree(scene.root)
    path = scene.generation / f"batches/{BATCH_A}.json"

    with pytest.raises(LedgerError, match="dispatches\\[0\\].role"):
        scene.ledger.replace(path, _batch(dispatches=[entry]))
    with pytest.raises(LedgerError, match="dispatches\\[0\\].role"):
        scene.ledger.replace_record(BatchRecord.from_dict(_batch(dispatches=[entry])))

    assert tree(scene.root) == before


def test_check_step_and_a_composite_write_refuse_a_variant(scene: Scene) -> None:
    path = scene.generation / f"reports/{DISPATCH_A}.json"
    variant = {**REPORT, "risks": {"status": "None", "details": ""}}
    before = (tree(scene.root), scene.store_bytes())

    with pytest.raises(LedgerError, match="risks.status"):
        scene.ledger.check_step("create", path, variant)
    write = CompositeWrite(
        scene.ledger,
        scene.store(),
        batch_id=BATCH_A,
        dispatch_id=DISPATCH_A,
        kind="report",
        idempotency_key="key",
    )
    write.write_immutable(path, variant)
    with pytest.raises(CoordinatorError, match="risks.status"):
        write.commit()

    assert (tree(scene.root), scene.store_bytes()) == before


# --- Audit events take only their closed vocabularies (DoD 1) -----------------------------------


def test_every_audit_event_a_ledger_writes_has_a_canonical_action(scene: Scene) -> None:
    scene.ledger.write_immutable(
        scene.generation / f"plans/{BATCH_A}.json", {"batch_id": BATCH_A}
    )
    scene.ledger.write_record(BatchRecord.from_dict(_batch()))
    scene.ledger.replace_record(BatchRecord.from_dict(_batch(ticket="#697")))
    scene.ledger.write_artifact(scene.generation / "reports/note.md", "# note\n")

    actions = {
        verified_audit(path)["action"]
        for path in (scene.generation / "audit").glob("*.json")
    }

    assert actions == {
        "generation-created",
        "immutable-record",
        "transition",
        "immutable-artifact",
    }
    assert actions <= set(AUDIT_ACTIONS)


@pytest.mark.parametrize(
    ("action", "details", "match"),
    [
        ("Immutable-Record", {"path": "reports/x.md", "sha256": "0" * 64}, "action"),
        ("record created", {"path": "reports/x.md", "sha256": "0" * 64}, "action"),
        ("generation-created", {"purpose": "init"}, "purpose"),
        ("generation-created", {}, "purpose"),
        (
            "transition",
            {"path": f"batches/{BATCH_A}.json", "from": "Active", "to": "completed"},
            "from",
        ),
        (
            "transition",
            {"path": f"batches/{BATCH_A}.json", "from": "active", "to": "done"},
            "to",
        ),
    ],
)
def test_an_audit_event_outside_its_vocabulary_is_refused_before_it_is_written(
    scene: Scene, action: str, details: JsonObject, match: str
) -> None:
    before = (tree(scene.root), _audit_count(scene))

    with pytest.raises(LedgerError, match=match):
        scene.ledger.append_audit(
            scene.generation / "reports/x.md",
            action,
            details,
            audit_id="audit-strict-1",
        )
    with pytest.raises(LedgerError, match=match):
        scene.ledger.append_generation_audit(
            scene.generation, action, details, audit_id="audit-strict-2"
        )

    assert (tree(scene.root), _audit_count(scene)) == before


def test_a_transition_of_another_record_carries_no_batch_states(scene: Scene) -> None:
    details: JsonObject = {
        "path": f"dispatch-status/{DISPATCH_A}.json",
        "before_sha256": "0" * 64,
        "sha256": "1" * 64,
    }

    scene.ledger.append_audit(
        scene.generation / f"dispatch-status/{DISPATCH_A}.json",
        "transition",
        details,
        audit_id="audit-strict-3",
    )

    assert scene.ledger.read_audit("audit-strict-3") is not None


# --- A planned continuation trigger is exact (DoD 1) --------------------------------------------


@pytest.mark.parametrize(
    "trigger",
    [" vertical-slice", "vertical-slice ", "Vertical-Slice", "vertical slice", None],
)
def test_a_planned_trigger_variant_is_refused_naming_the_exact_values(
    trigger: object,
) -> None:
    args = argparse.Namespace(
        trigger=trigger, measured_value=None, file=None, note=None
    )

    with pytest.raises(CoordinatorError) as caught:
        reports._authorize_planned_continuation({}, {}, {}, args)

    assert "--trigger must be one of: context-limit" in caught.value.message
    assert "vertical-slice" in caught.value.remedy


def test_an_exact_planned_trigger_passes_the_trigger_check() -> None:
    args = argparse.Namespace(
        trigger="vertical-slice", measured_value=None, file=None, note=None
    )

    with pytest.raises(CoordinatorError, match="requires --file"):
        reports._authorize_planned_continuation({}, {}, {}, args)
