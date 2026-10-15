"""Explicit migration rules of historical ledger values to the ``systemFields.v1`` registry.

A ledger generation written by an earlier harness may hold a value that the current registry
spells differently.  This module names, without any I/O of its own, the ledger locations bound to
a registry field and the explicit rules that map one known historical spelling of a field to its
canonical value.  Nothing here normalizes: a value that is neither an exact registry value nor the
legacy spelling of a rule is ``legacy-unknown``, whatever it resembles.

The rules are not registry aliases -- the registry never supplies aliases for machine records.  A
rule records a fact about history, and the registry it maps into checks it.  A rule wins over
registry membership, so a spelling a rule retires is ``mapped`` even while the registry still
accepts it as input.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass

from harness.errors import INTERNAL_INVARIANT_REMEDY
from harness.orchestration.core.system_fields import (
    SystemFieldsError,
    SystemFieldsRegistry,
    load_registry,
    near_miss,
)

# The `ledger.migration_disposition` values: how the migration handles one finding.
MIGRATION_DISPOSITIONS = ("mapped", "legacy-unknown", "unreadable")
MAPPED, LEGACY_UNKNOWN, UNREADABLE = MIGRATION_DISPOSITIONS
# A batch's completion reports: the files its `dispatches[].report` entries name.
COMPLETION_REPORTS = "reports"


@dataclass(frozen=True)
class FieldBinding:
    """One ledger location bound to a registry field: the record directory and the dotted path
    of the value in a record, where ``[]`` stands for every item of a list."""

    directory: str
    location: str
    field_id: str


# Only fields that drive an automatic decision are bound: free text such as `required_gates`
# drives none, so a historical spelling there never quarantines a batch.
FIELD_BINDINGS: tuple[FieldBinding, ...] = (
    FieldBinding("batches", "state", "batch.state"),
    FieldBinding("batches", "dispatches[].state", "dispatch.state"),
    FieldBinding("batches", "dispatches[].role", "pipeline.role"),
    FieldBinding("batches", "approval_policy", "config.approval_policy"),
    FieldBinding("plans", "approval_policy", "config.approval_policy"),
    FieldBinding("dispatches", "state", "dispatch.state"),
    FieldBinding("dispatches", "role", "pipeline.role"),
    FieldBinding("dispatches", "purpose", "dispatch.purpose"),
    FieldBinding("dispatch-status", "state", "dispatch.state"),
    FieldBinding(COMPLETION_REPORTS, "outcome", "report.outcome"),
    FieldBinding(COMPLETION_REPORTS, "checks_run[].result", "report.check_result"),
    FieldBinding(COMPLETION_REPORTS, "blockers.status", "report.evidence_status"),
    FieldBinding(COMPLETION_REPORTS, "risks.status", "report.evidence_status"),
)


@dataclass(frozen=True)
class MigrationRule:
    """One known historical spelling of a field and the canonical value it maps to."""

    field_id: str
    legacy: str
    canonical: str
    evidence: str


# `continuation.termination_reason` has rules but no ledger location: the coordinator keeps it
# only inside a free-text authorization note, which no rule rewrites.  Its two rules are checked
# against the registry like every other one, yet `ledger migrate` never applies them.
MIGRATION_RULES: tuple[MigrationRule, ...] = (
    MigrationRule(
        "continuation.termination_reason",
        "rate-limit",
        "rate_limit",
        "systemFields.v1 lists 'rate-limit' as another spelling of the rate limit",
    ),
    MigrationRule(
        "continuation.termination_reason",
        "429",
        "rate_limit",
        "systemFields.v1 lists the HTTP 429 answer as the rate limit",
    ),
    MigrationRule(
        "report.check_result",
        "passed",
        "pass",
        "CHANGELOG 1.4.2 stored a passed check as 'pass'; 1.4.4 refuses 'passed'",
    ),
    MigrationRule(
        "report.check_result",
        "not-run",
        "not_run",
        "CHANGELOG 1.4.2 stored a check that never ran as 'not_run'; 1.4.4 refuses 'not-run'",
    ),
    MigrationRule(
        "report.check_result",
        "not_run_architect_read_only",
        "not_run",
        "CHANGELOG 1.4.4 refuses the read-only architect's 'not_run_architect_read_only'",
    ),
)


@dataclass(frozen=True)
class Finding:
    """One value the migration does not keep as it is: a ``mapped`` historical value, a
    ``legacy-unknown`` value, or an ``unreadable`` file (no location, field or value)."""

    location: str | None
    field_id: str | None
    value: object
    disposition: str
    canonical: str | None
    reason: str


def rules_problems(
    registry: SystemFieldsRegistry,
    bindings: tuple[FieldBinding, ...] = FIELD_BINDINGS,
    rules: tuple[MigrationRule, ...] = MIGRATION_RULES,
) -> list[str]:
    """Every reason ``bindings`` and ``rules`` do not fit ``registry``; empty when they do."""
    problems: list[str] = []
    for binding in bindings:
        field = registry.fields.get(binding.field_id)
        where = f"binding {binding.directory}:{binding.location}"
        if field is None:
            problems.append(f"{where} names the unknown field {binding.field_id}")
        elif not field.automatic_decisions:
            problems.append(
                f"{where} names {binding.field_id}, which drives no decision"
            )
    seen: set[tuple[str, str]] = set()
    for rule in rules:
        where = f"rule {rule.field_id}:{rule.legacy!r}"
        field = registry.fields.get(rule.field_id)
        if field is None:
            problems.append(f"{where} names an unknown field")
        elif rule.canonical not in field.values:
            problems.append(f"{where} maps to {rule.canonical!r}, not a field value")
        if rule.legacy == rule.canonical:
            problems.append(f"{where} maps a value to itself")
        if (rule.field_id, rule.legacy) in seen:
            problems.append(f"{where} is listed twice")
        seen.add((rule.field_id, rule.legacy))
    return problems


def checked_registry() -> SystemFieldsRegistry:
    """The shipped registry, once the bindings and rules are checked against it."""
    registry = load_registry()
    if problems := rules_problems(registry, FIELD_BINDINGS, MIGRATION_RULES):
        raise SystemFieldsError(
            "the ledger migration rules do not fit systemFields.v1: "
            + "; ".join(problems),
            remedy=INTERNAL_INVARIANT_REMEDY,
        )
    return registry


def rules_sha256(registry: SystemFieldsRegistry) -> str:
    """The digest of the bindings, the rules and the values of every bound field: a change of
    any of them makes an earlier migration stale."""
    bound = sorted({binding.field_id for binding in FIELD_BINDINGS})
    document = {
        "bindings": [
            [binding.directory, binding.location, binding.field_id]
            for binding in FIELD_BINDINGS
        ],
        "rules": [
            [rule.field_id, rule.legacy, rule.canonical] for rule in MIGRATION_RULES
        ],
        "values": {
            field_id: list(registry.fields[field_id].values) for field_id in bound
        },
    }
    text = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def classify(
    registry: SystemFieldsRegistry, field_id: str, location: str, value: object
) -> Finding | None:
    """The finding of ``value`` at a location bound to ``field_id``; ``None`` for an exact
    registry value that no rule retires."""
    for rule in MIGRATION_RULES:
        if rule.field_id == field_id and value == rule.legacy:
            return Finding(
                location, field_id, value, MAPPED, rule.canonical, rule.evidence
            )
    allowed = registry.fields[field_id].values
    if isinstance(value, str) and value in allowed:
        return None
    exact = near_miss(value, allowed)
    reason = (
        f"not a {field_id} value; it differs from {' or '.join(exact)} only by case, spaces "
        "or a separator, which is never normalized"
        if exact
        else f"neither a {field_id} value nor the legacy spelling of a migration rule"
    )
    return Finding(location, field_id, value, LEGACY_UNKNOWN, None, reason)


def file_findings(
    registry: SystemFieldsRegistry, directory: str, raw: bytes
) -> list[Finding]:
    """The findings of one record file of ``directory`` from its raw bytes; a file that is not
    a JSON object is one ``unreadable`` finding."""
    try:
        record: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        record, problem = None, "the file is not UTF-8 JSON"
    else:
        problem = "" if isinstance(record, dict) else "the file is not a JSON object"
    if problem:
        return [Finding(None, None, None, UNREADABLE, None, problem)]
    findings: list[Finding] = []
    for binding in FIELD_BINDINGS:
        if binding.directory != directory:
            continue
        for location, value in values_at(record, binding.location):
            finding = classify(registry, binding.field_id, location, value)
            if finding is not None:
                findings.append(finding)
    return findings


def values_at(record: object, location: str) -> Iterator[tuple[str, object]]:
    """Every concrete location and value the bound ``location`` names in ``record``.  A missing
    key or a non-list under ``[]`` names nothing: an absent value is never defaulted."""

    def walk(
        node: object, parts: list[str], path: list[str]
    ) -> Iterator[tuple[str, object]]:
        if not parts:
            yield ".".join(path), node
            return
        head, rest = parts[0], parts[1:]
        key = head.removesuffix("[]")
        if not isinstance(node, dict) or key not in node:
            return
        if head == key:
            yield from walk(node[key], rest, [*path, key])
        elif isinstance(items := node[key], list):
            for index, item in enumerate(items):
                yield from walk(item, rest, [*path, f"{key}[{index}]"])

    yield from walk(record, location.split("."), [])
