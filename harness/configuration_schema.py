"""Dependency-free validation of the JSON Schema vocabulary used by Harness configs.

Business rules remain in Health and the orchestration contract. This module checks
the editor-facing constraints as well, including conditional and nested fields.
"""

from __future__ import annotations

import json
from difflib import get_close_matches
import math
import re
from pathlib import Path

from harness.configuration import parse_object
from harness.json_types import JsonObject
from harness.diagnostics import Diagnostic

PACKAGE = Path(__file__).resolve().parent  # holds the shipped schemas and templates


def _same(left: object, right: object) -> bool:
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(
        right, sort_keys=True, allow_nan=False
    )


def resolve_ref(schema: JsonObject, reference: object) -> JsonObject:
    """Follow a local ``#/...`` reference; other references are unsupported."""
    if not isinstance(reference, str) or not reference.startswith("#/"):
        raise ValueError("Поддерживаются только локальные ссылки схемы")
    target: object = schema
    for part in reference[2:].split("/"):
        if not isinstance(target, dict):
            raise ValueError("Повреждённая ссылка схемы")
        key = part.replace("~1", "/").replace("~0", "~")
        if key not in target:
            raise ValueError("Повреждённая ссылка схемы")
        target = target[key]
    if not isinstance(target, dict):
        raise ValueError("Повреждённая ссылка схемы")
    return target


def _check(
    value: object,
    node: object,
    schema: JsonObject,
    path: str,
    depth: int,
    parts: tuple[str | int, ...] = (),
    filename: str = "configuration",
) -> list[Diagnostic]:
    def issue(
        message: str,
        *,
        at: tuple[str | int, ...] = parts,
        received: object = value,
        expected: object = node,
    ) -> Diagnostic:
        constraints = (
            {
                key: expected[key]
                for key in (
                    "type",
                    "enum",
                    "const",
                    "minimum",
                    "maximum",
                    "minLength",
                    "pattern",
                    "required",
                )
                if key in expected
            }
            if isinstance(expected, dict)
            else expected
        )
        choices = expected.get("enum", []) if isinstance(expected, dict) else []
        suggestion = (
            get_close_matches(
                received,
                [choice for choice in choices if isinstance(choice, str)],
                n=3,
                cutoff=0.7,
            )
            if isinstance(received, str)
            else []
        )
        return Diagnostic(
            "config-schema",
            filename,
            at,
            path + ": " + message,
            constraints,
            received,
            suggestion or None,
        )

    if depth > 64:
        return [issue("слишком глубокая конфигурация")]
    if node is True:
        return []
    if node is False:
        return [issue("поле запрещено схемой")]
    if not isinstance(node, dict):
        raise ValueError("Некорректная схема конфигурации")
    if "$ref" in node:
        return _check(
            value,
            resolve_ref(schema, node["$ref"]),
            schema,
            path,
            depth + 1,
            parts,
            filename,
        )
    problems: list[Diagnostic] = []

    def check(
        item: object,
        child: object,
        location: str = path,
        at: tuple[str | int, ...] = parts,
    ) -> list[Diagnostic]:
        return _check(item, child, schema, location, depth + 1, at, filename)

    for child in node.get("allOf", []):
        problems.extend(check(value, child))
    alternatives = node.get("anyOf")
    if alternatives is not None and not any(
        not check(value, child) for child in alternatives
    ):
        problems.append(issue("заполните один из допустимых наборов связанных полей"))
    if "not" in node and not check(value, node["not"]):
        problems.append(issue("сочетание полей запрещено"))
    if "if" in node:
        branch = "else" if check(value, node["if"]) else "then"
        if branch in node:
            problems.extend(check(value, node[branch]))
    kinds = node.get("type")
    if kinds is not None:
        valid = {
            "string": isinstance(value, str),
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "null": value is None,
            "boolean": isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        }
        if not any(
            valid.get(kind, False)
            for kind in ([kinds] if isinstance(kinds, str) else kinds)
        ):
            return problems + [issue("неверный тип поля")]
    if "const" in node and not _same(value, node["const"]):
        problems.append(issue("требуется фиксированное значение схемы"))
    if "enum" in node and not any(_same(value, choice) for choice in node["enum"]):
        problems.append(issue("значение вне допустимых вариантов"))
    if isinstance(value, dict):
        for key in node.get("required", []):
            if key not in value:
                problems.append(
                    Diagnostic(
                        "config-schema",
                        filename,
                        (*parts, key),
                        path + "." + key + ": обязательное поле отсутствует",
                        "required",
                        None,
                    )
                )
        if len(value) < node.get("minProperties", 0) or len(value) > node.get(
            "maxProperties", len(value)
        ):
            problems.append(issue("неверное число полей объекта"))
        properties = node.get("properties", {})
        for key, item in value.items():
            child_parts = (*parts, key)
            if "propertyNames" in node:
                problems.extend(
                    check(key, node["propertyNames"], path + "." + key, child_parts)
                )
            child = properties.get(key, node.get("additionalProperties", True))
            problems.extend(check(item, child, path + "." + key, child_parts))
    elif isinstance(value, list):
        if len(value) < node.get("minItems", 0):
            problems.append(issue("недостаточно элементов списка"))
        if node.get("uniqueItems") and len(
            {json.dumps(v, sort_keys=True, allow_nan=False) for v in value}
        ) != len(value):
            problems.append(issue("повторяющиеся элементы"))
        items = node.get("items", True)
        extra = node.get("additionalItems", True)
        for index, item in enumerate(value):
            child = items
            if isinstance(items, list):  # draft-07 tuple form
                child = items[index] if index < len(items) else extra
            problems.extend(check(item, child, f"{path}[{index}]", (*parts, index)))
    elif isinstance(value, str):
        if len(value) < node.get("minLength", 0) or len(value) > node.get(
            "maxLength", len(value)
        ):
            problems.append(issue("неверная длина строки"))
        if "pattern" in node and re.search(node["pattern"], value) is None:
            problems.append(issue("неверный формат строки"))
        if node.get("format") == "regex":
            try:
                re.compile(value)
            except re.error:
                problems.append(issue("неверное регулярное выражение"))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            problems.append(issue("число должно быть конечным"))
        if value < node.get("minimum", value) or value > node.get("maximum", value):
            problems.append(issue("число вне допустимого диапазона"))
        if "exclusiveMinimum" in node and value <= node["exclusiveMinimum"]:
            problems.append(issue("число должно быть больше нижней границы"))
    return problems


