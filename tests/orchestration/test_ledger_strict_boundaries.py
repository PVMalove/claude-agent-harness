"""Strict canonical-value boundaries of the lifecycle ledger: a write accepts only exact machine
values, so free text, another letter case and synonyms never reach a record, a report or an audit
event, while explanations stay in their own text fields; and an automatic decision reads only a
generation whose machine fields are canonical or vouched for by its current migration, and whose
batch graph, hashes and immutable pointers are whole."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from harness.json_types import JsonObject
from harness.orchestration.core.utils import CoordinatorError
from harness.orchestration import runtime_emergency
from harness.orchestration.ledger import field_migration, ledger_admin
from harness.orchestration.ledger.field_migration import (
    FIELD_BINDINGS,
    MIGRATION_RULES,
    FieldBinding,
    MigrationRule,
    write_problems,
    write_values,
)
from harness.orchestration.ledger.generation_migration import read_manifest, survey
from harness.orchestration.ledger.ledger_ops import (
    LedgerUnmigratedError,
    LedgerUnreadableError,
    _load_batch,
    _load_dispatch,
    _load_dispatch_status,
)
from harness.orchestration.ledger.lifecycle import (
    AUDIT_ACTIONS,
    BatchRecord,
    LedgerError,
    LedgerUnmigrated,
    LifecycleLedger,
    verified_audit,
)
from harness.orchestration.ledger.strict_read import require_canonical, unvouched
from harness.orchestration.runtime_intent import CompositeWrite
from harness.orchestration.runtime_reconcile import settle
from harness.orchestration.runtime_state_store import RuntimeStateError
from harness.orchestration.workflow import reports
from tests.orchestration.test_ledger_migration import (
    BATCH_A,
    BATCH_B,
    DEFECTS,
    DISPATCH_A,
    DISPATCH_B,
    PASSED_REPORT,
    REPORT,
    Scene,
    canonical,
    make_scene,
    sha,
    tree,
)
from tests.orchestration.test_runtime_reconcile_faults import Crash, crash_on


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


# --- The strict read boundary of machine fields (DoD 2) -----------------------------------------

MIGRATE_ROUTE = "run 'coordinator.py ledger migrate'"


def _history(scene: Scene) -> None:
    """A mapped legacy check result in batch A and an unknown report outcome in batch B, which
    no check of the ledger's container finds."""
    scene.seed(BATCH_A, DISPATCH_A, report=PASSED_REPORT)
    scene.seed(BATCH_B, DISPATCH_B, report={**REPORT, "outcome": "done"})


def _state(scene: Scene) -> tuple[dict[str, bytes], dict[str, bytes]]:
    return tree(scene.root), scene.store_bytes()


def test_a_mapped_value_is_vouched_only_by_its_exact_manifest_entry(
    scene: Scene,
) -> None:
    scene.seed(BATCH_A, DISPATCH_A, report=PASSED_REPORT)
    [(path, finding)] = unvouched(survey(scene.generation), None)
    assert (path, finding.disposition) == (f"reports/{DISPATCH_A}.json", "mapped")

    _, manifest = scene.migrate()

    found = survey(scene.generation)
    assert unvouched(found, manifest) == []
    [entry] = manifest["mapped"]
    for changed in (
        {**entry, "sha256": "0" * 64},
        {**entry, "legacy_value": "not-run"},
        {**entry, "canonical_value": "fail"},
        {**entry, "location": "checks_run[1].result"},
    ):
        assert len(unvouched(found, {**manifest, "mapped": [changed]})) == 1


def test_a_generation_without_a_finding_needs_no_manifest(scene: Scene) -> None:
    scene.seed(BATCH_A, DISPATCH_A)

    require_canonical(scene.generation)

    assert read_manifest(scene.generation) is None
    assert _load_batch(scene.root, BATCH_A)["batch_id"] == BATCH_A
    assert _load_dispatch_status(scene.root, DISPATCH_A)["state"] == "reported"


