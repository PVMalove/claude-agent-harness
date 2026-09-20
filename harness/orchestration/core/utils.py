"""Leaf helpers of the orchestration coordinator: the JSON boundary type, the coordinator's own
error, and the small pure functions every layer above needs.

Nothing here reads project configuration, touches git, or opens the ledger, so `core.config`,
`ledger` and `workflow` may all import it without a cycle.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeGuard

from harness.errors import HarnessError


JsonObject = dict[str, Any]  # type: ignore[explicit-any]  # dynamic JSON boundary: ledger/config/report payloads are json.loads output validated at runtime by the *_FIELDS sets


class CoordinatorError(HarnessError):
    """A request that must fail without advancing coordinator state."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _non_empty(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and bool(value.strip())


def _read_object(path: Path, label: str) -> JsonObject:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CoordinatorError(f"{label} is not valid JSON", remedy=f"fix the JSON syntax in {label}") from exc
    if not isinstance(value, dict):
        raise CoordinatorError(f"{label} must be a JSON object", remedy=f"set {label} to a JSON object")
    return value


def _strings(value: object, label: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value) or not all(_non_empty(item) for item in value):
        raise CoordinatorError(f"{label} must be a list of non-empty strings", remedy=f"set {label} to a list of non-empty strings")
    return list(value)


def _canonical(value: JsonObject) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _safe_id(value: object, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"(?:batch|dispatch|risk|context-package|checkpoint)-[0-9a-f-]+", value) is None:
        raise CoordinatorError(f"{label} is not a valid coordinator ID", remedy=f"use a valid coordinator-generated ID for {label}")
    return value
