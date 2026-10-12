"""Structured risks and blockers: a machine status and separate human-readable details."""

from __future__ import annotations

from .constants import EvidenceStatus
from .utils import CoordinatorError, JsonObject


def validate_evidence(value: object, owner: str) -> None:
    remedy = (
        f'set {owner} to {{"status": "none", "details": ""}} when absent, or '
        '{"status": "present", "details": "<description>"} when present; '
        "status must be an exact English enum value; put explanations in output; "
        "correct the staging report and submit it again"
    )
    if not isinstance(value, dict) or set(value) != {"status", "details"}:
        raise CoordinatorError(
            f"{owner} must be a status/details object", remedy=remedy
        )
    status = value["status"]
    if not isinstance(status, str) or status not in EvidenceStatus:
        raise CoordinatorError(f"{owner}.status must be none or present", remedy=remedy)
    details = value["details"]
    if not isinstance(details, str) or (
        details != "" if status == EvidenceStatus.NONE else not details.strip()
    ):
        raise CoordinatorError(
            f"{owner}.details must be empty for none and non-empty for present",
            remedy=remedy,
        )


def evidence_is_none(value: object) -> bool:
    """Only a complete, valid empty record proves the absence of a blocker or risk."""
    return value == {"status": EvidenceStatus.NONE, "details": ""}


def evidence_text(value: JsonObject) -> str:
    """Render an already validated record without exposing its JSON structure to the reader."""
    return str(
        value["details"]
        if value["status"] == EvidenceStatus.PRESENT
        else EvidenceStatus.NONE
    )
