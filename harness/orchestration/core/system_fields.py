"""The ``systemFields.v1`` registry of orchestration system fields.

``system_fields.v1.json`` next to the orchestration package is the runtime-neutral list of every
closed vocabulary the coordinator branches on, validates or records: the exact machine values of
each field, their semantics, the sentinels and whether the field drives an automatic decision.
The code keeps its own literal vocabularies (``StrEnum`` classes, value sets, ``Literal``
aliases); tests check them against this registry.  Nothing reads the file at import time.

The module depends only on the standard library and :mod:`harness.errors`, so ``contract.py``
and ``core/config.py`` may use it without an import cycle.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from harness.errors import HarnessError

CONTRACT_VERSION = "systemFields.v1"
# Resolved next to the orchestration package, so the installed `.harness/orchestration` copy
# finds its own registry.
REGISTRY_PATH = Path(__file__).resolve().parent.parent / "system_fields.v1.json"
FIELD_ID = re.compile(r"[a-z][a-z0-9_]*(?:[.][a-z][a-z0-9_]*)+")
# A machine value: lower-case words joined by single separators.  The dot separates the policy
# section from its key in budget stop reasons, such as `retry_policy.max_developer_retries`.
MACHINE_VALUE = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*")
SENTINEL_VALUES = ("unknown", "unavailable", "unsupported")
UNRECOGNIZED_POLICIES = ("reject", "not-recognized")
REGISTRY_KEYS = frozenset(
    {"contract", "description", "unrecognized_policies", "fields"}
)
FIELD_REQUIRED_KEYS = frozenset(
    {"description", "automatic_decisions", "unrecognized", "values"}
)
FIELD_OPTIONAL_KEYS = frozenset({"default", "groups", "sentinels"})


class SystemFieldsError(HarnessError):
    """The system-fields registry is missing, unreadable or malformed."""


@dataclass(frozen=True)
class SystemField:
    """One registered system field: its exact values in code order and how they are handled."""

    field_id: str
    description: str
    automatic_decisions: bool
    unrecognized: str
    values: Mapping[str, str]
    default: str | None
    groups: Mapping[str, tuple[str, ...]]
    sentinels: tuple[str, ...]

    @property
    def allowed(self) -> tuple[str, ...]:
        """The exact machine values, in registry order."""
        return tuple(self.values)


@dataclass(frozen=True)
class SystemFieldsRegistry:
    """A loaded and validated ``systemFields`` registry."""

    contract: str
    description: str
    unrecognized_policies: Mapping[str, str]
    fields: Mapping[str, SystemField]


def _error(path: Path, problem: str, remedy: str) -> SystemFieldsError:
    return SystemFieldsError(
        f"system-fields registry {path} is invalid: {problem}",
        remedy=f"fix {path}: {remedy}",
    )


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate key {key!r}")
        document[key] = value
    return document


def _read(path: Path) -> object:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SystemFieldsError(
            f"system-fields registry {path} cannot be read: {exc.strerror or exc}",
            remedy=f"restore {path.name} next to the orchestration package by reinstalling "
            f"the harness ({CONTRACT_VERSION})",
        ) from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _error(path, "it is not UTF-8 text", "save it as UTF-8") from exc
    try:
        document: object = json.loads(text, object_pairs_hook=_unique_keys)
    except ValueError as exc:
        raise _error(
            path,
            f"it is not valid JSON ({exc})",
            "write one JSON object without duplicate keys",
        ) from exc
    return document


def _object(path: Path, value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _error(path, f"{where} is not an object", f"make {where} a JSON object")
    return {str(key): item for key, item in value.items()}


def _text(path: Path, value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error(path, f"{where} is not a non-empty string", f"give {where} a text")
    return value


def _exact_keys(
    path: Path,
    document: Mapping[str, object],
    where: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> None:
    missing = sorted(required - set(document))
    extra = sorted(set(document) - required - optional)
    if missing or extra:
        allowed = ", ".join(sorted(required | optional))
        raise _error(
            path,
            f"{where} has missing keys {missing} and unknown keys {extra}",
            f"give {where} exactly the keys: {allowed}",
        )


def _member(path: Path, value: object, values: Mapping[str, str], where: str) -> str:
    if not isinstance(value, str) or value not in values:
        raise _error(
            path,
            f"{where} {value!r} is not one of the field's values",
            f"use one of: {', '.join(values)}",
        )
    return value


def _members(
    path: Path, value: object, values: Mapping[str, str], where: str
) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise _error(
            path, f"{where} is not a non-empty list", f"make {where} a list of values"
        )
    members = tuple(_member(path, item, values, f"{where} member") for item in value)
    if len(set(members)) != len(members):
        raise _error(
            path, f"{where} repeats a value", f"list each value of {where} once"
        )
    return members


def _field(path: Path, field_id: str, value: object) -> SystemField:
    where = f"field {field_id!r}"
    if FIELD_ID.fullmatch(field_id) is None:
        raise _error(
            path,
            f"{where} is not a dotted lower-case field id",
            "name the field as <area>.<name>, for example batch.state",
        )
    document = _object(path, value, where)
    _exact_keys(path, document, where, FIELD_REQUIRED_KEYS, FIELD_OPTIONAL_KEYS)
    automatic = document["automatic_decisions"]
    if not isinstance(automatic, bool):
        raise _error(
            path,
            f"{where} automatic_decisions is not a boolean",
            "set it to true or false",
        )
    unrecognized = document["unrecognized"]
    if unrecognized not in UNRECOGNIZED_POLICIES:
        raise _error(
            path,
            f"{where} unrecognized {unrecognized!r} is not a policy",
            f"set it to one of: {', '.join(UNRECOGNIZED_POLICIES)}",
        )
    raw_values = _object(path, document["values"], f"{where} values")
    if not raw_values:
        raise _error(path, f"{where} has no values", "list at least one value")
    values: dict[str, str] = {}
    for name, semantics in raw_values.items():
        if MACHINE_VALUE.fullmatch(name) is None:
            raise _error(
                path,
                f"{where} value {name!r} is not an exact machine value",
                "spell a value in lower case, words joined by single '-', '_' or '.'",
            )
        values[name] = _text(path, semantics, f"{where} value {name!r} semantics")
    default = (
        _member(path, document["default"], values, f"{where} default")
        if "default" in document
        else None
    )
    groups: dict[str, tuple[str, ...]] = {}
    if "groups" in document:
        raw_groups = _object(path, document["groups"], f"{where} groups")
        if not raw_groups:
            raise _error(
                path, f"{where} groups is empty", "remove groups or name a group"
            )
        for name, members in raw_groups.items():
            if MACHINE_VALUE.fullmatch(name) is None:
                raise _error(
                    path,
                    f"{where} group {name!r} is not a machine name",
                    "name a group in lower case, words joined by single '-', '_' or '.'",
                )
            groups[name] = _members(path, members, values, f"{where} group {name!r}")
    sentinels: tuple[str, ...] = ()
    if "sentinels" in document:
        sentinels = _members(path, document["sentinels"], values, f"{where} sentinels")
        for name in sentinels:
            if name not in SENTINEL_VALUES:
                raise _error(
                    path,
                    f"{where} sentinel {name!r} is not a sentinel name",
                    f"list only these sentinel names: {', '.join(SENTINEL_VALUES)}",
                )
    unlisted = [
        name for name in values if name in SENTINEL_VALUES and name not in sentinels
    ]
    if unlisted:
        raise _error(
            path,
            f"{where} value {unlisted[0]!r} is a sentinel name but not listed in sentinels",
            f"add {unlisted[0]!r} to the field's sentinels and state its safe handling",
        )
    return SystemField(
        field_id=field_id,
        description=_text(path, document["description"], f"{where} description"),
        automatic_decisions=automatic,
        unrecognized=str(unrecognized),
        values=MappingProxyType(values),
        default=default,
        groups=MappingProxyType(groups),
        sentinels=sentinels,
    )


def load_registry(path: Path = REGISTRY_PATH) -> SystemFieldsRegistry:
    """Read and strictly validate the registry; any deviation is a :class:`SystemFieldsError`."""
    document = _object(path, _read(path), "the registry")
    _exact_keys(path, document, "the registry", REGISTRY_KEYS)
    contract = document["contract"]
    if contract != CONTRACT_VERSION:
        raise _error(
            path,
            f"contract {contract!r} is not {CONTRACT_VERSION!r}",
            f"set contract to {CONTRACT_VERSION!r} or load the registry of that version",
        )
    policies = _object(path, document["unrecognized_policies"], "unrecognized_policies")
    _exact_keys(
        path, policies, "unrecognized_policies", frozenset(UNRECOGNIZED_POLICIES)
    )
    fields = _object(path, document["fields"], "fields")
    if not fields:
        raise _error(path, "fields is empty", "register at least one field")
    return SystemFieldsRegistry(
        contract=CONTRACT_VERSION,
        description=_text(path, document["description"], "description"),
        unrecognized_policies=MappingProxyType(
            {
                name: _text(path, policies[name], f"unrecognized_policies {name!r}")
                for name in UNRECOGNIZED_POLICIES
            }
        ),
        fields=MappingProxyType(
            {name: _field(path, name, value) for name, value in fields.items()}
        ),
    )
