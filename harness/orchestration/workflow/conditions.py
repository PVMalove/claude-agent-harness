"""Conditional acceptance: a human attests that an infrastructure failure, not the work, stopped a stage.

`batch decide --decision accept-conditional` lets the pipeline move past a stage whose report is
`blocked` by the environment. The stage stays an open condition on the batch until a later,
unconditional gate independently covers it. Publish and completion refuse while one is open.
"""

from __future__ import annotations

import argparse

from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _non_empty,
)
from harness.orchestration.ledger.recovery_chain import active_dispatches

# The unconditional accepts that independently re-check a conditionally accepted stage.
COVERING_STAGES: dict[str, frozenset[str]] = {
    "architect": frozenset({"code-review", "qa"}),
    "developer": frozenset({"code-review", "qa"}),
    "verification": frozenset({"code-review", "qa"}),
    "code-review": frozenset({"qa"}),
    "qa": frozenset({"qa"}),
    # Publish is an external effect: its evidence is the remote tip, checked at attestation.
    "publish": frozenset(),
}
DEFAULT_COVERING = frozenset({"code-review", "qa"})


def attest(args: argparse.Namespace, report: JsonObject) -> str:
    """Validate the human attestation of an infrastructure failure; returns the approver."""
    approver = getattr(args, "approved_by", None)
    if not _non_empty(approver) or str(approver).strip().startswith("policy:"):
        raise CoordinatorError(
            "a conditional acceptance requires an explicit human approval",
            remedy="pass --approved-by naming the operator and --approved-at for the current decision",
        )
    if not getattr(args, "infrastructure_failure", False):
        raise CoordinatorError(
            "accept-conditional requires the operator to attest an infrastructure failure",
            remedy="pass --infrastructure-failure with --note naming the infrastructure cause, or decide the report normally",
        )
    note = getattr(args, "note", None)
    if not _non_empty(note) or str(note).strip().lower() == "none":
        raise CoordinatorError(
            "accept-conditional requires a recorded note",
            remedy="pass --note naming the infrastructure cause the operator verified",
        )
    if report.get("outcome") != "blocked":
        raise CoordinatorError(
            "accept-conditional applies only to a blocked report",
            remedy="accept a completed report with --decision accept",
        )
    if any(c.get("result") == "fail" for c in report.get("checks_run", [])):
        raise CoordinatorError(
            "a report with a failed check is a work defect, not an infrastructure failure",
            remedy="retry the role to fix the failed check",
        )
    return str(approver).strip()


def record(
    batch: JsonObject,
    *,
    dispatch_id: str,
    stage: str,
    candidate: str | None,
    approver: str,
    moment: str,
    note: str,
    closed_by: str | None = None,
) -> JsonObject:
    """Append the open condition (or one already closed by independent evidence) to the batch."""
    condition: JsonObject = {
        "dispatch_id": dispatch_id,
        "stage": stage,
        "kind": "infrastructure-failure",
        "candidate_commit": candidate,
        "attested_by": approver,
        "attested_at": moment,
        "note": note,
        "status": "closed" if closed_by else "open",
        "closed_by": closed_by,
    }
    batch.setdefault("conditions", []).append(condition)
    return condition


def open_conditions(batch: JsonObject) -> list[JsonObject]:
    """Open conditions of dispatches still on the active chain; a rewind retires the rest."""
    active = {entry["dispatch_id"] for entry in active_dispatches(batch)}
    return [
        item
        for item in batch.get("conditions", [])
        if item.get("status") == "open" and item.get("dispatch_id") in active
    ]


def close_covered(batch: JsonObject, stage: str, dispatch_id: str, moment: str) -> None:
    """An unconditional accept of `stage` closes every open condition it independently covers."""
    for item in open_conditions(batch):
        covering = COVERING_STAGES.get(item["stage"], DEFAULT_COVERING)
        if stage in covering and item["dispatch_id"] != dispatch_id:
            item["status"] = "closed"
            item["closed_by"] = dispatch_id
            item["closed_at"] = moment


def require_closed(batch: JsonObject) -> None:
    """Publish and completion refuse while a stage rests on an unverified attestation."""
    pending = open_conditions(batch)
    if pending:
        stages = ", ".join(
            f"{item['stage']} ({item['dispatch_id']})" for item in pending
        )
        raise CoordinatorError(
            f"conditionally accepted stages are not independently verified: {stages}",
            remedy="run and accept the later gate that covers each stage (code-review or qa), or rewind and rerun the stage",
        )
