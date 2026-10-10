"""Operator recovery and the active evidence boundary. Historical records are never rewritten."""

from __future__ import annotations

import argparse
from pathlib import Path

from harness.orchestration.core import utils
from harness.orchestration.core.constants import TERMINAL_BATCH_STATES
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _non_empty,
    _read_object,
)
from harness.orchestration.ledger.ledger_ops import _load_dispatch, _records_root
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
ENVIRONMENT_REASONS = frozenset(
    {"worktree-mismatch", "model-mismatch", "runtime-unavailable"}
)
REWIND_STAGES = (
    "architect",
    "developer",
    "code-review",
    "qa",
    "publish",
    "verification",
    "resolve-conflict",
)


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


def human_approval(args: argparse.Namespace) -> JsonObject:
    if not _non_empty(
        getattr(args, "approved_by", None)
    ) or args.approved_by.strip().startswith("policy:"):
        raise CoordinatorError(
            "recovery requires an explicit human approval",
            remedy="pass --approved-by naming the operator and --approved-at for the current decision",
        )
    return approval._approval(args)


def verified_checkout(repo: Path, dispatch: JsonObject, path: str) -> JsonObject:
    from harness.orchestration.runtime_attestation import AttestationError, attest

    try:
        return attest(repo, dispatch, path)
    except AttestationError as exc:
        raise CoordinatorError(exc.message, remedy=exc.remedy) from exc


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


def upgrade_runtime(
    repo: Path, root: Path, batch: JsonObject, evidence: JsonObject
) -> None:
    from harness.orchestration.core.workspace import (
        _harness_runtime_sha256,
        _runtime_snapshot_root,
        _store_runtime_snapshot,
    )

    current = _harness_runtime_sha256(repo)
    previous = effective_runtime(batch)
    if previous != current:
        _store_runtime_snapshot(root, _runtime_snapshot_root(repo).parent, current)
        evidence["runtime_upgrade"] = {
            "from": previous,
            "to": current,
            "original": batch.get("harness_runtime_sha256"),
        }


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


