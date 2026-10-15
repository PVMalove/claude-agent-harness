"""Operator recovery commands: resume a stopped batch or rewind it. Historical records are never rewritten."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import cast

from harness.orchestration.core import utils
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _non_empty,
)
from harness.orchestration.ledger.ledger_ops import _load_dispatch, _replace_record
from harness.orchestration.ledger.lifecycle import BatchRecord, LifecycleLedger
from harness.orchestration.ledger.recovery_chain import (
    active_dispatches,
    active_risks,
    effective_runtime,
    event_audit,
    terminal,
    writer_start,
)
from harness.orchestration.workflow import approval
from harness.orchestration.workflow.recovery_events import append_event

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


# Position of each stage or next_action in the pipeline; rewind may only go to an equal or lower rank.
STAGE_RANKS = {
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
# A rewind at or before verification retires the risk assessments of the active chain.
RISK_INVALIDATING_RANK = STAGE_RANKS["verification"]
# A rewind at or before the writer stage retires the registered candidates.
CANDIDATE_INVALIDATING_RANK = STAGE_RANKS["developer"]


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
        "resolved_runtime",
        "resolved_model",
        "resolved_effort",
        "resolved_provider_profile",
        "resolved_transport",
        "runtime_access",
        "orchestration_policy",
        "liveness",
        "commit_plan",
        "allowed_tools",
        "context_budget",
        "retry_reason",
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


def _no_live_worker(root: Path, batch: JsonObject, *, repo: Path | None = None) -> None:
    from harness.orchestration.core.constants import LIVE_DISPATCH_STATES
    from harness.orchestration.ledger.ledger_ops import _load_dispatch_status

    for entry in active_dispatches(batch):
        status = _load_dispatch_status(root, entry["dispatch_id"])
        from harness.orchestration.workflow.startup import worker_process_state

        if (
            status.get("state") in LIVE_DISPATCH_STATES
            and worker_process_state(status, repo=repo) != "stopped"
        ):
            raise CoordinatorError(
                "a worker may still be running",
                remedy=f"stop the runtime worker, then dispatch cancel --dispatch {entry['dispatch_id']} --runtime-stopped --approved-by <operator> --approved-at <now> --reason <why>, and repeat recovery; timeout alone is not proof of termination",
            )


def _recovery_result(batch: JsonObject, event: JsonObject) -> JsonObject:
    return {
        "batch_id": batch["batch_id"],
        "state": batch["state"],
        "next_action": batch.get("next_action"),
        "event": event,
    }


def _record_recovery(
    ledger: LifecycleLedger,
    repo: Path,
    batch: JsonObject,
    event: JsonObject,
    retired: list[str],
    decision: JsonObject,
) -> None:
    """Persist the batch with its decision and audit in one record, then drop stale leftovers."""
    from harness.orchestration.workflow.decisions import _discard_batch_leftovers

    batch.setdefault("coordinator_decisions", []).append(decision)
    _replace_record(ledger, BatchRecord.from_dict(batch), decision=event_audit(event))
    _discard_batch_leftovers(repo, ledger, batch, retired)


def _repeated_resume(batch: JsonObject) -> JsonObject | None:
    """The last resume-stop event when the batch has not moved since, so a retry is idempotent."""
    events = batch.get("recovery_events", [])
    if (
        events
        and events[-1]["kind"] == "resume-stop"
        and batch["state"] == events[-1]["state"]
        and batch.get("next_action") == events[-1]["next_action"]
        and len(batch.get("dispatches", []))
        == events[-1]["evidence"].get("dispatch_count")
    ):
        return cast(JsonObject, events[-1])
    return None


def _stop_to_resume(batch: JsonObject) -> JsonObject | None:
    """The stop being recovered; an auto_stop is historical once a human recovered the batch."""
    events = batch.get("recovery_events", [])
    if events and events[-1]["kind"] == "pause":
        return cast(JsonObject, events[-1]["evidence"].get("stop"))
    return None if batch.get("manual_recovery") else batch.get("auto_stop")


def _confirmed_failure(
    root: Path,
    batch: JsonObject,
    entry: JsonObject,
    *,
    runtime_stopped: bool = False,
    repo: Path | None = None,
) -> JsonObject:
    """Structural startup evidence of an environment failure; failing that, a conservative text
    classification of the blocked report, recorded as a heuristic for the human to approve.

    Either way the worker's outcome must be known first: a live or unknown worker may still run,
    so a restart waits for it or for the operator's ``--runtime-stopped``."""
    from harness.orchestration.core.constants import RECOVERY_EVIDENCE_COMMANDS
    from harness.orchestration.ledger.ledger_ops import _load_dispatch_status
    from harness.orchestration.workflow import failure_classifier
    from harness.orchestration.workflow.history import _pending_report
    from harness.orchestration.workflow.startup import assess_startup

    status = _load_dispatch_status(root, entry["dispatch_id"])
    failure = status.get("runtime_failure", {})
    assessment = assess_startup(
        _load_dispatch(root, entry["dispatch_id"]),
        status,
        runtime_stopped=runtime_stopped,
        repo=repo,
    )
    if not assessment["restart_allowed"]:
        raise CoordinatorError(
            f"the worker outcome is {assessment['worker_state']}; a restart could leave two "
            "workers on one worktree",
            remedy="keep waiting, or stop the worker through its runtime and repeat batch "
            f"resume-stop with --runtime-stopped; inspect {RECOVERY_EVIDENCE_COMMANDS} first",
        )
    evidence: JsonObject = {
        "model_self_report": status.get("model_self_report", {}),
        "worktree_attestation": status.get("worktree_attestation", {}),
        "runtime_failure": failure,
        "startup_assessment": assessment,
    }
    if assessment["runtime_failure"] is not None:
        evidence["classification"] = assessment["runtime_failure"]
    # A missing self-report, or a worker that exited after one without a report, proves no
    # environment failure; only a mismatch or a classified runtime failure does.
    if assessment["classification"] not in {
        None,
        "model-self-report-missing",
        "worker-exited-without-report",
    }:
        return evidence
    report = _pending_report(root, batch, entry) if entry.get("report") else {}
    classification = (
        failure_classifier.classify(
            (report.get("blockers") or {}).get("details"), failure.get("error")
        )
        if report.get("outcome") in {None, "blocked"}
        else None
    )
    if classification is None:
        raise CoordinatorError(
            "environment failure is not confirmed by runtime status or by the blocked report",
            remedy="collect runtime status evidence or name the infrastructure cause in the report "
            f"blockers; inspect {RECOVERY_EVIDENCE_COMMANDS}, then choose an ordinary decision, "
            "a human rewind or a separate human abandon decision",
        )
    return {**evidence, "classification": classification}


