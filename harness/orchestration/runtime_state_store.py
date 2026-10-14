"""Durable runtime state store (``runtimeState.v1``), one record per batch.

The store keeps operational progress beside the lifecycle ledger, never inside it: the ledger
stays the owner of lifecycle and approvals, and nothing here writes a ledger record.  Not yet
wired into dispatch, intent or reconciliation (#684, #685).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import cast

from harness.configuration_schema import resolve_ref, value_problems
from harness.gate_runner.gate_runner import sanitise
from harness.json_types import JsonObject

from .core.runtime_state_contract import RUNTIME_STATE_CONTRACT, transition_problem
from .core.system_fields import enum_problem, load_registry

SCHEMA_PATH = Path(__file__).with_name("runtime_state.v1.json")
SYSTEM_FIELD = "x-system-field"
DETAIL_LIMIT = 240
# An absolute POSIX or Windows path, not the tail of a relative one.
_ABSOLUTE_PATH = re.compile(r"(?<![\w.~-])(?:[A-Za-z]:[\\/]|[\\/])[^\s'\"]*")
_SETTLED_PHASES = frozenset({"ledger-committed", "committed"})


@cache
def _schema() -> JsonObject:
    value: JsonObject = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return value


def _without_system_enums(node: object) -> object:
    """The schema with each registry-bound ``enum`` dropped: those values get the registry's
    exact-spelling diagnostic instead of the generic schema one."""
    if isinstance(node, dict):
        return {
            key: _without_system_enums(child)
            for key, child in node.items()
            if not (key == "enum" and SYSTEM_FIELD in node)
        }
    if isinstance(node, list):
        return [_without_system_enums(child) for child in node]
    return node


@cache
def _structure() -> JsonObject:
    return cast(JsonObject, _without_system_enums(_schema()))


@cache
def field_values(field_id: str) -> tuple[str, ...]:
    """The exact values of a systemFields.v1 field."""
    return tuple(load_registry().fields[field_id].values)


def sanitize_detail(text: str) -> str:
    """Secrets masked, absolute paths dropped and length bounded; a fixed point of itself."""
    cleaned = _ABSOLUTE_PATH.sub("<path>", sanitise(text)).strip()
    return cleaned[:DETAIL_LIMIT].strip()


def compatibility_problem(record: object) -> str | None:
    """A record of another or no contract is never read with defaults filled in."""
    if not isinstance(record, dict):
        return "runtime state record must be a JSON object"
    if record.get("contract") != RUNTIME_STATE_CONTRACT:
        return (
            f"runtime state contract must be {RUNTIME_STATE_CONTRACT!r}, "
            f"got {record.get('contract')!r}"
        )
    return None


def _enum_problems(value: object, node: object, path: str) -> list[str]:
    if not isinstance(node, dict):
        return []
    if "$ref" in node:
        node = resolve_ref(_schema(), node["$ref"])
    if SYSTEM_FIELD in node:
        problem = enum_problem(path, value, field_values(node[SYSTEM_FIELD]))
        return [problem] if problem else []
    if not isinstance(value, dict):
        return []
    problems: list[str] = []
    for key, child in node.get("properties", {}).items():
        if key in value:
            problems.extend(_enum_problems(value[key], child, f"{path}.{key}"))
    return problems


def record_problems(record: object) -> list[str]:
    """Structure, closed enums and the contract of one record, without its predecessor."""
    if (problem := compatibility_problem(record)) is not None:
        return [problem]
    problems = value_problems(record, _structure(), _structure())
    problems += _enum_problems(record, _schema(), "record")
    return problems


def _timestamp_problem(value: object, label: str) -> str | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return f"{label} must be an ISO-8601 timestamp"
    return None if parsed.tzinfo else f"{label} must include a timezone"


def _operation_problems(
    operation: JsonObject | None, previous: JsonObject | None
) -> list[str]:
    if operation is None:
        if previous is not None and previous["phase"] != "committed":
            return ["an operation that is not committed cannot be dropped"]
        return []
    problems: list[str] = []
    if previous is not None and operation["operation_id"] == previous["operation_id"]:
        for key in (
            "kind",
            "idempotency_key",
            "expected_before_sha256",
            "intended_after_sha256",
        ):
            if operation[key] != previous[key]:
                problems.append(f"operation {key} is immutable")
        for field_id, after, before in (
            ("runtime.operation_phase", operation["phase"], previous["phase"]),
            (
                "runtime.effect_state",
                operation["effect"]["state"],
                previous["effect"]["state"],
            ),
        ):
            if (problem := transition_problem(field_id, before, after)) is not None:
                problems.append(problem)
        return problems
    if previous is not None and previous["phase"] != "committed":
        problems.append(
            "a new operation cannot start before the previous one is committed"
        )
    if (
        operation["phase"] != "prepared"
        or operation["effect"]["state"] != "not-started"
    ):
        problems.append("a new operation must start as prepared and not-started")
    return problems


def _settled_problems(record: JsonObject) -> list[str]:
    """Legacy metadata or the ``unknown`` sentinel never stands in for a confirmed effect."""
    operation = record["operation"]
    if operation is None or operation["phase"] not in _SETTLED_PHASES:
        return []
    problems: list[str] = []
    if operation["effect"]["state"] != "confirmed":
        problems.append(f"phase {operation['phase']!r} requires a confirmed effect")
    if not record["ledger"]["generation"] or not record["ledger"]["record_hashes"]:
        problems.append(
            f"phase {operation['phase']!r} requires the ledger generation and record hashes"
        )
    return problems


def validate_record(record: object, previous: JsonObject | None) -> list[str]:
    """Every reason ``record`` may not follow ``previous`` (``None`` when creating); pure."""
    problems = record_problems(record)
    if problems or not isinstance(record, dict):
        return problems
    for label, value in (
        ("updated_at", record["updated_at"]),
        (
            "operation.effect.observed_at",
            (record["operation"] or {}).get("effect", {}).get("observed_at"),
        ),
        (
            "last_confirmed_boundary.confirmed_at",
            (record["last_confirmed_boundary"] or {}).get("confirmed_at"),
        ),
    ):
        if (problem := _timestamp_problem(value, label)) is not None:
            problems.append(problem)
    failure = record["failure"]
    if (
        failure is not None
        and sanitize_detail(failure["message"]) != failure["message"]
    ):
        problems.append(
            "failure.message must be sanitized: no secrets, absolute paths or excess length"
        )
    problems += _settled_problems(record)
    if previous is None:
        if record["revision"] != 1:
            problems.append("a new runtime state record starts at revision 1")
        if record["state"] != "ready":
            problems.append("a new runtime state record starts in state 'ready'")
        return problems + _operation_problems(record["operation"], None)
    if record["identity"] != previous["identity"]:
        problems.append("identity is immutable")
    if record["revision"] != previous["revision"] + 1:
        problems.append(
            f"revision must be {previous['revision'] + 1}, got {record['revision']}"
        )
    if previous["state"] == "completed":
        problems.append("a completed runtime state record is immutable")
    elif (
        problem := transition_problem(
            "runtime.state", previous["state"], record["state"]
        )
    ) is not None:
        problems.append(problem)
    return problems + _operation_problems(record["operation"], previous["operation"])
