"""The systemFields.v1 registry: its shape, its loader and its agreement with the code."""

from __future__ import annotations

import argparse
import copy
import importlib
import inspect
import json
import pkgutil
from collections.abc import Callable, Iterable, Iterator
from enum import StrEnum
from pathlib import Path
from typing import get_args

import pytest

import harness.orchestration
from harness.orchestration import (
    contract,
    coordinator,
    extensions,
    operational_guards,
    runtime_attestation,
)
from harness.orchestration.core import ci_source, constants, system_fields
from harness.orchestration.core.system_fields import (
    CONTRACT_VERSION,
    REGISTRY_PATH,
    SystemFieldsError,
    load_registry,
    vocabulary_drift,
)
from harness.orchestration.core.utils import JsonObject
from harness.orchestration.workflow import (
    auto_policy,
    carried_items,
    completion,
    conditions,
    fix_forward,
    recovery,
    resolver_state,
)

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


# -- registry-to-code drift ----------------------------------------------------------------------

SCHEMA = json.loads(
    (Path(contract.__file__).parent / "orchestration.schema.json").read_text(
        encoding="utf-8"
    )
)
ROLE_STEMS = tuple(
    sorted(
        path.stem
        for path in (Path(contract.__file__).parent / "roles").glob("*.md")
        if path.stem != "_common"
    )
)
CommandOption = tuple[tuple[str, ...], str]


def _cli_choices() -> dict[CommandOption, tuple[object, ...]]:
    """Every argparse action with choices, keyed by its command path and first option string."""
    found: dict[CommandOption, tuple[object, ...]] = {}
    seen: set[int] = set()

    def walk(parser: argparse.ArgumentParser, path: tuple[str, ...]) -> None:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action._name_parser_map.items():
                    if id(sub) not in seen:
                        seen.add(id(sub))
                        walk(sub, (*path, name))
            elif action.choices is not None:
                found[(path, action.option_strings[0])] = tuple(action.choices)

    walk(coordinator.parser(), ())
    return found


def _schema_enums() -> dict[str, tuple[list[object], object]]:
    """Every ``enum`` of the schema, keyed by its JSON pointer, with the schema default."""
    found: dict[str, tuple[list[object], object]] = {}

    def walk(node: object, pointer: str) -> None:
        if isinstance(node, dict):
            if "enum" in node:
                found[pointer] = (node["enum"], node.get("default"))
            for key, child in node.items():
                walk(child, f"{pointer}/{key}")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f"{pointer}/{index}")

    walk(SCHEMA, "")
    return found


def _schema_enum(pointer: str) -> list[object]:
    return _schema_enums()[pointer][0]


