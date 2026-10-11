"""Pure views of a batch's recovery history: the active evidence chain and the audit of an event.

Recovery never rewrites records: a rewind or runtime upgrade is an appended event, and these
functions read the chain it leaves. They depend only on the batch record, so the ledger layer can
validate transitions without importing the workflow layer.
"""

from __future__ import annotations

from harness.orchestration.core.constants import TERMINAL_BATCH_STATES
from harness.orchestration.core.utils import JsonObject, _non_empty


def legacy_abandoned(batch: JsonObject) -> bool:
    """Only the former standalone abandon command made failed a terminal operator refusal."""
    refusal = batch.get("abandoned")
    return bool(
        batch.get("state") == "failed"
        and isinstance(refusal, dict)
        and all(
            _non_empty(refusal.get(k))
            for k in ("approved_by", "approved_at", "reason", "abandoned_at")
        )
        and not refusal["approved_by"].startswith("policy:")
        and any(
            d.get("decision") == "abandon"
            and all(d.get(k) == refusal[k] for k in ("approved_by", "approved_at"))
            for d in batch.get("coordinator_decisions", [])
        )
    )


def terminal(batch: JsonObject) -> bool:
    return batch.get("state") in TERMINAL_BATCH_STATES or legacy_abandoned(batch)


def active_dispatches(batch: JsonObject) -> list[JsonObject]:
    """The active chain; absent events preserve the legacy interpretation."""
    superseded = {
        item
        for event in batch.get("recovery_events", [])
        for item in event["evidence"].get("superseded_dispatches", [])
    }
    return [
        item
        for item in batch.get("dispatches", [])
        if item.get("dispatch_id") not in superseded
    ]


def active_risks(batch: JsonObject) -> list[JsonObject]:
    superseded = {
        item
        for event in batch.get("recovery_events", [])
        for item in event["evidence"].get("superseded_risks", [])
    }
    return [
        item
        for item in batch.get("risk_assessments", [])
        if item.get("risk_assessment_id") not in superseded
    ]


def effective_runtime(batch: JsonObject) -> object:
    """The audited control-plane epoch; the immutable batch plan retains its original pin."""
    value = batch.get("harness_runtime_sha256")
    for event in batch.get("recovery_events", []):
        upgrade = event["evidence"].get("runtime_upgrade")
        if upgrade:
            value = upgrade["to"]
    return value


def writer_start(batch: JsonObject) -> str | None:
    for event in reversed(batch.get("recovery_events", [])):
        if event["kind"] == "rewind":
            value = event["evidence"].get("writer_start_commit")
            return value if isinstance(value, str) else None
    return None


def carried_architect_active(batch: JsonObject) -> bool:
    return not any(
        e["kind"] == "rewind" and e["next_action"] == "architect"
        for e in batch.get("recovery_events", [])
    )


def event_audit(event: JsonObject) -> JsonObject:
    return {
        "decision": event["kind"],
        "dispatch_id": event["dispatch_id"],
        "approver": {
            "kind": "policy" if event["kind"] == "pause" else "human",
            "name": event["approved_by"],
        },
        "approved_at": event["approved_at"],
        "evidence": {"recovery_event": event},
    }
