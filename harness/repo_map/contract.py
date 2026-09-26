"""Проверка JSON-контракта Repo Map по поставляемой схеме без runtime-зависимостей."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _schema() -> dict[str, object]:
    """Загрузить схему рядом с CLI один раз на процесс."""
    value: object = json.loads(Path(__file__).with_name("repo_map.schema.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("Repo Map schema must be a JSON object")
    return value


def _has_type(value: object, expected: str) -> bool:
    """Проверить примитивный тип JSON Schema, используемый контрактом."""
    return {
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
        "string": lambda: isinstance(value, str),
        "integer": lambda: type(value) is int,
        "null": lambda: value is None,
    }[expected]()


def _error(value: object, schema: dict[str, object], path: str) -> str | None:
    """Вернуть первое нарушение правил из repo_map.schema.json."""
    expected = schema.get("type")
    if isinstance(expected, str) and not _has_type(value, expected):
        return f"{path} must be {expected}"
    if isinstance(expected, list) and not any(
        isinstance(item, str) and _has_type(value, item) for item in expected
    ):
        return f"{path} has an unexpected type"
    if "const" in schema and value != schema["const"]:
        return f"{path} must equal {schema['const']!r}"
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        return f"{path} must be one of {enum!r}"
    minimum = schema.get("minimum")
    if type(value) is int and type(minimum) is int and value < minimum:
        return f"{path} must be >= {minimum}"
    min_length = schema.get("minLength")
    if isinstance(value, str) and type(min_length) is int and len(value) < min_length:
        return f"{path} is too short"
    if isinstance(value, dict):
        required = schema.get("required", [])
        if isinstance(required, list):
            for field in required:
                if isinstance(field, str) and field not in value:
                    return f"{path}.{field} is required"
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for field, item in value.items():
                if not isinstance(field, str):
                    return f"{path} has a non-string field"
                child = properties.get(field)
                if child is None:
                    if schema.get("additionalProperties") is False:
                        return f"{path}.{field} is not allowed"
                elif isinstance(child, dict):
                    problem = _error(item, child, f"{path}.{field}")
                    if problem is not None:
                        return problem
    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                problem = _error(item, items, f"{path}[{index}]")
                if problem is not None:
                    return problem
    return None


def validation_error(payload: object) -> str | None:
    """Вернуть ошибку контракта либо ``None`` для корректной карты."""
    problem = _error(payload, _schema(), "$repo_map")
    if problem is not None or not isinstance(payload, dict):
        return problem
    tier = payload.get("tier")
    parser = payload.get("parser")
    if (tier, parser) not in {("full", "bundle"), ("minimal", "path-only")}:
        return "$repo_map tier and parser disagree"
    return None