CLI = _cli_choices()
# Every registry field and the code vocabularies that must hold exactly its values.
BINDINGS: dict[str, dict[str, Iterable[str]]] = {
    "batch.state": {"BatchState": constants.BatchState},
    "batch.next_action": {
        "NEXT_ACTION_DISPATCH_ROLE keys | ACCEPT_NEXT_STEP actions": {
            *constants.NEXT_ACTION_DISPATCH_ROLE,
            *(action for _, action in carried_items.ACCEPT_NEXT_STEP.values()),
        }
    },
    "dispatch.state": {"DispatchState": constants.DispatchState},
    "dispatch.purpose": {"DISPATCH_PURPOSES": constants.DISPATCH_PURPOSES},
    "pipeline.role": {"roles/*.md": ROLE_STEMS},
    "pipeline.rewind_stage": {"recovery.REWIND_STAGES": recovery.REWIND_STAGES},
    "batch.decision": {"DECISIONS": constants.DECISIONS},
    "retry.reason": {
        "RetryReason": constants.RetryReason,
        "RETRY_REASON_CATEGORIES": constants.RETRY_REASON_CATEGORIES,
    },
    "retry.route": {
        "RecoveryRoute": constants.RecoveryRoute,
        "RECOVERY_ROUTES": constants.RECOVERY_ROUTES,
    },
    "recovery.environment_reason": {
        "recovery.ENVIRONMENT_REASONS": recovery.ENVIRONMENT_REASONS
    },
    "attention.reason": {
        "operational_guards.ATTENTION_REASONS": operational_guards.ATTENTION_REASONS,
        "auto_policy.ATTENTION_STOPS keys": auto_policy.ATTENTION_STOPS,
    },
    "attention.event_kind": {"ATTENTION_EVENT_KINDS": constants.ATTENTION_EVENT_KINDS},
    "auto.decision_kind": {
        "AUTO_DECISION_KINDS": constants.AUTO_DECISION_KINDS,
        "AUTO_EVIDENCE_FIELDS keys": constants.AUTO_EVIDENCE_FIELDS,
    },
    "auto.stop_category": {
        "AUTO_STOP_REASONS keys": constants.AUTO_STOP_REASONS,
        "auto_policy.STOP_ORDER": auto_policy.STOP_ORDER,
    },
    "auto.stop_reason": {
        "AUTO_STOP_REASONS values": {
            reason
            for reasons in constants.AUTO_STOP_REASONS.values()
            for reason in reasons
        }
    },
    "continuation.trigger": {
        "dispatch resume --trigger": [
            str(value) for value in CLI[(("dispatch", "resume"), "--trigger")]
        ]
    },
    "continuation.termination_reason": {
        "RATE_LIMIT_TERMINATION_REASONS": constants.RATE_LIMIT_TERMINATION_REASONS
    },
    "report.outcome": {
        "ReportOutcome": constants.ReportOutcome,
        "REPORT_OUTCOMES": constants.REPORT_OUTCOMES,
    },
    "report.check_result": {
        "ReportCheckResult": constants.ReportCheckResult,
        "REPORT_CHECK_RESULTS": constants.REPORT_CHECK_RESULTS,
    },
    "report.evidence_status": {"EvidenceStatus": constants.EvidenceStatus},
    "review.severity": {"REVIEW_SEVERITIES": constants.REVIEW_SEVERITIES},
    "finding.severity": {"FINDING_SEVERITIES": constants.FINDING_SEVERITIES},
    "carried_item.source": {
        "CARRIED_ITEM_SOURCES": constants.CARRIED_ITEM_SOURCES,
        "CARRIED_ITEM_BRIEF_ROLES keys": constants.CARRIED_ITEM_BRIEF_ROLES,
    },
    "carried_item.status": {"CARRIED_ITEM_STATUSES": constants.CARRIED_ITEM_STATUSES},
    "delta_review.mode": {"DELTA_REVIEW_MODES": constants.DELTA_REVIEW_MODES},
    "delta_review.escalation": {
        "DELTA_REVIEW_ESCALATIONS": constants.DELTA_REVIEW_ESCALATIONS
    },
    "qa.stage": {"QA_STAGE_NAMES": constants.QA_STAGE_NAMES},
    "qa.code_checks_started": {
        "QA_CODE_CHECKS_STARTED": constants.QA_CODE_CHECKS_STARTED
    },
    "qa.diagnosis_category": {
        "QA_DIAGNOSIS_CATEGORIES": constants.QA_DIAGNOSIS_CATEGORIES
    },
    "context.telemetry_source": {
        "CONTEXT_TELEMETRY_SOURCES": constants.CONTEXT_TELEMETRY_SOURCES
    },
    "context.pressure_level": {
        "operational_guards.CONTEXT_PRESSURE_LEVELS": operational_guards.CONTEXT_PRESSURE_LEVELS
    },
    "integration.evidence_kind": {
        "INTEGRATION_EVIDENCE_KINDS": constants.INTEGRATION_EVIDENCE_KINDS
    },
    "integration.check_result": {
        "IntegrationCheckResult": constants.IntegrationCheckResult,
        "INTEGRATION_EVIDENCE_RESULTS": constants.INTEGRATION_EVIDENCE_RESULTS,
    },
    "integration.local_qa_ci_condition": {
        "LOCAL_QA_CI_CONDITIONS": constants.LOCAL_QA_CI_CONDITIONS
    },
    "integration.ci_verification": {
        "INTEGRATION_CI_COLLECTED, INTEGRATION_CI_COLLECTED_FAILURE": (
            constants.INTEGRATION_CI_COLLECTED,
            constants.INTEGRATION_CI_COLLECTED_FAILURE,
        )
    },
    "ci.checkout": {"ci_source.Checkout": get_args(ci_source.Checkout)},
    "ci.verdict": {"ci_source.Outcome": get_args(ci_source.Outcome)},
    "ci.verdict_reason": {"ci_source.REASONS": ci_source.REASONS},
    "resolver.event_kind": {"resolver_state.EVENT_KINDS": resolver_state.EVENT_KINDS},
    "resolver.cause": {
        "resolver_state.RESOLVER_CAUSES": resolver_state.RESOLVER_CAUSES
    },
    "config.approval_policy": {
        "contract.APPROVAL_POLICIES": contract.APPROVAL_POLICIES
    },
    "config.human_approval_gate": {
        "contract.HUMAN_APPROVAL_GATES": contract.HUMAN_APPROVAL_GATES
    },
    "config.auto_retry_min_severity": {
        "AUTO_RETRY_SEVERITIES": constants.AUTO_RETRY_SEVERITIES,
        "AUTO_RETRY_SEVERITY_RANKS keys": constants.AUTO_RETRY_SEVERITY_RANKS,
    },
    "config.communication_language": {
        "contract.COMMUNICATION_LANGUAGES": contract.COMMUNICATION_LANGUAGES
    },
    "role.mode": {
        "contract.ROLE_MODES": contract.ROLE_MODES,
        "contract.DEFAULT_ALLOWED_TOOLS keys": contract.DEFAULT_ALLOWED_TOOLS,
    },
    "role.transport": {
        "constants.ROLE_TRANSPORTS": constants.ROLE_TRANSPORTS,
        "contract.ROLE_TRANSPORTS": contract.ROLE_TRANSPORTS,
    },
    "assignment.effort": {"contract.EFFORT_LEVELS": contract.EFFORT_LEVELS},
    "access.mode": {"ACCESS_MODES": constants.ACCESS_MODES},
    "access.resource": {"ACCESS_RESOURCES": constants.ACCESS_RESOURCES},
    "access.operation": {"ACCESS_OPERATIONS": constants.ACCESS_OPERATIONS},
    "access.permission": {
        "schema filesystem access enum": [
            str(value)
            for value in _schema_enum(
                "/definitions/accessComponent/properties/filesystem/items/properties/access"
            )
        ]
    },
    "extension.kind": {"extensions.EXTENSION_KINDS": extensions.EXTENSION_KINDS},
    "repo_map.tier": {"contract.REPO_MAP_TIER_ORDER": contract.REPO_MAP_TIER_ORDER},
    "context_package.role": {
        "context-package register --role": [
            str(value) for value in CLI[(("context-package", "register"), "--role")]
        ]
    },
}
# Every registry group and the code subset it must equal.
GROUP_BINDINGS: dict[str, dict[str, Iterable[str]]] = {
    "batch.state": {"terminal": constants.TERMINAL_BATCH_STATES},
    "dispatch.state": {"live": constants.LIVE_DISPATCH_STATES},
    "batch.decision": {"accepted": constants.ACCEPTED_DECISIONS},
    "retry.reason": {
        "operational": constants.OPERATIONAL_REASON_CATEGORIES,
        "developer": constants.DEVELOPER_REASON_CATEGORIES,
    },
    "qa.stage": {"primary": constants.QA_PRIMARY_STAGE_NAMES},
    "continuation.trigger": {"planned": constants.PLANNED_TRIGGER_KINDS},
    "auto.stop_reason": dict(constants.AUTO_STOP_REASONS),
}
_TIER = "repo_map.tier"
# Every enum pointer of orchestration.schema.json and the registry field it spells.
SCHEMA_BINDINGS = {
    "/properties/access_policy/properties/roles/propertyNames": "pipeline.role",
    "/properties/access_policy/properties/operations/propertyNames": "access.operation",
    "/properties/approval_policy": "config.approval_policy",
    "/properties/human_approval_gate": "config.human_approval_gate",
    "/properties/tool_policy/properties/modes/propertyNames": "role.mode",
    "/properties/repo_map_policy/properties/tier": _TIER,
    "/properties/repo_map_policy/properties/min_tier": _TIER,
    "/properties/repo_map_policy/properties/min_tier_by_role/properties/architect": _TIER,
    "/properties/repo_map_policy/properties/min_tier_by_role/properties/developer": _TIER,
    "/properties/repo_map_policy/properties/min_tier_by_role/properties/code-review": _TIER,
    "/properties/auto_retry_min_severity": "config.auto_retry_min_severity",
    "/properties/extensions/propertyNames": "extension.kind",
    "/definitions/accessComponent/properties/mode": "access.mode",
    "/definitions/accessComponent/properties/filesystem/items/properties/resource": "access.resource",
    "/definitions/accessComponent/properties/filesystem/items/properties/access": "access.permission",
    "/definitions/assignmentPlan/properties/transport": "role.transport",
    "/definitions/runtimeAssignment/properties/effort": "assignment.effort",
}
EQUAL, SUBSET = "equal", "subset"
# Every CLI option with choices: the field it takes and whether it takes all or some values.
CLI_BINDINGS: dict[CommandOption, tuple[str, str]] = {
    (("batch", "create"), "--auto-retry-min-severity"): (
        "config.auto_retry_min_severity",
        EQUAL,
    ),
    (("batch", "rewind"), "--to"): ("pipeline.rewind_stage", EQUAL),
    (("batch", "decide"), "--decision"): ("batch.decision", EQUAL),
    (("batch", "decide"), "--reason-category"): ("retry.reason", EQUAL),
    (("batch", "decide"), "--retry-role"): ("pipeline.role", SUBSET),
    (("batch", "decision-packet"), "--reason-category"): ("retry.reason", EQUAL),
    (("batch", "decision-packet"), "--retry-role"): ("pipeline.role", SUBSET),
    (("context-package", "register"), "--role"): ("context_package.role", EQUAL),
    (("dispatch", "preflight"), "--purpose"): ("dispatch.purpose", EQUAL),
    (("dispatch", "propose"), "--purpose"): ("dispatch.purpose", EQUAL),
    (("dispatch", "create"), "--purpose"): ("dispatch.purpose", EQUAL),
    (("dispatch", "heartbeat"), "--context-source"): (
        "context.telemetry_source",
        SUBSET,
    ),
    (("dispatch", "context-pressure"), "--source"): ("context.telemetry_source", EQUAL),
    (("dispatch", "resume"), "--trigger"): ("continuation.trigger", EQUAL),
    (("integration", "local-qa"), "--ci-condition"): (
        "integration.local_qa_ci_condition",
        EQUAL,
    ),
    (("integration", "link-evidence"), "--kind"): ("integration.evidence_kind", EQUAL),
    (("integration", "link-evidence"), "--result"): ("integration.check_result", EQUAL),
    (("integration", "resolver-event"), "--kind"): ("resolver.event_kind", SUBSET),
}
# Role-, stage-, route- and reason-valued constants that must stay inside a field's values.
SUBSET_BINDINGS: dict[str, tuple[str, Iterable[str]]] = {
    "BLOCK_BYPASS_STAGES": ("pipeline.role", constants.BLOCK_BYPASS_STAGES),
    "KEYED_READ_ONLY_ROLES": (
        "pipeline.role",
        operational_guards.KEYED_READ_ONLY_ROLES,
    ),
    "NO_GATE_WORK_ROLES": ("pipeline.role", contract.NO_GATE_WORK_ROLES),
    "REPO_MAP_POLICY_ROLES": ("pipeline.role", contract.REPO_MAP_POLICY_ROLES),
    "RISK_ASSESSED_ROLES": ("pipeline.role", completion.RISK_ASSESSED_ROLES),
    "WORKER_ROLES_REQUIRING_ISSUE_WORKTREE": (
        "pipeline.role",
        runtime_attestation.WORKER_ROLES_REQUIRING_ISSUE_WORKTREE,
    ),
    "ISSUE_WORKTREE_WRITER_ROLES": (
        "pipeline.role",
        runtime_attestation.ISSUE_WORKTREE_WRITER_ROLES,
    ),
    "CARRIED_ITEM_BRIEF_ROLES values": (
        "pipeline.role",
        {
            role
            for roles in constants.CARRIED_ITEM_BRIEF_ROLES.values()
            for role in roles
        },
    ),
    "INCOMPLETE_ITEM_TARGET_ROLES keys": (
        "pipeline.role",
        constants.INCOMPLETE_ITEM_TARGET_ROLES,
    ),
    "INCOMPLETE_ITEM_TARGET_ROLES values": (
        "pipeline.role",
        {
            role
            for roles in constants.INCOMPLETE_ITEM_TARGET_ROLES.values()
            for role in roles
        },
    ),
    "NEXT_ACTION_DISPATCH_ROLE values": (
        "pipeline.role",
        constants.NEXT_ACTION_DISPATCH_ROLE.values(),
    ),
    "ACCEPT_NEXT_STEP keys": ("pipeline.role", carried_items.ACCEPT_NEXT_STEP),
    "ACCEPT_NEXT_STEP roles": (
        "pipeline.role",
        {
            role
            for role, _ in carried_items.ACCEPT_NEXT_STEP.values()
            if role is not None
        },
    ),
    "conditions.COVERING_STAGES keys": (
        "pipeline.rewind_stage",
        conditions.COVERING_STAGES,
    ),
    "conditions.COVERING_STAGES values": (
        "pipeline.role",
        {stage for stages in conditions.COVERING_STAGES.values() for stage in stages},
    ),
    "INCOMPLETE_ITEM_ROUTES": ("retry.route", carried_items.INCOMPLETE_ITEM_ROUTES),
    "FIX_FORWARD_ROUTES": ("retry.route", fix_forward.FIX_FORWARD_ROUTES),
    "PRIOR_REVIEW_RETRY_ROUTES": (
        "retry.route",
        fix_forward.PRIOR_REVIEW_RETRY_ROUTES,
    ),
    "ATTENTION_STOPS categories": (
        "auto.stop_category",
        {category for category, _ in auto_policy.ATTENTION_STOPS.values()},
    ),
    "ATTENTION_STOPS reasons": (
        "auto.stop_reason",
        {reason for _, reason in auto_policy.ATTENTION_STOPS.values()},
    ),
    "TOOLING_REASON_CATEGORY": ("retry.reason", (constants.TOOLING_REASON_CATEGORY,)),
    "BLOCK_BYPASS_REASON_CATEGORY": (
        "retry.reason",
        (constants.BLOCK_BYPASS_REASON_CATEGORY,),
    ),
}