def test_before_migrate_unmigrated_history_denies_every_automatic_read(
    scene: Scene,
) -> None:
    _history(scene)
    before = _state(scene)

    for load, record_id in (
        (_load_batch, BATCH_A),
        (_load_batch, BATCH_B),
        (_load_dispatch_status, DISPATCH_A),
    ):
        with pytest.raises(LedgerUnmigratedError) as caught:
            load(scene.root, record_id)
        assert caught.value.message.startswith(MIGRATE_ROUTE)
        assert f"reports/{DISPATCH_A}.json checks_run[0].result" in caught.value.message
        assert "ledger inspect" in caught.value.remedy
        assert "ledger migrate" in caught.value.remedy
    # The lookup a reconcile starts from stays on the container check, so settle runs first.
    assert _load_dispatch(scene.root, DISPATCH_A)["batch_id"] == BATCH_A
    assert settle(scene.repo, scene.root, scene.ledger, BATCH_A) is False
    assert _state(scene) == before


def test_after_migrate_a_vouched_value_is_read_and_a_quarantined_batch_is_not(
    scene: Scene,
) -> None:
    _history(scene)

    result = scene.run()

    assert result["quarantined"] == [BATCH_B] and result["mapped"] == 1
    assert _load_batch(scene.root, BATCH_A)["batch_id"] == BATCH_A
    assert _load_dispatch_status(scene.root, DISPATCH_A)["state"] == "reported"
    # The mapped report keeps its legacy bytes: no canonical value replaces it.
    report = (scene.generation / f"reports/{DISPATCH_A}.json").read_bytes()
    assert report == canonical(PASSED_REPORT)
    with pytest.raises(CoordinatorError) as caught:
        _load_batch(scene.root, BATCH_B)
    assert not isinstance(caught.value, LedgerUnreadableError)


def test_an_unknown_value_in_unowned_evidence_names_ledger_clean(scene: Scene) -> None:
    scene.seed(BATCH_A, DISPATCH_A)
    scene.put(
        "dispatches/dispatch-0000ffff.json",
        {"dispatch_id": "dispatch-0000ffff", "state": "approved", "role": "Developer"},
    )

    with pytest.raises(LedgerUnmigratedError) as caught:
        _load_batch(scene.root, BATCH_A)

    assert "'Developer' (legacy-unknown)" in caught.value.message
    assert "ledger clean" in caught.value.remedy


def test_a_long_value_is_shown_bounded_after_the_route(scene: Scene) -> None:
    scene.seed(BATCH_A, DISPATCH_A, report={**REPORT, "outcome": "x" * 500})

    with pytest.raises(LedgerUnmigrated) as caught:
        require_canonical(scene.generation)

    assert caught.value.message.startswith(MIGRATE_ROUTE)
    assert len(caught.value.message) < 240


def test_changed_rules_route_the_read_to_migrate_again(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene.seed(BATCH_A, DISPATCH_A, report=PASSED_REPORT)
    scene.run()
    extra = MigrationRule("report.outcome", "done", "completed", "a test rule")
    monkeypatch.setattr(
        field_migration, "MIGRATION_RULES", (*field_migration.MIGRATION_RULES, extra)
    )

    with pytest.raises(LedgerUnmigratedError, match="migration rules changed"):
        _load_batch(scene.root, BATCH_A)
    assert scene.run()["migrated"] is True

    assert _load_batch(scene.root, BATCH_A)["batch_id"] == BATCH_A


def test_an_unmigrated_read_is_a_classified_integrity_stop(
    scene: Scene, capsys: pytest.CaptureFixture[str]
) -> None:
    _history(scene)
    ledger = tree(scene.root / "generations")
    args = argparse.Namespace(
        command="batch",
        batch_command="decide",
        batch=BATCH_A,
        repo=str(scene.repo),
        state_dir=str(scene.root),
    )
    with pytest.raises(LedgerUnmigratedError) as caught:
        _load_batch(scene.root, BATCH_A)

    runtime_emergency.report_failure(args, caught.value)

    line = capsys.readouterr().err
    assert '"code": "integrity-failure"' in line and '"step": "ledger-read"' in line
    assert "ledger migrate" in line
    assert tree(scene.root / "generations") == ledger


# --- A value or migration evidence that changed after the migration (DoD 2, 3) ------------------


def _migrated(scene: Scene) -> None:
    _history(scene)
    scene.run()


def _late_value(scene: Scene) -> None:
    scene.put(
        "dispatches/dispatch-000000a9.json",
        {
            "dispatch_id": "dispatch-000000a9",
            "batch_id": BATCH_A,
            "state": "approved",
            "role": "Developer",
        },
    )


def _changed_mapped_report(scene: Scene) -> None:
    path = scene.generation / f"reports/{DISPATCH_A}.json"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))