def schema_file(repo: Path, name: str) -> Path:
    """Schema of the target's installed capability, else the one shipped with Harness."""
    relative = (
        "project.schema.json"
        if name == "project.json"
        else "orchestration/orchestration.schema.json"
    )
    installed = repo / ".harness" / relative
    if installed.is_file():
        return installed
    shipped = PACKAGE / ("project/" + relative if name == "project.json" else relative)
    if not shipped.is_file():
        raise ValueError("Схема отсутствует; обновите capability Harness")
    return shipped


def load_schema(repo: Path, name: str) -> JsonObject:
    return parse_object(schema_file(repo, name).read_bytes(), "schema")


def value_problems(value: object, node: JsonObject, schema: JsonObject) -> list[str]:
    """Problems of one value against one node of ``schema``, labelled ``значение``."""
    try:
        return [item.message for item in _check(value, node, schema, "значение", 0)]
    except (KeyError, TypeError, RecursionError):
        raise ValueError("Повреждённая схема конфигурации; обновите Harness") from None


def schema_problems(repo: Path, name: str, document: JsonObject) -> list[str]:
    try:
        return [item.message for item in schema_diagnostics(repo, name, document)]
    except (KeyError, TypeError, RecursionError):
        raise ValueError("Повреждённая схема конфигурации; обновите Harness") from None


def schema_diagnostics(repo: Path, name: str, document: JsonObject) -> list[Diagnostic]:
    """Structured constraints with exact JSON pointers; same engine as editor checks."""
    schema = load_schema(repo, name)
    try:
        return _check(document, schema, schema, name, 0, (), name)
    except (KeyError, TypeError, RecursionError):
        raise ValueError("Повреждённая схема конфигурации; обновите Harness") from None