def _orchestration_modules() -> Iterator[str]:
    package = harness.orchestration
    for module in pkgutil.walk_packages(package.__path__, f"{package.__name__}."):
        yield module.name


def test_the_shipped_registry_has_no_drift_from_the_code() -> None:
    assert vocabulary_drift(load_registry(), BINDINGS, GROUP_BINDINGS) == []


def test_every_orchestration_str_enum_is_bound_to_a_field() -> None:
    bound = [
        vocabulary
        for vocabularies in BINDINGS.values()
        for vocabulary in vocabularies.values()
    ]
    enums = []
    for name in _orchestration_modules():
        module = importlib.import_module(name)
        for _, member in inspect.getmembers(module, inspect.isclass):
            if issubclass(member, StrEnum) and member.__module__ == name:
                enums.append(member)

    assert enums
    unbound = [enum.__qualname__ for enum in enums if not any(enum is v for v in bound)]
    assert unbound == []


def test_every_schema_enum_spells_its_field() -> None:
    registry = load_registry()
    enums = _schema_enums()

    assert set(enums) == set(SCHEMA_BINDINGS)
    for pointer, field_id in SCHEMA_BINDINGS.items():
        values, default = enums[pointer]
        field = registry.fields[field_id]
        assert all(isinstance(value, str) for value in values), pointer
        assert sorted(str(value) for value in values) == sorted(field.values), pointer
        if default is not None:
            assert default == field.default, pointer


