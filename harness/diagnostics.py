"""Structured, bounded diagnostics shared by configuration boundaries and UIs."""

from __future__ import annotations

import json
from dataclasses import dataclass

from harness.configuration import reject_secrets
from harness.json_types import JsonObject

DIAGNOSTIC_CODES = (
    "config-normalized",
    "config-noncanonical",
    "config-contract",
    "config-schema",
    "config-invalid",
    "config-options",
    "config-read",
    "config-write",
    "config-ambiguous",
    "config-reference",
    "config-cycle",
)
DIAGNOSTIC_SEVERITIES = ("error", "warning")


def safe_value(value: object) -> object:
    """Never include secret-like or unbounded authored values in diagnostics."""
    try:
        reject_secrets(value)
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        return "<redacted>"
    return value if len(encoded) <= 256 else "<value exceeds diagnostic limit>"


def pointer(parts: tuple[str | int, ...]) -> str:
    return "".join(
        "/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts
    )


@dataclass(frozen=True)
class Diagnostic:
    code: str
    file: str
    parts: tuple[str | int, ...]
    message: str
    expected: object = None
    received: object = None
    suggestion: object = None
    severity: str = "error"
    recoverable: bool = False
    rule_id: str | None = None
    rule_version: str | None = None

    def __post_init__(self) -> None:
        if (
            self.code not in DIAGNOSTIC_CODES
            or self.severity not in DIAGNOSTIC_SEVERITIES
        ):
            raise ValueError("unregistered configuration diagnostic code or severity")

    def to_dict(self) -> JsonObject:
        return {
            "code": self.code,
            "file": self.file,
            "pointer": pointer(self.parts),
            "jsonpath": "$"
            + "".join("[" + json.dumps(part) + "]" for part in self.parts),
            "message": safe_value(self.message),
            "expected": safe_value(self.expected),
            "received_redacted": safe_value(self.received),
            "suggestion": safe_value(self.suggestion),
            "severity": self.severity,
            "recoverable": self.recoverable,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
        }