def _require_codeless_report(root: Path, batch: JsonObject, entry: JsonObject) -> None:
    """A pending report may only be bypassed when it holds no code, failed checks or findings."""
    from harness.orchestration.workflow.history import _pending_report

    if not entry.get("report"):
        return
    report = _pending_report(root, batch, entry)
    review = report.get("review") or {}
    if (
        report.get("outcome") != "blocked"
        or report.get("changed_files")
        or any(c.get("result") == "fail" for c in report.get("checks_run", []))
        or any(review.get(axis, {}).get("findings") for axis in ("standards", "spec"))
    ):
        raise CoordinatorError(
            "report requires code correction rather than environment restart",
            remedy="use the ordinary report decision or rewind; preserved findings and failed checks cannot be bypassed",
        )


def _restart_failed_dispatch(
    repo: Path,
    root: Path,
    ledger: LifecycleLedger,
    batch: JsonObject,
    entry: JsonObject,
    stop: JsonObject | None,
    *,
    runtime_stopped: bool = False,
) -> tuple[JsonObject, list[str]]:
    """Retire the failed dispatch and re-open its stage; returns the event evidence and retirees."""
    from harness.orchestration.core.config import _attention_policy, _config
    from harness.orchestration.core.constants import RECOVERY_EVIDENCE_COMMANDS
    from harness.orchestration.core.git_utils import _git
    from harness.orchestration.workflow.decisions import _abandon_open_dispatches

    failure = _confirmed_failure(
        root, batch, entry, runtime_stopped=runtime_stopped, repo=repo
    )
    if stop and stop.get("evidence", {}).get("dispatch_id") != entry["dispatch_id"]:
        raise CoordinatorError(
            "stop and active dispatch disagree",
            remedy="inspect the source dispatch before choosing recovery",
        )
    brief = _load_dispatch(root, entry["dispatch_id"])
    _require_codeless_report(root, batch, entry)
    checkout = Path(batch["worktree"]).resolve()
    proof = verified_checkout(repo, brief, str(checkout))
    if _git(checkout, "status", "--porcelain", "--untracked-files=all"):
        raise CoordinatorError(
            "unknown dirty worktree prevents restart",
            remedy="preserve and register known progress or create a scoped dispatch; never reset or stash to pass recovery",
        )
    attempts = sum(
        e["kind"] == "resume-stop" and e["snapshot_commit"] == brief["snapshot_commit"]
        for e in batch.get("recovery_events", [])
    )
    if attempts >= _attention_policy(_config(repo))["max_infrastructure_retries"]:
        raise CoordinatorError(
            "environment recovery budget exhausted",
            remedy=f"the batch stays {batch['state']} for a human choice: wait, repair the launch, "
            "explicitly adjust the bounded attention policy, rewind, or a separate abandon "
            f"decision; inspect {RECOVERY_EVIDENCE_COMMANDS}; recovery does not reset counters",
        )
    retired = _abandon_open_dispatches(ledger, root, batch, utils._now())
    batch["next_action"] = batch.get("next_action") or (
        "publish"
        if brief.get("purpose") == "publish"
        else "resolve-conflict"
        if brief["role"] == "conflict-resolver"
        else brief["role"]
    )
    batch["environmental_restart"] = {
        "dispatch_id": brief["dispatch_id"],
        "snapshot_commit": brief["snapshot_commit"],
    }
    evidence = {"restart": proof, "retired_dispatches": retired, "stop": stop}
    return {**evidence, "failure": failure}, retired