def test_every_cli_choice_list_takes_its_field() -> None:
    registry = load_registry()

    assert set(CLI) == set(CLI_BINDINGS)
    for option, (field_id, relation) in CLI_BINDINGS.items():
        choices = {str(value) for value in CLI[option]}
        values = set(registry.fields[field_id].values)
        if relation == EQUAL:
            assert choices == values, option
        else:
            assert choices < values, option


def test_role_stage_route_and_reason_constants_stay_inside_their_fields() -> None:
    registry = load_registry()

    for name, (field_id, vocabulary) in SUBSET_BINDINGS.items():
        values = {str(value) for value in vocabulary}
        outside = sorted(values - set(registry.fields[field_id].values))
        assert values, name
        assert outside == [], name
    for reason, (category, stop) in auto_policy.ATTENTION_STOPS.items():
        assert stop in registry.fields["auto.stop_reason"].groups[category], reason


def _tampered(tmp_path: Path, mutate: Mutation) -> system_fields.SystemFieldsRegistry:
    document = copy.deepcopy(_shipped())
    mutate(document)
    return load_registry(_write(tmp_path, document))


def test_a_value_removed_from_the_registry_is_drift(tmp_path: Path) -> None:
    registry = _tampered(tmp_path, _drop(("fields", "batch.state", "values", "paused")))

    assert vocabulary_drift(registry, BINDINGS, GROUP_BINDINGS) == [
        "batch.state: BatchState has values the registry lacks ['paused']"
    ]


