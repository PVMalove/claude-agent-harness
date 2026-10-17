"""Explicit migration rules of historical ledger values, checked against systemFields.v1."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from harness.json_types import JsonObject
from harness.orchestration.core.system_fields import (
    REGISTRY_PATH,
    SystemFieldsError,
    load_registry,
)
from harness.orchestration.ledger import field_migration
from harness.orchestration.ledger.field_migration import (
    FIELD_BINDINGS,
    LEGACY_UNKNOWN,
    MAPPED,
    MIGRATION_RULES,
    UNREADABLE,
    FieldBinding,
    MigrationRule,
    classify,
    file_findings,
    rules_problems,
    rules_sha256,
)

REGISTRY = load_registry()


def _findings(directory: str, record: object) -> list[field_migration.Finding]:
    return file_findings(REGISTRY, directory, json.dumps(record).encode("utf-8"))


def test_the_shipped_bindings_and_rules_fit_the_registry() -> None:
    assert rules_problems(REGISTRY) == []
    assert field_migration.checked_registry().contract == "systemFields.v1"
    assert all(
        REGISTRY.fields[binding.field_id].automatic_decisions
        for binding in FIELD_BINDINGS
    )


@pytest.mark.parametrize(
    ("rule", "fragment"),
    [
        (MigrationRule("no.such_field", "x", "y", "e"), "unknown field"),
        (MigrationRule("report.check_result", "ok", "okay", "e"), "not a field value"),
        (
            MigrationRule("report.check_result", "pass", "pass", "e"),
            "maps a value to itself",
        ),
        (MIGRATION_RULES[2], "listed twice"),
    ],
)
def test_a_rule_that_does_not_fit_the_registry_is_refused(
    rule: MigrationRule, fragment: str
) -> None:
    problems = rules_problems(REGISTRY, rules=(*MIGRATION_RULES, rule))

    assert len(problems) == 1 and fragment in problems[0], problems


@pytest.mark.parametrize(
    ("binding", "fragment"),
    [
        (FieldBinding("batches", "x", "no.such_field"), "unknown field"),
        (
            FieldBinding("batches", "required_gates[]", "batch.required_gate"),
            "no decision",
        ),
    ],
)
def test_a_binding_to_an_unknown_or_undecided_field_is_refused(
    binding: FieldBinding, fragment: str
) -> None:
    problems = rules_problems(REGISTRY, bindings=(*FIELD_BINDINGS, binding))

    assert len(problems) == 1 and fragment in problems[0], problems


def test_a_rule_that_stops_fitting_the_shipped_registry_stops_the_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = (replace(MIGRATION_RULES[2], canonical="passed-ok"),)
    monkeypatch.setattr(field_migration, "MIGRATION_RULES", broken)

    with pytest.raises(SystemFieldsError, match="do not fit systemFields.v1"):
        field_migration.checked_registry()


@pytest.mark.parametrize("legacy", ["rate-limit", "429"])
def test_the_rate_limit_spellings_map_to_rate_limit(legacy: str) -> None:
    finding = classify(REGISTRY, "continuation.termination_reason", "reason", legacy)

    assert finding is not None
    assert (finding.disposition, finding.canonical) == (MAPPED, "rate_limit")
    # The rule wins over the registry, which still accepts the spelling as input.
    assert legacy in REGISTRY.fields["continuation.termination_reason"].values
    assert (
        classify(REGISTRY, "continuation.termination_reason", "r", "rate_limit") is None
    )


@pytest.mark.parametrize(
    ("legacy", "canonical"),
    [
        ("passed", "pass"),
        ("not-run", "not_run"),
        ("not_run_architect_read_only", "not_run"),
    ],
)
def test_a_historical_check_result_in_a_report_is_mapped(
    legacy: str, canonical: str
) -> None:
    report = {
        "outcome": "completed",
        "checks_run": [
            {"command": "make lint", "result": "pass"},
            {"command": "make test", "result": legacy},
        ],
        "blockers": {"status": "none", "details": ""},
    }

    [finding] = _findings("reports", report)

    assert finding.disposition == MAPPED
    assert (finding.location, finding.field_id) == (
        "checks_run[1].result",
        "report.check_result",
    )
    assert (finding.value, finding.canonical) == (legacy, canonical)


def test_the_same_spelling_outside_a_bound_location_is_left_alone() -> None:
    local_qa = {"result": "passed", "kind": "local-qa"}
    batch = {
        "state": "active",
        "dispatches": [{"dispatch_id": "dispatch-1", "state": "reported"}],
        "decisions": [{"decision": "continue-rate-limit"}],
        "continuations": [{"note": "termination_reason=rate-limit"}],
        "required_gates": ["make verify"],
    }

    assert _findings("reports", local_qa) == []
    assert _findings("batches", batch) == []
    assert _findings("checkpoints", {"state": "Weird"}) == []


@pytest.mark.parametrize("variant", ["Pass", "pass ", "pass_", "PASS", "ok"])
def test_a_variant_of_an_exact_value_is_legacy_unknown(variant: str) -> None:
    [finding] = _findings("reports", {"checks_run": [{"result": variant}]})

    assert finding.disposition == LEGACY_UNKNOWN
    assert finding.canonical is None
    assert finding.value == variant


@pytest.mark.parametrize("value", [None, 3, True, ["active"], {"state": "active"}])
def test_a_non_string_value_is_legacy_unknown(value: object) -> None:
    [finding] = _findings("batches", {"state": value})

    assert (finding.disposition, finding.location, finding.value) == (
        LEGACY_UNKNOWN,
        "state",
        value,
    )


def test_an_absent_location_is_never_defaulted() -> None:
    assert _findings("dispatches", {"state": "approved"}) == []
    assert _findings("reports", {"blockers": "none", "checks_run": "pass"}) == []
    assert _findings("batches", {"state": "active", "dispatches": "none"}) == []


def test_every_bound_location_of_a_record_is_checked() -> None:
    dispatch = {"state": "Approved", "role": "reviewer", "purpose": "work"}

    findings = _findings("dispatches", dispatch)

    assert [(f.location, f.field_id) for f in findings] == [
        ("state", "dispatch.state"),
        ("role", "pipeline.role"),
    ]


@pytest.mark.parametrize("raw", [b"{not json", b"\xff\xfe", b"[]", b'"text"'])
def test_a_file_that_is_not_a_json_object_is_unreadable(raw: bytes) -> None:
    [finding] = file_findings(REGISTRY, "batches", raw)

    assert finding.disposition == UNREADABLE
    assert (finding.location, finding.field_id, finding.value) == (None, None, None)


def test_the_rules_digest_follows_the_bound_values_and_nothing_else(
    tmp_path: Path,
) -> None:
    document: JsonObject = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    unbound = copy.deepcopy(document)
    unbound["fields"]["review.severity"]["values"]["extra"] = "Another value."
    bound = copy.deepcopy(document)
    bound["fields"]["dispatch.state"]["values"]["extra"] = "Another value."

    def digest(registry: JsonObject) -> str:
        path = tmp_path / "system_fields.v1.json"
        path.write_text(json.dumps(registry), encoding="utf-8")
        return rules_sha256(load_registry(path))

    assert digest(document) == rules_sha256(REGISTRY)
    assert digest(unbound) == rules_sha256(REGISTRY)
    assert digest(bound) != rules_sha256(REGISTRY)