def _record_resumed_batch(args: argparse.Namespace) -> JsonObject:
    """Retire a proven environment failure and prepare a new, human-approved same-stage dispatch."""
    from harness.orchestration.core.utils import _repo
    from harness.orchestration.ledger.ledger_ops import (
        _ledger_lock,
        _load_batch,
        _state_root,
    )
    from harness.orchestration.workflow.history import _validate_batch_integrity

    approver, note = human_approval(args), _note(args)
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        ledger.validate()
        batch = _load_batch(root, args.batch)
        _validate_batch_integrity(root, batch)
        _recoverable(batch)
        if (repeated := _repeated_resume(batch)) is not None:
            return _recovery_result(batch, repeated)
        if batch["state"] not in {"paused", "blocked", "failed"}:
            raise CoordinatorError(
                "batch has no recorded stop to resume",
                remedy="inspect batch auto-report or use batch rewind to choose an earlier gate",
            )
        _no_live_worker(root, batch, repo=repo)
        if batch.get("needs_attention"):
            raise CoordinatorError(
                "unresolved attention prevents recovery",
                remedy="inspect and resolve the named attention with batch attention resolve; resume never clears unrelated attention",
            )
        entries = active_dispatches(batch)
        entry = entries[-1] if entries else None
        stop = _stop_to_resume(batch)
        before = batch["state"]
        retired: list[str] = []
        source_id = None
        if entry is not None and (
            not stop or stop.get("reason") in ENVIRONMENT_REASONS
        ):
            source_id = entry["dispatch_id"]
            evidence, retired = _restart_failed_dispatch(
                repo,
                root,
                ledger,
                batch,
                entry,
                stop,
                runtime_stopped=bool(getattr(args, "runtime_stopped", False)),
            )
        elif any(
            e.get("state") == "reported" and not e.get("decision") for e in entries
        ):
            # Budget/no-route pauses keep the pending report for a manual decision.
            evidence = {"stop": stop}
        else:
            raise CoordinatorError(
                "no pending report or confirmed environment failure",
                remedy="use batch rewind to select an earlier gate with a fresh human approval",
            )
        batch["state"] = "awaiting-approval"
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
        _record_recovery(
            ledger,
            repo,
            batch,
            event,
            retired,
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
            },
        )
        return _recovery_result(batch, event)