def test_a_value_added_to_the_registry_is_drift(tmp_path: Path) -> None:
    registry = _tampered(tmp_path, _add_value("retry.route", "reroute"))

    assert vocabulary_drift(registry, BINDINGS, GROUP_BINDINGS) == [
        "retry.route: RecoveryRoute lacks the registry values ['reroute']",
        "retry.route: RECOVERY_ROUTES lacks the registry values ['reroute']",
    ]


def test_a_changed_group_member_is_drift(tmp_path: Path) -> None:
    registry = _tampered(
        tmp_path,
        _set(
            ("fields", "batch.decision", "groups", "accepted"),
            ["accept", "override-warning", "retry"],
        ),
    )

    assert vocabulary_drift(registry, BINDINGS, GROUP_BINDINGS) == [
        "batch.decision: group 'accepted' is ['accept', 'override-warning', 'retry'] in "
        "the registry but ['accept', 'accept-conditional', 'override-warning'] in the code"
    ]


def test_unbound_and_unknown_bindings_are_drift() -> None:
    registry = load_registry()
    values = {key: value for key, value in BINDINGS.items() if key != "ci.verdict"}
    values["ci.outcome"] = {"ci_source.Outcome": get_args(ci_source.Outcome)}
    groups = {key: value for key, value in GROUP_BINDINGS.items() if key != "qa.stage"}
    groups["batch.state"] = {"terminal": constants.TERMINAL_BATCH_STATES, "open": []}
    groups["qa.stages"] = {"primary": constants.QA_PRIMARY_STAGE_NAMES}

    assert vocabulary_drift(registry, values, groups) == [
        "ci.verdict: no code vocabulary is bound to the registry field",
        "ci.outcome: ci_source.Outcome is bound to a field the registry lacks",
        "qa.stage: registry group 'primary' has no code binding",
        "batch.state: code group 'open' is not a registry group",
        "qa.stages: groups primary are bound to a field the registry lacks",
    ]