def _changed_manifest(scene: Scene) -> None:
    path = scene.generation / "migration/manifest.json"
    path.write_bytes(path.read_bytes().replace(b'"mapped"', b'"mapped" '))


def _changed_quarantine(scene: Scene) -> None:
    path = scene.generation / f"migration/quarantine/{BATCH_B}/batches/{BATCH_B}.json"
    path.write_bytes(path.read_bytes() + b" ")


def _extra_migration_file(scene: Scene) -> None:
    (scene.generation / "migration/notes.txt").write_text("note", encoding="utf-8")


CHANGED = {
    "a value written after the migration": (_late_value, "does not vouch"),
    "a mapped report changed after the migration": (
        _changed_mapped_report,
        "does not vouch",
    ),
    "a changed manifest": (_changed_manifest, "does not record its manifest"),
    "a changed quarantined record": (_changed_quarantine, "changed migration file"),
    "an extra migration file": (_extra_migration_file, "does not name"),
}


@pytest.mark.parametrize("change", sorted(CHANGED))
def test_a_change_after_the_migration_is_an_integrity_stop_not_a_migration(
    scene: Scene, change: str
) -> None:
    _migrated(scene)
    make, match = CHANGED[change]
    make(scene)
    before = _state(scene)

    with pytest.raises(LedgerUnreadableError, match=match) as caught:
        _load_batch(scene.root, BATCH_A)

    assert not isinstance(caught.value, LedgerUnmigratedError)
    assert _state(scene) == before


# --- The batch graph, its hashes and immutable pointers (DoD 3) ---------------------------------


def _json(path: Path) -> JsonObject:
    value = json.loads(path.read_text("utf-8"))
    assert isinstance(value, dict)
    return value


def _rewrite_entry(scene: Scene, **changes: object) -> None:
    """Rewrite batch A on disk, as damaged history would hold it, with its entry changed."""
    batch = _json(scene.generation / f"batches/{BATCH_A}.json")
    batch["dispatches"][0].update(changes)
    scene.put(f"batches/{BATCH_A}.json", batch)


def _missing_brief(scene: Scene) -> None:
    (scene.generation / f"dispatches/{DISPATCH_A}.json").unlink()


def _missing_status(scene: Scene) -> None:
    (scene.generation / f"dispatch-status/{DISPATCH_A}.json").unlink()


def _changed_brief(scene: Scene) -> None:
    brief = _json(scene.generation / f"dispatches/{DISPATCH_A}.json")
    scene.put(f"dispatches/{DISPATCH_A}.json", {**brief, "note": "changed"})


def _changed_report(scene: Scene) -> None:
    report = _json(scene.generation / f"reports/{DISPATCH_A}.json")
    scene.put(f"reports/{DISPATCH_A}.json", {**report, "output": "changed"})


def _brief_of_another_batch(scene: Scene) -> None:
    brief = _json(scene.generation / f"dispatches/{DISPATCH_A}.json")
    data = scene.put(f"dispatches/{DISPATCH_A}.json", {**brief, "batch_id": BATCH_B})
    _rewrite_entry(scene, brief_sha256=sha(data))


def _report_outside_the_generation(scene: Scene) -> None:
    _rewrite_entry(scene, report="../outside.json")


def _dispatch_id_with_a_separator(scene: Scene) -> None:
    _rewrite_entry(scene, dispatch_id=f"../dispatches/{DISPATCH_A}")