def resume_stopped_batch(args: argparse.Namespace) -> JsonObject:
    """Optionally approve the same-contract successor in the one recorded human recovery."""
    if not getattr(args, "restart", False):
        return _record_resumed_batch(args)
    from harness.orchestration.core.utils import _repo
    from harness.orchestration.ledger.ledger_ops import (
        _ledger_lock,
        _load_batch,
        _state_root,
    )
    from harness.orchestration.workflow.history import _validate_batch_integrity
    from harness.orchestration.workflow.dispatch import create_dispatch

    repo = _repo(args)
    root = _state_root(args, repo)
    with _ledger_lock(LifecycleLedger(root)):
        batch = _load_batch(root, args.batch)
        _validate_batch_integrity(root, batch)
        stop = _stop_to_resume(batch)
        if stop and stop.get("reason") not in ENVIRONMENT_REASONS:
            raise CoordinatorError(
                "this recovery has no same-stage worker to restart",
                remedy="resume without --restart and decide the preserved pending report",
            )
        events = batch.get("recovery_events", [])
        if events and events[-1]["kind"] == "resume-stop":
            event = events[-1]
            following = batch["dispatches"][
                event["evidence"].get("dispatch_count", len(batch["dispatches"])) :
            ]
            if following:
                first = _load_dispatch(root, following[0]["dispatch_id"])
                if (
                    first.get("transition", {}).get("recovery_event_sha256")
                    == event["record_sha256"]
                ):
                    return {
                        **_recovery_result(batch, event),
                        "restart_dispatch_id": first["dispatch_id"],
                        "already_recorded": True,
                    }
    result = _record_resumed_batch(args)
    event = result["event"]
    if not event.get("dispatch_id"):
        raise CoordinatorError(
            "this recovery has no same-stage worker to restart",
            remedy="decide the pending report; use --restart only for a confirmed startup failure",
        )
    source = _load_dispatch(root, event["dispatch_id"])
    successor = create_dispatch(
        argparse.Namespace(
            repo=str(repo),
            state_dir=getattr(args, "state_dir", None),
            batch=args.batch,
            role=source["role"],
            runtime=source["resolved_runtime"],
            purpose=source["purpose"],
            candidate_commit=source.get("candidate_commit"),
            delta_review_of=source.get("delta_review_of"),
            model=source["resolved_model"],
            effort=source["resolved_effort"],
            propose=False,
            transition_digest=None,
            approved_by=None,
            approved_at=None,
            _restart_recovery_sha256=event["record_sha256"],
        )
    )
    return {
        **result,
        "restart_dispatch": successor,
        "restart_dispatch_id": successor["dispatch_id"],
    }


def _stage_rank(root: Path, entry: JsonObject) -> int:
    brief = _load_dispatch(root, entry["dispatch_id"])
    return STAGE_RANKS[
        "publish" if brief.get("purpose") == "publish" else brief["role"]
    ]


