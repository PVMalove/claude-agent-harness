"""The systemFields.v1 registry: its shape, its loader and its agreement with the code."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from harness.orchestration.core import system_fields
from harness.orchestration.core.system_fields import (
    CONTRACT_VERSION,
    REGISTRY_PATH,
    SystemFieldsError,
    load_registry,
)
from harness.orchestration.core.utils import JsonObject

# The v1 inventory: every field the registry must carry.
V1_FIELDS = frozenset(
    {
        "batch.state",
        "batch.next_action",
        "dispatch.state",
        "dispatch.purpose",
        "pipeline.role",
        "pipeline.rewind_stage",
        "batch.decision",
        "retry.reason",
        "retry.route",
        "recovery.environment_reason",
        "attention.reason",
        "attention.event_kind",
        "auto.decision_kind",
        "auto.stop_category",
        "auto.stop_reason",
        "continuation.trigger",
        "continuation.termination_reason",
        "report.outcome",
        "report.check_result",
        "report.evidence_status",
        "review.severity",
        "finding.severity",
        "carried_item.source",
        "carried_item.status",
        "delta_review.mode",
        "delta_review.escalation",
        "qa.stage",
        "qa.code_checks_started",
        "qa.diagnosis_category",
        "context.telemetry_source",
        "context.pressure_level",
        "integration.evidence_kind",
        "integration.check_result",
        "integration.local_qa_ci_condition",
        "integration.ci_verification",
        "ci.checkout",
        "ci.verdict",
        "ci.verdict_reason",
        "resolver.event_kind",
        "resolver.cause",
        "config.approval_policy",
        "config.human_approval_gate",
        "config.auto_retry_min_severity",
        "config.communication_language",
        "role.mode",
        "role.transport",
        "assignment.effort",
        "access.mode",
        "access.resource",
        "access.operation",
        "access.permission",
        "extension.kind",
        "repo_map.tier",
        "context_package.role",
    }
)


def _shipped() -> JsonObject:
    document: JsonObject = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return document


def _fields(document: JsonObject) -> JsonObject:
    fields = document["fields"]
    assert isinstance(fields, dict)
    return fields


def _field(document: JsonObject, field_id: str) -> JsonObject:
    field = _fields(document)[field_id]
    assert isinstance(field, dict)
    return field


def _write(tmp_path: Path, document: JsonObject) -> Path:
    path = tmp_path / "system_fields.v1.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


# -- the shipped registry ------------------------------------------------------------------------


def test_the_shipped_registry_loads_with_its_contract_version() -> None:
    registry = load_registry()

    assert registry.contract == CONTRACT_VERSION == "systemFields.v1"
    assert set(registry.unrecognized_policies) == {"reject", "not-recognized"}
    assert V1_FIELDS <= set(registry.fields)


def test_the_registry_ships_next_to_the_orchestration_package() -> None:
    package = Path(system_fields.__file__).resolve().parent.parent

    assert REGISTRY_PATH == package / "system_fields.v1.json"
    assert REGISTRY_PATH.is_file()


def test_every_field_states_its_handling_and_every_value_its_semantics() -> None:
    for field_id, field in load_registry().fields.items():
        assert field.field_id == field_id
        assert field.description.strip()
        assert isinstance(field.automatic_decisions, bool)
        assert field.unrecognized in {"reject", "not-recognized"}
        assert field.values
        assert all(text.strip() for text in field.values.values())
        assert field.allowed == tuple(field.values)


def test_only_the_termination_reason_carries_diagnostic_text() -> None:
    registry = load_registry()

    assert {
        field_id
        for field_id, field in registry.fields.items()
        if field.unrecognized == "not-recognized"
    } == {"continuation.termination_reason"}


def test_sentinels_name_their_safe_handling() -> None:
    sentinels = {
        field_id: field.sentinels
        for field_id, field in load_registry().fields.items()
        if field.sentinels
    }

    assert sentinels == {
        "retry.reason": ("unknown",),
        "qa.code_checks_started": ("unknown",),
        "qa.diagnosis_category": ("unknown",),
        "integration.local_qa_ci_condition": ("unavailable",),
        "ci.checkout": ("unknown",),
        "ci.verdict_reason": ("unavailable",),
    }
    registry = load_registry()
    for field_id, names in sentinels.items():
        for name in names:
            assert registry.fields[field_id].values[name].startswith("Sentinel: ")


def test_the_defaults_and_groups_of_the_inventory() -> None:
    registry = load_registry()

    assert {
        field_id: field.default
        for field_id, field in registry.fields.items()
        if field.default is not None
    } == {
        "dispatch.purpose": "work",
        "config.approval_policy": "manual_all",
        "config.human_approval_gate": "trusted",
        "config.auto_retry_min_severity": "info",
        "role.transport": "in-process",
        "access.mode": "inherit",
        "repo_map.tier": "full",
        "context_package.role": "shared",
    }
    assert {
        field_id: set(field.groups)
        for field_id, field in registry.fields.items()
        if field.groups
    } == {
        "batch.state": {"terminal"},
        "dispatch.state": {"live"},
        "batch.decision": {"accepted"},
        "retry.reason": {"operational", "developer"},
        "auto.stop_reason": {
            "budget-exhausted",
            "integrity-failure",
            "no-automatic-route",
        },
        "continuation.trigger": {"planned"},
        "qa.stage": {"primary"},
    }


def test_the_fields_that_drive_no_automatic_decision() -> None:
    registry = load_registry()

    assert {
        field_id
        for field_id, field in registry.fields.items()
        if not field.automatic_decisions
    } == {
        "pipeline.rewind_stage",
        "attention.event_kind",
        "config.communication_language",
        "assignment.effort",
        "extension.kind",
        "context_package.role",
    }


def test_a_loaded_registry_is_read_only() -> None:
    field = load_registry().fields["batch.state"]

    with pytest.raises(TypeError):
        field.values["planned"] = "changed"  # type: ignore[index]


# -- malformed shapes ----------------------------------------------------------------------------

Mutation = Callable[[JsonObject], None]


def _set(path: tuple[str, ...], value: object) -> Mutation:
    def mutate(document: JsonObject) -> None:
        node: JsonObject = document
        for key in path[:-1]:
            child = node[key]
            assert isinstance(child, dict)
            node = child
        node[path[-1]] = value

    return mutate


def _drop(path: tuple[str, ...]) -> Mutation:
    def mutate(document: JsonObject) -> None:
        node: JsonObject = document
        for key in path[:-1]:
            child = node[key]
            assert isinstance(child, dict)
            node = child
        del node[path[-1]]

    return mutate


def _rename_value(field_id: str, old: str, new: str) -> Mutation:
    def mutate(document: JsonObject) -> None:
        values = _field(document, field_id)["values"]
        assert isinstance(values, dict)
        values[new] = values.pop(old)

    return mutate


def _add_value(field_id: str, name: str) -> Mutation:
    def mutate(document: JsonObject) -> None:
        values = _field(document, field_id)["values"]
        assert isinstance(values, dict)
        values[name] = "A value."

    return mutate


def _rename_field(old: str, new: str) -> Mutation:
    def mutate(document: JsonObject) -> None:
        fields = _fields(document)
        fields[new] = fields.pop(old)

    return mutate


_F = ("fields", "batch.state")
MALFORMED: dict[str, tuple[Mutation, str]] = {
    "unknown top-level key": (_set(("extra",), 1), "unknown keys ['extra']"),
    "missing top-level key": (_drop(("description",)), "missing keys ['description']"),
    "other contract version": (_set(("contract",), "systemFields.v2"), "contract"),
    "contract not a string": (_set(("contract",), 1), "contract 1"),
    "empty description": (_set(("description",), " "), "description"),
    "unknown policy": (
        _set(("unrecognized_policies", "ignore"), "text"),
        "unrecognized_policies has missing keys [] and unknown keys ['ignore']",
    ),
    "missing policy": (
        _drop(("unrecognized_policies", "reject")),
        "unrecognized_policies has missing keys ['reject']",
    ),
    "empty policy text": (_set(("unrecognized_policies", "reject"), ""), "reject"),
    "fields not an object": (_set(("fields",), []), "fields is not an object"),
    "no fields": (_set(("fields",), {}), "fields is empty"),
    "field id without area": (_rename_field("batch.state", "state"), "field id"),
    "upper-case field id": (_rename_field("batch.state", "Batch.state"), "field id"),
    "field not an object": (_set(_F, "planned"), "is not an object"),
    "unknown field key": (
        _set((*_F, "transitions"), {}),
        "unknown keys ['transitions']",
    ),
    "missing values": (_drop((*_F, "values")), "missing keys ['values']"),
    "missing automatic flag": (
        _drop((*_F, "automatic_decisions")),
        "missing keys ['automatic_decisions']",
    ),
    "automatic flag not a boolean": (
        _set((*_F, "automatic_decisions"), "true"),
        "automatic_decisions is not a boolean",
    ),
    "unknown unrecognized policy": (
        _set((*_F, "unrecognized"), "ignore"),
        "unrecognized 'ignore'",
    ),
    "empty field description": (_set((*_F, "description"), ""), "description"),
    "values not an object": (
        _set((*_F, "values"), ["planned"]),
        "values is not an object",
    ),
    "no values": (_set((*_F, "values"), {}), "has no values"),
    "upper-case value": (
        _rename_value("batch.state", "planned", "Planned"),
        "'Planned'",
    ),
    "value with spaces": (
        _rename_value("batch.state", "planned", "plan ned"),
        "'plan ned'",
    ),
    "value with a double separator": (
        _rename_value("batch.state", "planned", "plan--ned"),
        "'plan--ned'",
    ),
    "Cyrillic value": (_rename_value("batch.state", "planned", "план"), "'план'"),
    "empty semantics": (_set((*_F, "values", "planned"), " "), "semantics"),
    "semantics not a string": (_set((*_F, "values", "planned"), 1), "semantics"),
    "default outside values": (_set((*_F, "default"), "running"), "default 'running'"),
    "default not a string": (
        _set((*_F, "default"), ["planned"]),
        "default ['planned']",
    ),
    "group member outside values": (
        _set((*_F, "groups", "terminal"), ["completed", "done"]),
        "member 'done'",
    ),
    "empty group": (_set((*_F, "groups", "terminal"), []), "non-empty list"),
    "repeated group member": (
        _set((*_F, "groups", "terminal"), ["completed", "completed"]),
        "repeats a value",
    ),
    "groups not an object": (
        _set((*_F, "groups"), ["completed"]),
        "groups is not an object",
    ),
    "no groups": (_set((*_F, "groups"), {}), "groups is empty"),
    "upper-case group name": (
        _set((*_F, "groups", "Live"), ["active"]),
        "group 'Live'",
    ),
    "sentinel outside values": (
        _set(("fields", "retry.reason", "sentinels"), ["unavailable"]),
        "member 'unavailable'",
    ),
    "sentinel with another name": (
        _set(("fields", "retry.reason", "sentinels"), ["unknown", "code"]),
        "sentinel 'code'",
    ),
    "unlisted sentinel value": (
        _drop(("fields", "retry.reason", "sentinels")),
        "'unknown' is a sentinel name but not listed",
    ),
    "unlisted unsupported value": (
        _add_value("batch.state", "unsupported"),
        "'unsupported' is a sentinel name but not listed",
    ),
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_a_malformed_registry_is_refused_with_a_remedy(
    tmp_path: Path, case: str
) -> None:
    mutate, expected = MALFORMED[case]
    document = copy.deepcopy(_shipped())
    mutate(document)

    with pytest.raises(SystemFieldsError) as caught:
        load_registry(_write(tmp_path, document))

    assert expected in caught.value.message
    assert caught.value.remedy.startswith("fix ")


def test_a_registry_that_is_not_an_object_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "system_fields.v1.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(SystemFieldsError) as caught:
        load_registry(path)

    assert "the registry is not an object" in caught.value.message
    assert caught.value.remedy


def test_duplicate_json_keys_are_refused(tmp_path: Path) -> None:
    text = REGISTRY_PATH.read_text(encoding="utf-8").replace(
        '"planned": ', '"planned": "Created.", "planned": ', 1
    )
    path = tmp_path / "system_fields.v1.json"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(SystemFieldsError) as caught:
        load_registry(path)

    assert "duplicate key 'planned'" in caught.value.message
    assert "without duplicate keys" in caught.value.remedy


def test_invalid_json_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "system_fields.v1.json"
    path.write_text('{"contract": ', encoding="utf-8")

    with pytest.raises(SystemFieldsError) as caught:
        load_registry(path)

    assert "is not valid JSON" in caught.value.message
    assert caught.value.remedy


def test_invalid_utf8_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "system_fields.v1.json"
    path.write_bytes('{"description": "реестр"}'.encode("cp1251"))

    with pytest.raises(SystemFieldsError) as caught:
        load_registry(path)

    assert "is not UTF-8 text" in caught.value.message
    assert caught.value.remedy.endswith("save it as UTF-8")


def test_a_missing_registry_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "system_fields.v1.json"

    with pytest.raises(SystemFieldsError) as caught:
        load_registry(path)

    assert "cannot be read" in caught.value.message
    assert "reinstalling the harness" in caught.value.remedy


def test_the_loader_imports_no_contract_or_workflow_module() -> None:
    source = Path(system_fields.__file__).read_text(encoding="utf-8")
    imports = [
        line for line in source.splitlines() if line.startswith(("import ", "from "))
    ]

    assert all("contract" not in line and "workflow" not in line for line in imports)
    assert all(
        line.startswith(("from __future__", "from harness.errors"))
        or "harness" not in line
        for line in imports
    )