GRAPH_DAMAGE = {
    "a missing brief": (_missing_brief, "dispatch evidence is incomplete"),
    "a missing status": (_missing_status, "dispatch evidence is incomplete"),
    "a changed brief": (_changed_brief, "brief integrity"),
    "a changed report": (_changed_report, "report failed immutable integrity"),
    "a brief of another batch": (_brief_of_another_batch, "does not belong"),
    "a report outside the generation": (
        _report_outside_the_generation,
        "invalid report path",
    ),
    "a dispatch ID with a separator": (
        _dispatch_id_with_a_separator,
        "invalid dispatch entry",
    ),
}


@pytest.mark.parametrize("damage", sorted(GRAPH_DAMAGE))
def test_a_damaged_batch_graph_is_a_controlled_stop_that_rewrites_nothing(
    scene: Scene, damage: str, capsys: pytest.CaptureFixture[str]
) -> None:
    scene.seed(BATCH_A, DISPATCH_A)
    make, match = GRAPH_DAMAGE[damage]
    make(scene)
    before = _state(scene)

    with pytest.raises(LedgerUnreadableError, match=match) as caught:
        _load_batch(scene.root, BATCH_A)
    runtime_emergency.report_failure(
        argparse.Namespace(command="batch", batch_command="show"), caught.value
    )

    assert not isinstance(caught.value, LedgerUnmigratedError)
    line = capsys.readouterr().err
    assert '"code": "integrity-failure"' in line and '"step": "ledger-read"' in line
    assert _state(scene) == before


def test_the_check_of_one_batch_returns_what_it_references(scene: Scene) -> None:
    batch = scene.seed(BATCH_A, DISPATCH_A)

    referenced = LifecycleLedger.check_batch_references(
        scene.generation, batch, f"{BATCH_A}.json"
    )

    assert referenced == {DISPATCH_A}


def test_the_intermediate_states_of_a_composite_write_are_read(scene: Scene) -> None:
    scene.seed(BATCH_A, DISPATCH_A)
    # A dispatch and its status that a composite write creates before the batch names them.
    scene.put(
        "dispatches/dispatch-000000a2.json",
        {"dispatch_id": "dispatch-000000a2", "batch_id": BATCH_A, "state": "approved"},
    )
    scene.put(
        "dispatch-status/dispatch-000000a2.json",
        {"dispatch_id": "dispatch-000000a2", "state": "approved"},
    )
    # A status already ahead of its batch entry.
    scene.put(
        f"dispatch-status/{DISPATCH_A}.json",
        {"dispatch_id": DISPATCH_A, "state": "working"},
    )

    assert _load_batch(scene.root, BATCH_A)["batch_id"] == BATCH_A
    assert _load_dispatch_status(scene.root, DISPATCH_A)["state"] == "working"
    with pytest.raises(LedgerError, match="orphaned dispatch evidence"):
        scene.ledger.validate()


# --- An unmigrated ledger names the ledger migrate route (DoD 4) --------------------------------


def _batch_not_an_object(scene: Scene) -> None:
    scene.seed(BATCH_B, DISPATCH_B)
    scene.put(f"batches/{BATCH_B}.json", b"[]")


# Every value the container check of the ledger stops on, which `ledger migrate` takes over.
UNMIGRATED = {
    **{name: make for name, (make, *_) in DEFECTS.items()},
    "batch not an object": _batch_not_an_object,
}


def _args(scene: Scene, **values: object) -> argparse.Namespace:
    return argparse.Namespace(repo=str(scene.repo), state_dir=str(scene.root), **values)