def require_restart_contract(
    root: Path, batch: JsonObject, renewed: JsonObject
) -> None:
    original = _load_dispatch(root, batch["environmental_restart"]["dispatch_id"])
    # Context and approvals are renewed; the work contract and original handoff stay fixed.
    keys = (
        "role",
        "purpose",
        "access",
        "worktree",
        "branch",
        "snapshot_commit",
        "candidate_commit",
        "definition_of_done",
        "dependencies",
        "write_paths",
        "prohibited_changes",
        "verification_commands",
        "retry_handoff",
        "retry_package",
        "resolver",
        "carried_items",
        "rebase_target_commit",
    )
    changed = [k for k in keys if original.get(k) != renewed.get(k)]
    if changed:
        raise CoordinatorError(
            "environment restart changed the approved contract: " + ", ".join(changed),
            remedy="use an ordinary scoped transition or human rewind; environment restart preserves the original role, scope, candidate and findings",
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
            import re

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
                {e["dispatch_id"] for e in batch.get("candidate_registrations", [])},
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


def _note(args: argparse.Namespace) -> str:
    value = getattr(args, "note", None)
    if not _non_empty(value) or value.strip().lower() == "none":
        raise CoordinatorError(
            "recovery requires a recorded note",
            remedy="pass --note describing the resolved cause or the intended gate",
        )
    from harness.orchestration.core.config import _reject_sensitive

    _reject_sensitive({"note": value}, "recovery note")
    return value.strip()


def _recoverable(batch: JsonObject) -> None:
    if terminal(batch):
        raise CoordinatorError(
            "terminal operator refusal or completion cannot resume",
            remedy="create a new batch, using --supersedes only for an explicit abandon with accepted history",
        )


def _no_live_worker(root: Path, batch: JsonObject) -> None:
    from harness.orchestration.ledger.ledger_ops import _load_dispatch_status
    from harness.orchestration.core.constants import LIVE_DISPATCH_STATES

    for entry in active_dispatches(batch):
        status = _load_dispatch_status(root, entry["dispatch_id"])
        if status.get("state") in LIVE_DISPATCH_STATES:
            raise CoordinatorError(
                "a worker may still be running",
                remedy=f"stop the runtime worker, then dispatch cancel --dispatch {entry['dispatch_id']} --runtime-stopped --approved-by <operator> --approved-at <now> --reason <why>, and repeat recovery; timeout alone is not proof of termination",
            )


def resume_stopped_batch(args: argparse.Namespace) -> JsonObject:
    """Retire a proven environment failure and prepare a new, human-approved same-stage dispatch."""
    from harness.orchestration.core.config import _config, _attention_policy
    from harness.orchestration.core.utils import _repo
    from harness.orchestration.core.git_utils import _git
    from harness.orchestration.ledger.ledger_ops import (
        _state_root,
        _ledger_lock,
        _load_batch,
        _load_dispatch_status,
        _replace_record,
    )
    from harness.orchestration.ledger.lifecycle import LifecycleLedger, BatchRecord
    from harness.orchestration.workflow.history import (
        _validate_batch_integrity,
        _pending_report,
    )
    from harness.orchestration.workflow.decisions import (
        _abandon_open_dispatches,
        _discard_batch_leftovers,
    )

    approver, note = human_approval(args), _note(args)
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        ledger.validate()
        batch = _load_batch(root, args.batch)
        _validate_batch_integrity(root, batch)
        _recoverable(batch)
        events = batch.get("recovery_events", [])
        if (
            events
            and events[-1]["kind"] == "resume-stop"
            and batch["state"] == events[-1]["state"]
            and batch.get("next_action") == events[-1]["next_action"]
            and len(batch.get("dispatches", []))
            == events[-1]["evidence"].get("dispatch_count")
        ):
            return {
                "batch_id": batch["batch_id"],
                "state": batch["state"],
                "next_action": batch.get("next_action"),
                "event": events[-1],
            }
        if batch["state"] not in {"paused", "blocked", "failed"}:
            raise CoordinatorError(
                "batch has no recorded stop to resume",
                remedy="inspect batch auto-report or use batch rewind to choose an earlier gate",
            )
        _no_live_worker(root, batch)
        if batch.get("needs_attention"):
            raise CoordinatorError(
                "unresolved attention prevents recovery",
                remedy="inspect and resolve the named attention with batch attention resolve; resume never clears unrelated attention",
            )
        entries = active_dispatches(batch)
        entry = entries[-1] if entries else None
        # auto_stop is historical after recovery. A later manual startup failure belongs
        # to its own dispatch, even when the first automatic stop named a different one.
        stop = batch.get("auto_stop") if not batch.get("manual_recovery") else None
        if events and events[-1]["kind"] == "pause":
            stop = events[-1]["evidence"].get("stop")
        if entry is not None and (
            not stop or stop.get("reason") in ENVIRONMENT_REASONS
        ):
            status = _load_dispatch_status(root, entry["dispatch_id"])
            model = status.get("model_self_report", {})
            tree = status.get("worktree_attestation", {})
            failure = status.get("runtime_failure", {})
            unavailable = (
                failure.get("kind") == "runtime-unavailable"
                and failure.get("operation") == "startup"
                and failure.get("worker_started") is False
            )
            if not (
                model.get("match") is False or tree.get("match") is False or unavailable
            ):
                raise CoordinatorError(
                    "environment failure is not structurally confirmed",
                    remedy="collect runtime status evidence; use an ordinary decision or human rewind for an unknown failure",
                )
            if (
                stop
                and stop.get("evidence", {}).get("dispatch_id") != entry["dispatch_id"]
            ):
                raise CoordinatorError(
                    "stop and active dispatch disagree",
                    remedy="inspect the source dispatch before choosing recovery",
                )
            brief = _load_dispatch(root, entry["dispatch_id"])
            if entry.get("report"):
                report = _pending_report(root, batch, entry)
                review = report.get("review") or {}
                if (
                    report.get("outcome") != "blocked"
                    or report.get("changed_files")
                    or any(
                        c.get("result") == "fail" for c in report.get("checks_run", [])
                    )
                    or any(
                        review.get(axis, {}).get("findings")
                        for axis in ("standards", "spec")
                    )
                ):
                    raise CoordinatorError(
                        "report requires code correction rather than environment restart",
                        remedy="use the ordinary report decision or rewind; preserved findings and failed checks cannot be bypassed",
                    )
            checkout = Path(batch["worktree"]).resolve()
            proof = verified_checkout(repo, brief, str(checkout))
            if _git(checkout, "status", "--porcelain", "--untracked-files=all"):
                raise CoordinatorError(
                    "unknown dirty worktree prevents restart",
                    remedy="preserve and register known progress or create a scoped dispatch; never reset or stash to pass recovery",
                )
            attempts = sum(
                e["kind"] == "resume-stop"
                and e["snapshot_commit"] == brief["snapshot_commit"]
                for e in events
            )
            if (
                attempts
                >= _attention_policy(_config(repo))["max_infrastructure_retries"]
            ):
                raise CoordinatorError(
                    "environment recovery budget exhausted",
                    remedy="inspect operational attempts and explicitly adjust the bounded attention policy; recovery does not reset counters",
                )
            before = batch["state"]
            retired = _abandon_open_dispatches(ledger, root, batch, utils._now())
            action = batch.get("next_action") or (
                "publish"
                if brief.get("purpose") == "publish"
                else "resolve-conflict"
                if brief["role"] == "conflict-resolver"
                else brief["role"]
            )
            batch["next_action"] = action
            batch["state"] = "awaiting-approval"
            batch["environmental_restart"] = {
                "dispatch_id": brief["dispatch_id"],
                "snapshot_commit": brief["snapshot_commit"],
            }
            evidence = {
                "restart": proof,
                "retired_dispatches": retired,
                "stop": stop,
                "failure": {
                    "model_self_report": model,
                    "worktree_attestation": tree,
                    "runtime_failure": failure,
                },
            }
            source_id = brief["dispatch_id"]
        else:
            # Budget/no-route pauses keep the pending report for a manual decision.
            if not any(
                e.get("state") == "reported" and not e.get("decision") for e in entries
            ):
                raise CoordinatorError(
                    "no pending report or confirmed environment failure",
                    remedy="use batch rewind to select an earlier gate with a fresh human approval",
                )
            before = batch["state"]
            batch["state"] = "awaiting-approval"
            evidence, source_id = {"stop": stop}, None
            retired = []
        batch["manual_recovery"] = True
        evidence["dispatch_count"] = len(batch.get("dispatches", []))
        upgrade_runtime(repo, root, batch, evidence)
        event = append_event(
            root,
            batch,
            kind="resume-stop",
            before=before,
            approver=approver,
            note=note,
            evidence=evidence,
            dispatch_id=source_id,
        )
        batch.setdefault("coordinator_decisions", []).append(
            {
                "decision": "resume-stop",
                **approver,
                "note": note,
                "dispatch_id": source_id,
                "routing": {
                    "route": "environmental-restart" if source_id else "rewind",
                    "candidate_commit": event["snapshot_commit"],
                    "recovery_event_sha256": event["record_sha256"],
                },
            }
        )
        _replace_record(
            ledger, BatchRecord.from_dict(batch), decision=event_audit(event)
        )
        _discard_batch_leftovers(repo, ledger, batch, retired)
        return {
            "batch_id": batch["batch_id"],
            "state": batch["state"],
            "next_action": batch.get("next_action"),
            "event": event,
        }


def rewind_batch(args: argparse.Namespace) -> JsonObject:
    """Select a previous gate, superseding dependent evidence without changing Git history."""
    from harness.orchestration.core.utils import _repo
    from harness.orchestration.core.git_utils import _git, _candidate_commit
    from harness.orchestration.ledger.ledger_ops import (
        _state_root,
        _ledger_lock,
        _load_batch,
        _replace_record,
    )
    from harness.orchestration.ledger.lifecycle import LifecycleLedger, BatchRecord
    from harness.orchestration.workflow.history import (
        _validate_batch_integrity,
        _current_developer_candidate,
        _latest_registered_verification_candidate,
        _latest_checkpoint_for_dispatch,
        _pending_report,
    )
    from harness.orchestration.workflow.decisions import (
        _abandon_open_dispatches,
        _discard_batch_leftovers,
    )

    approver, note = human_approval(args), _note(args)
    target = getattr(args, "to", None)
    if target not in REWIND_STAGES:
        raise CoordinatorError(
            "unknown rewind gate", remedy=f"choose --to from {', '.join(REWIND_STAGES)}"
        )
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        ledger.validate()
        batch = _load_batch(root, args.batch)
        _validate_batch_integrity(root, batch)
        _recoverable(batch)
        _no_live_worker(root, batch)
        entries = active_dispatches(batch)
        ranks = {
            "architect": 0,
            "developer": 1,
            "developer-retry": 1,
            "resolve-conflict": 1,
            "conflict-resolver": 1,
            "risk-assessment": 2,
            "verification": 3,
            "code-review": 4,
            "qa": 5,
            "publish": 6,
        }

        def rank(entry: JsonObject) -> int:
            brief = _load_dispatch(root, entry["dispatch_id"])
            return ranks[
                "publish" if brief.get("purpose") == "publish" else brief["role"]
            ]

        current_rank = max(
            [ranks.get(str(batch.get("next_action")), 0), *[rank(e) for e in entries]],
            default=0,
        )
        if ranks[target] > current_rank:
            raise CoordinatorError(
                "rewind cannot skip forward to an unreached gate",
                remedy="choose architect or a gate this batch has reached, then pass the normal approvals and prerequisites",
            )
        if target == "verification":
            _latest_registered_verification_candidate(repo, batch)
        if target == "resolve-conflict" and not isinstance(batch.get("resolver"), dict):
            raise CoordinatorError(
                "resolver boundary is not registered",
                remedy="use integration resolve to register the conflicting candidate/target pair before dispatching a resolver",
            )
        candidate = _current_developer_candidate(repo, root, batch)
        # A validated report or committed checkpoint is known progress, never accepted evidence.
        for entry in reversed(entries):
            if (
                entry.get("role") in {"developer", "conflict-resolver"}
                and entry.get("purpose", "work") == "work"
            ):
                if entry.get("report"):
                    report = _pending_report(root, batch, entry)
                    candidate = _candidate_commit(repo, report["commit_sha"])
                elif any(
                    c["dispatch_id"] == entry["dispatch_id"]
                    for c in batch.get("checkpoints", [])
                ):
                    checkpoint = _latest_checkpoint_for_dispatch(
                        root, batch, entry["dispatch_id"]
                    )
                    candidate = _candidate_commit(repo, checkpoint["commit_sha"])
                break
        checkout = Path(batch["worktree"]).resolve()
        known = (
            candidate
            or writer_start(batch)
            or batch.get("branch_start_commit", batch["base_commit"])
        )
        verified_checkout(
            repo,
            {"role": "developer", "branch": batch["branch"], "snapshot_commit": known},
            str(checkout),
        )
        if _git(checkout, "status", "--porcelain", "--untracked-files=all"):
            raise CoordinatorError(
                "rewind found unknown dirty files",
                remedy="preserve and register progress before selecting a fresh gate; rewind never resets files",
            )
        excluded = [e["dispatch_id"] for e in entries if rank(e) >= ranks[target]]
        before = batch["state"]
        retired = _abandon_open_dispatches(ledger, root, batch, utils._now())
        evidence: JsonObject = {
            "superseded_dispatches": excluded,
            "superseded_risks": [r["risk_assessment_id"] for r in active_risks(batch)]
            if ranks[target] <= 3
            else [],
            "writer_start_commit": candidate,
            "retired_dispatches": retired,
            "target": target,
        }
        evidence["superseded_candidate_registrations"] = (
            [r["dispatch_id"] for r in batch.get("candidate_registrations", [])]
            if ranks[target] <= 1
            else []
        )
        if target == "architect" and "commit_plan" in batch:
            evidence["superseded_commit_plan"] = batch.pop("commit_plan")
        batch["state"] = (
            "awaiting-approval" if batch.get("coordinator_approval") else "planned"
        )
        batch["next_action"] = target
        batch.pop("required_next_role", None)
        batch.pop("environmental_restart", None)
        batch["manual_recovery"] = True
        if ranks[target] <= 1 and candidate:
            batch["rewind_writer_pending"] = True
        batch["risk_reassessment_required"] = bool(evidence["superseded_risks"])
        upgrade_runtime(repo, root, batch, evidence)
        event = append_event(
            root,
            batch,
            kind="rewind",
            before=before,
            approver=approver,
            note=note,
            evidence=evidence,
        )
        batch.setdefault("coordinator_decisions", []).append(
            {
                "decision": "rewind",
                **approver,
                "note": note,
                "next_role": target,
                "routing": {
                    "route": "rewind",
                    "candidate_commit": candidate,
                    "recovery_event_sha256": event["record_sha256"],
                },
            }
        )
        _replace_record(
            ledger, BatchRecord.from_dict(batch), decision=event_audit(event)
        )
        _discard_batch_leftovers(repo, ledger, batch, retired)
        return {
            "batch_id": batch["batch_id"],
            "state": batch["state"],
            "next_action": target,
            "event": event,
        }