def _known_candidate(
    repo: Path, root: Path, batch: JsonObject, entries: list[JsonObject]
) -> str | None:
    """The writer's commit: a validated report or committed checkpoint is progress, not acceptance."""
    from harness.orchestration.core.git_utils import _candidate_commit
    from harness.orchestration.workflow.history import (
        _current_developer_candidate,
        _latest_checkpoint_for_dispatch,
        _pending_report,
    )

    candidate = _current_developer_candidate(repo, root, batch)
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
    return candidate


def _require_known_checkout(repo: Path, batch: JsonObject, snapshot: str) -> None:
    """The issue worktree must sit on known progress with no unknown dirty files."""
    from harness.orchestration.core.git_utils import _git

    checkout = Path(batch["worktree"]).resolve()
    verified_checkout(
        repo,
        {"role": "developer", "branch": batch["branch"], "snapshot_commit": snapshot},
        str(checkout),
    )
    if _git(checkout, "status", "--porcelain", "--untracked-files=all"):
        raise CoordinatorError(
            "rewind found unknown dirty files",
            remedy="preserve and register progress before selecting a fresh gate; rewind never resets files",
        )


def rewind_batch(args: argparse.Namespace) -> JsonObject:
    """Select a previous gate, superseding dependent evidence without changing Git history."""
    from harness.orchestration.core.utils import _repo
    from harness.orchestration.ledger.ledger_ops import (
        _ledger_lock,
        _load_batch,
        _state_root,
    )
    from harness.orchestration.workflow.decisions import _abandon_open_dispatches
    from harness.orchestration.workflow.history import (
        _latest_registered_verification_candidate,
        _validate_batch_integrity,
    )

    approver, note = human_approval(args), _note(args)
    target = getattr(args, "to", None)
    if target not in REWIND_STAGES:
        raise CoordinatorError(
            "unknown rewind gate", remedy=f"choose --to from {', '.join(REWIND_STAGES)}"
        )
    target_rank = STAGE_RANKS[target]
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        ledger.validate()
        batch = _load_batch(root, args.batch)
        _validate_batch_integrity(root, batch)
        _recoverable(batch)
        _no_live_worker(root, batch, repo=repo)
        entries = active_dispatches(batch)
        current_rank = max(
            [
                STAGE_RANKS.get(str(batch.get("next_action")), 0),
                *[_stage_rank(root, e) for e in entries],
            ],
            default=0,
        )
        if target_rank > current_rank:
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
        candidate = _known_candidate(repo, root, batch, entries)
        _require_known_checkout(
            repo,
            batch,
            candidate
            or writer_start(batch)
            or cast(str, batch.get("branch_start_commit", batch["base_commit"])),
        )
        before = batch["state"]
        retired = _abandon_open_dispatches(ledger, root, batch, utils._now())
        evidence: JsonObject = {
            "superseded_dispatches": [
                e["dispatch_id"] for e in entries if _stage_rank(root, e) >= target_rank
            ],
            "superseded_risks": [r["risk_assessment_id"] for r in active_risks(batch)]
            if target_rank <= RISK_INVALIDATING_RANK
            else [],
            "writer_start_commit": candidate,
            "retired_dispatches": retired,
            "target": target,
            "superseded_candidate_registrations": [
                r["source_dispatch_id"] for r in batch.get("candidate_registrations", [])
            ]
            if target_rank <= CANDIDATE_INVALIDATING_RANK
            else [],
        }
        if target == "architect" and "commit_plan" in batch:
            evidence["superseded_commit_plan"] = batch.pop("commit_plan")
        batch["state"] = (
            "awaiting-approval" if batch.get("coordinator_approval") else "planned"
        )
        batch["next_action"] = target
        batch.pop("required_next_role", None)
        batch.pop("environmental_restart", None)
        batch["manual_recovery"] = True
        if target_rank <= CANDIDATE_INVALIDATING_RANK and candidate:
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
        _record_recovery(
            ledger,
            repo,
            batch,
            event,
            retired,
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
            },
        )
        return {**_recovery_result(batch, event), "next_action": target}