@pytest.mark.parametrize("defect", sorted(UNMIGRATED))
def test_a_container_refusal_of_history_names_the_migrate_route(
    scene: Scene, defect: str, capsys: pytest.CaptureFixture[str]
) -> None:
    scene.seed(BATCH_A, DISPATCH_A)
    UNMIGRATED[defect](scene)
    before = _state(scene)

    for load, record_id in ((_load_batch, BATCH_A), (_load_dispatch, DISPATCH_A)):
        with pytest.raises(LedgerUnmigratedError) as caught:
            load(scene.root, record_id)
        assert caught.value.message.startswith(MIGRATE_ROUTE)
        assert "ledger inspect" in caught.value.remedy
        assert "ledger migrate" in caught.value.remedy
    runtime_emergency.report_failure(
        argparse.Namespace(command="ledger", ledger_command="validate"), caught.value
    )
    assert "ledger migrate" in capsys.readouterr().err
    assert _state(scene) == before

    scene.run()

    assert _load_batch(scene.root, BATCH_A)["batch_id"] == BATCH_A


def test_an_unreadable_record_outside_the_migrated_graph_keeps_its_own_remedy(
    scene: Scene,
) -> None:
    scene.seed(BATCH_A, DISPATCH_A)
    scene.put("checkpoints/checkpoint-0000000a.json", b"{ not JSON")

    with pytest.raises(LedgerUnreadableError) as caught:
        _load_batch(scene.root, BATCH_A)

    assert not isinstance(caught.value, LedgerUnmigratedError)
    assert "fix the JSON syntax" in caught.value.remedy


@pytest.mark.parametrize(
    "history",
    ["container", "machine field"],
)
def test_ledger_validate_names_the_migrate_route_until_ledger_migrate(
    scene: Scene, history: str
) -> None:
    if history == "container":
        scene.seed(BATCH_A, DISPATCH_A)
        UNMIGRATED["unknown batch.state"](scene)
    else:
        _history(scene)
    before = _state(scene)

    with pytest.raises(LedgerUnmigratedError) as caught:
        ledger_admin.validate_ledger(_args(scene))

    assert caught.value.message.startswith(MIGRATE_ROUTE)
    assert "ledger migrate" in caught.value.remedy
    assert _state(scene) == before

    scene.run()

    assert scene.ledger.validate()["valid"] is True
    require_canonical(scene.generation)


# --- Retry and migration idempotency under the strict boundaries (DoD 5) ------------------------


def test_a_repeated_migrate_changes_nothing_and_the_strict_read_writes_nothing(
    scene: Scene,
) -> None:
    _history(scene)
    first = scene.run()
    _load_batch(scene.root, BATCH_A)
    snapshot = _state(scene)

    again = scene.run()
    loaded = _load_batch(scene.root, BATCH_A)
    status = _load_dispatch_status(scene.root, DISPATCH_A)

    assert again == {**first, "migrated": False}
    assert (loaded["batch_id"], status["state"]) == (BATCH_A, "reported")
    assert _state(scene) == snapshot


def test_an_unfinished_operation_is_settled_before_the_read_routes_to_migrate(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    _history(scene)
    write = CompositeWrite(
        scene.ledger,
        scene.store(),
        batch_id=BATCH_A,
        dispatch_id=DISPATCH_A,
        kind="checkpoint",
        idempotency_key="key",
    )
    write.write_immutable(
        scene.generation / "checkpoints/checkpoint-0000000a.json",
        {"dispatch_id": DISPATCH_A},
    )
    crash_on(monkeypatch, LifecycleLedger, "_append_audit", 1)
    with pytest.raises(Crash):
        write.commit()
    monkeypatch.undo()
    # The migration waits for the batch's unfinished operation.
    with pytest.raises(RuntimeStateError, match="concurrency-conflict"):
        scene.run()

    # A command reconciles first, from the lookup outside the strict read, then is refused.
    batch_id = _load_dispatch(scene.root, DISPATCH_A)["batch_id"]
    assert settle(scene.repo, scene.root, scene.ledger, batch_id) is True
    with pytest.raises(LedgerUnmigratedError):
        _load_batch(scene.root, batch_id)
    result = scene.run()

    assert result["migrated"] is True and result["quarantined"] == [BATCH_B]
    assert _load_batch(scene.root, BATCH_A)["batch_id"] == BATCH_A
    assert (scene.generation / "checkpoints/checkpoint-0000000a.json").is_file()
