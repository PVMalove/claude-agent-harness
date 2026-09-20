"""Leaf helpers of the orchestration coordinator: the JSON boundary type, the coordinator's own
error, and the small pure functions every layer above needs.

Nothing here reads project configuration, touches git, or opens the ledger, so `core.config`,
`ledger` and `workflow` may all import it without a cycle.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeGuard

from harness.errors import HarnessError
from harness.gate_runner.gate_runner import concise_evidence, sanitise


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


def _repo(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "repo", ".")).resolve()


def _moment(value: object, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise CoordinatorError(f"{label} is not a readable timestamp", remedy=f"pass {label} as an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise CoordinatorError(f"{label} must include a timezone", remedy=f"include an explicit UTC offset (e.g. Z or +00:00) in {label}")
    return parsed


def _lease_expired(lease: JsonObject) -> bool:
    return _moment(lease["expires_at"], "QA lease expiry") <= datetime.now(timezone.utc)


def _silent_seconds(status: JsonObject) -> int:
    """Seconds since a dispatch last proved it was alive.  This generalizes the QA lane's
    lease-expiry check to every dispatch, whatever transport is carrying it."""
    last = status.get("heartbeat_at") or status.get("updated_at")
    elapsed = datetime.now(timezone.utc) - _moment(last, "dispatch heartbeat")
    return max(0, int(elapsed.total_seconds()))


def _sanitise(text: str) -> str:
    return sanitise(text)


def _concise_evidence(text: str) -> str:
    return concise_evidence(text)


def _short(value: object) -> str:
    return value[:12] if isinstance(value, str) and value else "none"
