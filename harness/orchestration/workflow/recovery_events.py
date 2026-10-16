"""Recovery events: the sealed, audited records that pause, resume or rewind a batch.

An event is appended to `recovery_events` and never edited; `validate_events` proves each one
against the immutable dispatches and the audit trail.
"""

from __future__ import annotations

import re
from pathlib import Path

from harness.orchestration.core.constants import TERMINAL_BATCH_STATES
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _non_empty,
    _read_object,
)
from harness.orchestration.ledger.ledger_ops import _load_dispatch, _records_root
from harness.orchestration.ledger.recovery_chain import event_audit
from harness.orchestration.workflow import approval

EVENT_FIELDS = frozenset(
    {
        "sequence",
        "kind",
        "before_state",
        "state",
        "next_action",
        "dispatch_id",
        "snapshot_commit",
        "transition_digest",
        "approved_by",
        "approved_at",
        "note",
        "evidence",
        "record_sha256",
    }
)


def append_event(
    root: Path,
    batch: JsonObject,
    *,
    kind: str,
    before: str,
    approver: JsonObject,
    note: str,
    evidence: JsonObject,
    dispatch_id: str | None = None,
) -> JsonObject:
    dispatch = _load_dispatch(root, dispatch_id) if dispatch_id else {}
    event = approval.sealed(
        {
            "sequence": len(batch.get("recovery_events", [])) + 1,
            "kind": kind,
            "before_state": before,
            "state": batch["state"],
            "next_action": batch.get("next_action"),
            "dispatch_id": dispatch_id,
            "snapshot_commit": dispatch.get("snapshot_commit"),
            "transition_digest": dispatch.get("transition_digest"),
            "approved_by": approver["approved_by"],
            "approved_at": approver["approved_at"],
            "note": note,
            "evidence": evidence,
        }
    )
    batch.setdefault("recovery_events", []).append(event)
    return event


def validate_events(root: Path, batch: JsonObject) -> None:
    events = batch.get("recovery_events", [])
    if not isinstance(events, list):
        raise CoordinatorError(
            "invalid recovery events",
            remedy="inspect ledger integrity; do not edit recovery records",
        )
    records = _records_root(root)
    audits = (
        [_read_object(p, "audit") for p in (records / "audit").glob("*.json")]
        if events
        else []
    )
    runtime = batch.get("harness_runtime_sha256")
    for index, event in enumerate(events, 1):
        if (
            not isinstance(event, dict)
            or set(event) != EVENT_FIELDS
            or event
            != approval.sealed({k: v for k, v in event.items() if k != "record_sha256"})
        ):
            raise CoordinatorError(
                "recovery event failed integrity validation",
                remedy="inspect the preserved ledger generation; do not rewrite checksums",
            )
        if (
            event["sequence"] != index
            or event["kind"] not in {"pause", "resume-stop", "rewind"}
            or not isinstance(event["evidence"], dict)
        ):
            raise CoordinatorError(
                "invalid recovery event schema",
                remedy="inspect recovery sequence and evidence",
            )
        policy = event["kind"] == "pause"
        upgrade = event["evidence"].get("runtime_upgrade")
        if upgrade is not None:
            if (
                policy
                or not isinstance(upgrade, dict)
                or set(upgrade) != {"from", "to", "original"}
                or upgrade["from"] != runtime
                or upgrade["original"] != batch.get("harness_runtime_sha256")
                or not isinstance(upgrade["to"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", upgrade["to"])
            ):
                raise CoordinatorError(
                    "invalid control runtime upgrade",
                    remedy="use a human recovery command; preserve the original runtime pin and audit",
                )
            runtime = upgrade["to"]
        if (
            not _non_empty(event["approved_by"])
            or not _non_empty(event["approved_at"])
            or policy != (event["approved_by"] == approval.AUTO_APPROVER)
            or (not policy and event["approved_by"].startswith("policy:"))
        ):
            raise CoordinatorError(
                "invalid recovery approver",
                remedy="inspect the original operator decision and audit",
            )
        if event["dispatch_id"] is not None:
            brief = _load_dispatch(root, event["dispatch_id"])
            if brief.get("batch_id") != batch["batch_id"] or any(
                event[k] != brief.get(k)
                for k in ("snapshot_commit", "transition_digest")
            ):
                raise CoordinatorError(
                    "recovery event diverged from source dispatch",
                    remedy="inspect the immutable dispatch evidence",
                )
        if not any(
            a.get("details", {}).get("path") == f"batches/{batch['batch_id']}.json"
            and a["details"].get("decision") == event_audit(event)
            for a in audits
        ):
            raise CoordinatorError(
                "recovery event has no matching audit transition",
                remedy="inspect the recovery decision audit; no approval bypass is allowed",
            )
        for field, identifiers in (
            (
                "superseded_dispatches",
                {e["dispatch_id"] for e in batch.get("dispatches", [])},
            ),
            (
                "superseded_risks",
                {e["risk_assessment_id"] for e in batch.get("risk_assessments", [])},
            ),
            (
                "superseded_candidate_registrations",
                {e["source_dispatch_id"] for e in batch.get("candidate_registrations", [])},
            ),
        ):
            values = event["evidence"].get(field, [])
            if not isinstance(values, list) or any(
                not isinstance(value, str) or value not in identifiers
                for value in values
            ):
                raise CoordinatorError(
                    "recovery supersedes unknown evidence",
                    remedy="inspect the original batch-scoped evidence; do not repair event checksums",
                )
    recovered = any(e["kind"] in {"resume-stop", "rewind"} for e in events)
    if bool(batch.get("manual_recovery")) != recovered:
        raise CoordinatorError(
            "manual recovery flag diverged from audited events",
            remedy="retain manual_all after a human recovery; inspect the ledger audit",
        )
    restart = batch.get("environmental_restart")
    if restart is not None:
        source = events[-1] if events else {}
        if (
            not isinstance(restart, dict)
            or source.get("kind") != "resume-stop"
            or restart.get("dispatch_id") != source.get("dispatch_id")
            or restart.get("snapshot_commit") != source.get("snapshot_commit")
        ):
            raise CoordinatorError(
                "environment restart diverged from recovery event",
                remedy="inspect the immutable source and audited recovery; do not substitute a candidate",
            )


def pause(root: Path, batch: JsonObject, stop: JsonObject, moment: str) -> JsonObject:
    if batch["state"] in TERMINAL_BATCH_STATES:
        raise CoordinatorError(
            "completed or abandoned batch cannot pause",
            remedy="inspect its historical report; create a new batch for new work",
        )
    before = batch["state"]
    batch["state"] = "paused"
    return append_event(
        root,
        batch,
        kind="pause",
        before=before,
        approver={"approved_by": approval.AUTO_APPROVER, "approved_at": moment},
        note=f"{stop['category']}: {stop['reason']}",
        evidence={"stop": stop},
        dispatch_id=stop["evidence"].get("dispatch_id"),
    )
