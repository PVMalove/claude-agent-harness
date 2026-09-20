"""A dispatch: one approved, immutable brief handed to exactly one role.

Creating a dispatch is the gated step of the lifecycle -- the batch must be clean of attention, its
base must still be fresh, the transition must be approved, and the brief is written once and never
edited.  Sending, cancelling, waiting on and publishing that brief all live here too, because they
are the same object's later states.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import uuid
from dataclasses import replace as _vo_replace
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Optional, cast

from harness.errors import INTERNAL_INVARIANT_REMEDY
from harness.orchestration import extensions, operational_guards
from harness.orchestration.contract import (
    ContractError, resolve_allowed_tools, resolve_assignment, resolve_runtime_name, valid_tool_list,
    validate_brief_policy,
)
from harness.orchestration.core import config as core_config, utils
from harness.orchestration.dispatch_preflight import PreflightError, prepare as prepare_dispatch
from harness.orchestration.ledger.lifecycle import (
    BatchRecord, DispatchRecord, DispatchStatusRecord, LifecycleLedger, PlanRecord,
)
from harness.orchestration.runtime_attestation import AttestationError, attest as attest_runtime_worktree
from harness.orchestration.core.constants import (
    TERMINAL_BATCH_STATES,
)
from harness.orchestration.core.utils import (
    CoordinatorError, JsonObject, _canonical, _non_empty, _read_object, _repo, _safe_id,
)
from harness.orchestration.core.git_utils import (
    _candidate_commit, _changed_files_between, _commit_evidence, _fetch_ref_tip, _git_is_ancestor,
)
from harness.orchestration.core.config import (
    _adaptive_continuation_policy, _approval_policy, _communication_policy, _configured, _is_test_path,
    _orchestration_policy, _reject_sensitive, _resolve_assignment, _test_path_patterns,
    _worker_attestation_required,
)
from harness.orchestration.core.workspace import (
    _agent_inbox, _integration_ref, _prepare_agent_inbox,
)
from harness.orchestration.ledger.ledger_ops import (
    _ledger_lock, _load_batch, _load_dispatch, _load_dispatch_status, _records_root, _replace_record, _state_root,
    _write_record,
)
from harness.orchestration.workflow.history import (
    _accepted_architect, _accepted_qa_for_candidate, _context_package_freshness, _context_package_summary,
    _effective_base, _latest_context_package, _latest_developer_candidate, _pending_report, _risk_for_candidate,
    _settled, _transition_idempotency_key, _validate_batch_integrity,
)
from harness.orchestration.workflow.approval import (
    _approval,
)
from harness.orchestration.workflow.attention import (
    _require_no_attention,
)
from harness.orchestration.workflow.risk import (
    _matching_triggers, _risk_triggers,
)
from harness.orchestration.workflow.context_package import (
    _persist_context_package,
)
from harness.orchestration.workflow.decisions import (
    _review_severity,
)
from harness.orchestration.workflow.batch import (
    _check_batch_conflicts,
)


def _enforce_base_freshness(repo: Path, root: Path, ledger: LifecycleLedger, batch: JsonObject) -> None:
    """Mandatory re-check, immediately before a review or publish dispatch: the batch's pinned
    integration base must still be the integration ref's current tip. A stale base is cleared only
    by a new developer dispatch (a rebase), never by the coordinator moving this field directly."""
    recorded = batch.get("integration_base_commit")
    if not isinstance(recorded, str) or not recorded:
        raise CoordinatorError("batch has no recorded integration base commit to check freshness against", remedy="this batch predates integration-base freshness tracking; re-plan it to record one")
    ref = _integration_ref(repo, batch)
    current = _fetch_ref_tip(repo, ref)
    if current == recorded:
        return
    batch["next_action"] = "developer"
    batch["required_next_role"] = "developer"
    batch["retry_candidate_required"] = True
    batch["base_rebase_required"] = True
    _safe_id(batch["batch_id"], "batch")
    _replace_record(ledger, BatchRecord.from_dict(batch))
    raise CoordinatorError(
        f"batch base is stale: origin/{ref} has moved from {recorded} to {current}; "
        "only a new developer rebase dispatch can clear this block",
        remedy="run a new developer rebase dispatch to bring the batch base up to date with origin, then retry",
    )


def _dispatch_approval_mode(
    args: argparse.Namespace, batch: JsonObject, config: JsonObject, role: str,
    purpose: str, risk: JsonObject | None,
) -> str:
    """How this dispatch is approved: ``explicit`` (a human) or ``policy:<name>``.

    A project-approved continuation is allowed only outside the preserved risk milestones.
    """
    if _non_empty(getattr(args, "approved_by", None)) or _non_empty(getattr(args, "approved_at", None)):
        return "explicit"
    policy = batch.get("approval_policy", _approval_policy(config))
    risk_triggered = bool(risk and risk.get("matched_triggers"))
    milestone = purpose == "publish" or role == "qa" or risk_triggered or batch.get("risk_reassessment_required")
    if policy == "manual_all" or milestone:
        raise CoordinatorError("this transition requires --approved-by and --approved-at under its approval policy", remedy="pass --approved-by and --approved-at, as required by this project's approval_policy")
    if policy == "low_risk":
        zones = config.get("low_risk_zones", [])
        if batch["zone"] not in zones:
            raise CoordinatorError("low_risk continuation requires the batch zone in low_risk_zones", remedy="add the batch's zone to low_risk_zones in the project orchestration config, or use a different approval_policy")
    return f"policy:{policy}"


def _bind_dispatch_approval(args: argparse.Namespace, mode: str, digest: str) -> dict[str, str]:
    """Record the approval bound to the exact transition it was given for.

    An explicit approval must name the digest the human saw in the proposal; a different digest --
    any change of scope, candidate, role, verification command, reason category or Context Package
    -- means it approved another transition, and nothing is created. A policy approval is derived
    from the very transition being created, so it binds to its own digest.
    """
    if mode != "explicit":
        return {"approved_by": mode, "approved_at": utils._now(), "transition_digest": digest}
    supplied = getattr(args, "transition_digest", None)
    if not _non_empty(supplied):
        raise CoordinatorError(
            "an explicit approval must name the transition digest it approves",
            remedy=f"run 'dispatch propose' with the same arguments and pass its transition_digest as --transition-digest (currently {digest})",
        )
    if supplied.strip() != digest:
        raise CoordinatorError(
            f"approval digest does not match the proposed transition: approved {supplied.strip()}, proposed {digest}",
            remedy="a changed scope, candidate, role, verification command, reason category or Context Package needs a new approval: run 'dispatch propose' again and approve its digest",
        )
    return {**_approval(args, digest), "transition_digest": digest}


def preflight_dispatch(args: argparse.Namespace) -> JsonObject:
    """Render one deterministic dispatch proposal without writing a brief or starting a worker."""
    repo = _repo(args)
    root = _state_root(args, repo)
    config = core_config._config(repo)
    if not _configured(repo):
        raise CoordinatorError("dispatch preflight requires a project-owned .harness/orchestration.json", remedy="create .harness/orchestration.json for this project (see harness init/update) before dispatching")
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        batch = _load_batch(root, args.batch)
        _validate_batch_integrity(root, batch)
        candidate = _candidate_commit(repo, args.candidate_commit) if args.candidate_commit else None
        if candidate is None:
            try:
                candidate = _latest_developer_candidate(repo, root, batch)
            except CoordinatorError:
                candidate = None
        snapshot = candidate or batch["base_commit"]
        package = _latest_context_package(root, batch)
        package_pointer: JsonObject = {}
        if package is not None:
            package_pointer = {
                "package_id": package["context_package_id"],
                "sha256": hashlib.sha256(_canonical(package).encode("utf-8")).hexdigest(),
                "freshness": _context_package_freshness(repo, root, batch),
            }
        checks = (
            batch["developer_verification_commands"]
            if args.role == "developer" else batch["verification_commands"]
        )
        state = {
            "repo": str(repo), "config": config, "branch": batch["branch"], "worktree": batch["worktree"],
            "zone": batch["zone"], "base_sha": batch["base_commit"], "candidate_sha": candidate,
            "snapshot_sha": snapshot, "integration_ref": _integration_ref(repo, batch), "runtime": args.runtime,
            "mandatory_checks": checks, "starting_files": package_pointer,
            "architecture_decision": batch.get("architecture_decision"),
            "affected_symbols": batch.get("affected_symbols", []),
            "related_tests": package.get("related_tests", []) if package else [],
            "pinned_diff": package.get("diff", "") if package else "",
            "prior_findings": batch.get("prior_findings", []),
        }
    try:
        prepared = prepare_dispatch(batch["ticket"], args.role, state)
    except PreflightError as exc:
        raise CoordinatorError(exc.message, remedy=exc.remedy) from exc
    return prepared.to_dict()


def _proposed_transition(
    batch: JsonObject, next_action: object, role_name: str, purpose: str, candidate: str | None,
    risk: JsonObject | None, verification_commands: list[str], context_package: JsonObject | None,
) -> JsonObject:
    """The canonical transition an approval binds: what came before, and exactly what is about to run.

    "What came before" is the newest dispatch a human decided on, so a brief that was created but is
    still unsent (or was cancelled) does not change the transition it was created for."""
    previous = next((item for item in reversed(batch.get("dispatches", [])) if isinstance(item.get("decision"), dict)), None)
    decision = previous.get("decision") if previous else None
    routing = decision.get("routing") if isinstance(decision, dict) else None
    return operational_guards.build_transition(
        batch_id=batch["batch_id"],
        previous_dispatch_id=previous["dispatch_id"] if previous else None,
        previous_role=previous["role"] if previous else None,
        reason_category=routing.get("reason_category") if isinstance(routing, dict) else None,
        next_role=role_name, next_action=str(next_action or "initial"), purpose=purpose, candidate_sha=candidate,
        base_sha=_effective_base(batch), review_scope=list(risk["review_scope"]) if risk else [],
        verification_commands=verification_commands,
        context_package_id=context_package["context_package_id"] if context_package else None,
        required_gates=batch["required_gates"],
    )


def _reject_active_duplicate(root: Path, batch: JsonObject, key: str) -> None:
    """At most one active read-only dispatch per idempotency key. A settled dispatch (decided,
    cancelled or abandoned) never blocks a new one, which always gets a new immutable ID."""
    for path in sorted((_records_root(root) / "batches").glob("batch-*.json")):
        other = _read_object(path, "batch record")
        if other.get("batch_id") == batch.get("batch_id"):
            other = batch
        elif other.get("state") in TERMINAL_BATCH_STATES:
            continue
        for entry in other.get("dispatches", []):
            if _settled(entry):
                continue
            if _load_dispatch(root, entry["dispatch_id"]).get("retry_idempotency_key") == key:
                raise CoordinatorError(
                    f"an active dispatch with the same retry idempotency key already exists: {entry['dispatch_id']}",
                    remedy=f"let {entry['dispatch_id']} settle or cancel it before creating another dispatch for the same role, candidate, base, scope, reason and verification",
                )


def cancel_dispatch(args: argparse.Namespace) -> JsonObject:
    """Cancel one approved brief before it reaches any runtime.

    This is deliberately narrower than batch abandonment: the immutable brief remains available
    for audit, but a discovered assignment/transport mistake can be corrected without failing the
    otherwise valid batch or consuming a worker slot.
    """
    repo = _repo(args)
    root = _state_root(args, repo)
    approval = _approval(args)
    reason = args.reason.strip() if _non_empty(args.reason) else ""
    if not reason:
        raise CoordinatorError("cancelling a dispatch requires a recorded reason", remedy="pass --reason explaining why this dispatch is being cancelled")
    _reject_sensitive({"reason": reason}, "dispatch cancellation reason")
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        dispatch = _load_dispatch(root, args.dispatch)
        batch = _load_batch(root, dispatch["batch_id"])
        _validate_batch_integrity(root, batch)
        entry = next((item for item in batch.get("dispatches", []) if item.get("dispatch_id") == dispatch["dispatch_id"]), None)
        if not entry or entry.get("brief_sha256") != hashlib.sha256(_canonical(dispatch).encode("utf-8")).hexdigest():
            raise CoordinatorError("dispatch record failed immutable brief integrity check", remedy="the dispatch record was modified after its brief integrity hash was recorded -- " + INTERNAL_INVARIANT_REMEDY)
        if entry.get("state") != "approved":
            raise CoordinatorError("only an approved, unsent dispatch may be cancelled", remedy="only cancel a dispatch that is approved and not yet sent")
        status = _load_dispatch_status(root, dispatch["dispatch_id"])
        if status.get("state") != "approved":
            raise CoordinatorError("only an approved, unsent dispatch may be cancelled", remedy="only cancel a dispatch that is approved and not yet sent")
        moment = utils._now()
        entry["state"] = "cancelled"
        entry["cancellation"] = {**approval, "cancelled_at": moment, "reason": reason}
        batch["state"] = "awaiting-approval"
        batch.setdefault("coordinator_decisions", []).append({
            "dispatch_id": dispatch["dispatch_id"], "decision": "cancel", **approval, "note": reason,
        })
        _safe_id(dispatch["dispatch_id"], "dispatch")
        _replace_record(ledger, DispatchStatusRecord.from_dict({
            "dispatch_id": dispatch["dispatch_id"], "state": "cancelled", "updated_at": moment,
            "cancellation": entry["cancellation"],
        }))
        _safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, BatchRecord.from_dict(batch))
    return {"dispatch_id": dispatch["dispatch_id"], "batch_id": batch["batch_id"], "state": "cancelled"}


def _prior_review_entry(batch: JsonObject, dispatch_id: str) -> JsonObject:
    entry = next(
        (item for item in batch.get("dispatches", []) if item.get("dispatch_id") == dispatch_id), None,
    )
    if entry is None or entry.get("role") != "code-review":
        raise CoordinatorError("delta-review-of must reference a code-review dispatch in this batch", remedy="pass --delta-review-of naming a code-review dispatch that belongs to this batch")
    if entry.get("state") != "reported" or entry.get("decision", {}).get("decision") != "retry":
        raise CoordinatorError("delta-review-of must reference a retried code-review dispatch", remedy="pass --delta-review-of naming a code-review dispatch that was actually retried")
    return cast(JsonObject, entry)


def _delta_review_eligibility(
    repo: Path,
    config: JsonObject,
    known: list[str],
    prior_dispatch: JsonObject,
    prior_report: JsonObject,
    candidate: str,
) -> str:
    """Whether ``candidate`` may be delta-reviewed against ``prior_dispatch``'s review.

    Eligible only for a test-only fix after the prior Spec axis raised a Warning or Blocker. The
    Standards evidence is inherited only when it was Clean; Spec is always re-evaluated against the
    new candidate. Any production or risk-triggering diff falls back to a full independent review.
    """
    severities = _review_severity(prior_report["review"])
    if severities.get("standards") != "clean" or severities.get("spec") not in {"warning", "blocker"}:
        raise CoordinatorError(
            "delta-review requires prior Standards=Clean and prior Spec=Warning or Blocker",
            remedy="only delta-review a dispatch whose prior review was Standards=Clean and Spec=Warning or Blocker; otherwise dispatch a full review",
        )
    prior_candidate = prior_dispatch.get("candidate_commit")
    if (
        not isinstance(prior_candidate, str)
        or prior_candidate == candidate
        or not _git_is_ancestor(repo, prior_candidate, candidate)
    ):
        raise CoordinatorError("delta-review requires a new candidate descended from the prior reviewed candidate", remedy="delta-review requires the new candidate to descend from the prior reviewed candidate; rebase or dispatch a full review instead")
    delta_files = _changed_files_between(repo, prior_candidate, candidate)
    if not delta_files:
        raise CoordinatorError("delta-review requires a non-empty fix diff since the prior reviewed candidate", remedy="delta-review requires a non-empty fix diff since the prior reviewed candidate")
    patterns = _test_path_patterns(config)
    non_test = sorted(path for path in delta_files if not _is_test_path(path, patterns))
    if non_test:
        raise CoordinatorError(
            "delta-review is rejected because the fix diff touches non-test file(s): " + ", ".join(non_test),
            remedy="keep a delta-review fix diff to test files only, or dispatch a full review",
        )
    evidence = _commit_evidence(repo, prior_candidate, candidate)
    matched = _matching_triggers(evidence, known)
    if matched:
        raise CoordinatorError(
            "delta-review is rejected because the fix diff matches risk trigger(s): " + ", ".join(sorted(matched)),
            remedy="a fix diff touching a risk trigger needs a full review, not a delta-review",
        )
    return "spec"


def create_dispatch(args: argparse.Namespace) -> JsonObject:
    """Create one approved immutable dispatch brief, or, with ``propose``, render its transition.

    A proposal is the dry run a human approves: it registers the shared Context Package the brief
    would pin and returns the canonical transition with its digest, but writes no brief. Creation
    then requires the same digest, so an approval is valid for exactly the transition it was shown.
    """
    repo = _repo(args)
    root = _state_root(args, repo)
    config = core_config._config(repo)
    propose = bool(getattr(args, "propose", False))
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        batch = _load_batch(root, args.batch)
        _validate_batch_integrity(root, batch)
        if batch.get("state") != "awaiting-approval":
            raise CoordinatorError("a dispatch requires a batch awaiting explicit approval", remedy="move the batch to awaiting explicit approval before creating this dispatch")
        pending_report = any(item.get("state") == "reported" and "decision" not in item for item in batch.get("dispatches", []))
        if pending_report:
            raise CoordinatorError("the previous completion report requires an explicit coordinator decision", remedy="decide (accept/override-warning/retry/block/fail) the previous completion report before continuing")
        _check_batch_conflicts(root, config, batch)
        if not propose:
            _require_no_attention(ledger, repo, root, config, batch)
        # Existing packages are recorded as audit evidence. A fresh role-specific package is
        # registered below before the immutable brief is written, so a stale snapshot cannot be
        # silently reused by a new role.
        context_package_freshness = _context_package_freshness(repo, root, batch)
        if context_package_freshness is not None:
            batch.setdefault("context_package_freshness_checks", []).append(context_package_freshness)
        role_name = args.role
        purpose = args.purpose
        next_action = batch.get("next_action")
        required = {
            None: {("architect", "work"), ("developer", "work")},
            "developer": {("developer", "work")},
            "code-review": {("code-review", "work")},
            # A low-risk candidate goes straight to QA, but the fixed pipeline may still review it.
            "qa": {("qa", "work"), ("code-review", "work")},
            "publish": {("developer", "publish")},
            "developer-retry": {("developer", "work")},
            # A retried architect report gets a new architect, never a developer.
            "architect": {("architect", "work")},
        }
        if next_action == "risk-assessment":
            raise CoordinatorError("risk assessment must prepare the next dispatch before another role starts", remedy="register a risk assessment for this candidate before dispatching another role")
        if (role_name, purpose) not in required.get(next_action, set()):
            raise CoordinatorError("dispatch does not match the coordinator-prepared next action", remedy="dispatch exactly the role/action the coordinator prepared next")
        # The architect step cannot be skipped, whatever the entry point: no coding dispatch exists
        # for a batch whose architect report has not been accepted.
        if role_name == "developer" and purpose == "work" and not _accepted_architect(batch):
            raise CoordinatorError(
                "a developer dispatch requires an accepted architect report for the same batch",
                remedy="accept an architect completion report for this batch before dispatching a developer",
            )
        role, zone, profile_id, model, effort, transport, resolved_runtime = _resolve_assignment(
            repo, config, role_name, batch["zone"], args.runtime,
            session_model=getattr(args, "model", None), session_effort=getattr(args, "effort", None),
        )
        candidate = None
        risk = None
        review_scope: list[str] = []
        delta_review_of: str | None = None
        delta_review_axis: str | None = None
        requested_delta_review_of = getattr(args, "delta_review_of", None)
        if args.candidate_commit is not None:
            candidate = _candidate_commit(repo, args.candidate_commit)
        if role_name in {"code-review", "qa"} and candidate is None:
            raise CoordinatorError(f"{role_name} dispatch requires candidate_commit", remedy=f"pass --candidate-commit before dispatching the {role_name} role")
        if purpose == "publish":
            if role_name != "developer" or candidate is None:
                raise CoordinatorError("publish requires a developer role and candidate_commit", remedy="publish requires a developer role and a pinned candidate_commit")
            if candidate != _latest_developer_candidate(repo, root, batch):
                raise CoordinatorError("publish must use the latest accepted developer candidate", remedy="publish must use the latest accepted developer candidate_commit")
            _accepted_qa_for_candidate(root, batch, candidate)
        elif purpose != "work":
            raise CoordinatorError("dispatch purpose is invalid", remedy="pass a recognized dispatch purpose")
        is_review_work = role_name == "code-review" and purpose == "work"
        if is_review_work or purpose == "publish":
            _enforce_base_freshness(repo, root, ledger, batch)
        if candidate is not None:
            risk = _risk_for_candidate(root, batch, candidate)
        if role_name in {"code-review", "qa"} and candidate != _latest_developer_candidate(repo, root, batch):
            raise CoordinatorError("review and QA dispatches must use the latest accepted developer candidate", remedy="pin the latest accepted developer candidate_commit for this review/QA dispatch")
        if role_name in {"code-review", "qa"} and risk is None:
            raise CoordinatorError("candidate commit has no coordinator risk assessment", remedy="register a risk assessment for this candidate commit before dispatching review/QA")
        if role_name == "code-review":
            # The risk assessment decides when review is *mandatory*, never when it is permitted:
            # the fixed pipeline reviews every candidate, high-risk or not.
            assert risk is not None  # the guard above raised when a code-review dispatch has no risk
            review_scope = list(risk["review_scope"])
            if requested_delta_review_of is not None:
                prior_entry = _prior_review_entry(batch, requested_delta_review_of)
                prior_dispatch = _load_dispatch(root, requested_delta_review_of)
                if prior_dispatch.get("batch_id") != batch["batch_id"]:
                    raise CoordinatorError("delta-review-of must reference a dispatch in this batch", remedy="pass --delta-review-of naming a dispatch that belongs to this batch")
                prior_report = _pending_report(root, batch, prior_entry)
                delta_review_axis = _delta_review_eligibility(
                    repo, config, _risk_triggers(repo), prior_dispatch, prior_report, cast(str, candidate),
                )
                delta_review_of = requested_delta_review_of
        elif requested_delta_review_of is not None:
            raise CoordinatorError("--delta-review-of is only valid for a code-review dispatch", remedy="only pass --delta-review-of for a code-review dispatch")
        if role_name == "qa":
            if batch.get("risk_reassessment_required"):
                raise CoordinatorError("QA is blocked until the candidate is risk-assessed again", remedy="register a new risk assessment for this candidate before dispatching QA")
            assert risk is not None  # the guard above raised when a qa dispatch has no risk
            if risk["review_required"]:
                accepted_review = any(
                    item.get("role") == "code-review"
                    and item.get("state") == "reported"
                    and item.get("decision", {}).get("decision") in {"accept", "override-warning"}
                    and _load_dispatch(root, item["dispatch_id"]).get("candidate_commit") == candidate
                    for item in batch.get("dispatches", [])
                )
                if not accepted_review:
                    raise CoordinatorError("QA requires an accepted composite review for the candidate", remedy="accept a composite review for this candidate before dispatching QA")
        required_role = batch.get("required_next_role")
        if required_role and role_name != required_role:
            raise CoordinatorError(f"the coordinator requires a new {required_role} dispatch before this role", remedy=f"dispatch a new {required_role} role before this one")
        approval_mode = None if propose else _dispatch_approval_mode(args, batch, config, role_name, purpose, risk)
        context_package = None
        if role_name in {"architect", "developer", "code-review"}:
            snapshot = candidate
            if snapshot is None:
                try:
                    snapshot = _latest_developer_candidate(repo, root, batch)
                except CoordinatorError:
                    snapshot = batch["base_commit"]
            context_package = _persist_context_package(
                repo, root, ledger, batch, role="shared", snapshot=snapshot,
                inclusion_reason=(
                    f"automatic shared package for {role_name} at pinned snapshot {snapshot}; "
                    "included before immutable brief creation"
                ),
            )
            context_package_freshness = _context_package_freshness(repo, root, batch)
            if context_package_freshness is None or context_package_freshness["status"] != "fresh":
                raise CoordinatorError("newly registered Context Package is stale; refresh before dispatch", remedy="re-register the Context Package immediately before dispatching; it is validated fresh at dispatch time")
        dispatch_id = f"dispatch-{uuid.uuid4()}"
        dispatch_commands = (
            batch["developer_verification_commands"]
            if role_name == "developer" and purpose == "work"
            else batch["verification_commands"]
        )
        transition = _proposed_transition(
            batch, next_action, role_name, purpose, candidate, risk, dispatch_commands, context_package,
        )
        digest = operational_guards.transition_digest(transition)
        idempotency_key = _transition_idempotency_key(role_name, purpose, transition)
        if idempotency_key is not None:
            _reject_active_duplicate(root, batch, idempotency_key)
        if propose:
            _safe_id(batch["batch_id"], "batch")
            _replace_record(ledger, BatchRecord.from_dict(batch))
            return {
                "batch_id": batch["batch_id"], "state": "proposed", "transition": transition,
                "transition_digest": digest, "retry_idempotency_key": idempotency_key,
                "needs_attention": bool(batch.get("needs_attention", False)),
                "context_package_freshness": context_package_freshness,
            }
        assert approval_mode is not None  # only a proposal skips the approval mode
        approval = _bind_dispatch_approval(args, approval_mode, digest)
        brief: JsonObject = {
            "dispatch_id": dispatch_id,
            "batch_id": batch["batch_id"],
            "ticket": batch["ticket"],
            "role": role_name,
            "access": role["mode"],
            "zone": batch["zone"],
            "write_paths": zone["paths"] if role["mode"] == "write" else [],
            "branch": batch["branch"],
            "worktree": batch["worktree"],
            "definition_of_done": batch["definition_of_done"],
            "prohibited_changes": batch["prohibited_changes"],
            "verification_commands": dispatch_commands,
            "required_gates": batch["required_gates"],
            "dependencies": batch["dependencies"],
            "resolved_runtime": resolved_runtime,
            "resolved_provider_profile": profile_id,
            "resolved_model": model,
            "resolved_effort": effort,
            "resolved_transport": transport,
            "allowed_tools": resolve_allowed_tools(config, role_name, role["mode"]),
            "context_budget": _adaptive_continuation_policy(config)["context_limit"],
            "coordinator_approval": approval,
            "candidate_commit": candidate,
            "review_base": risk["base_commit"] if risk else None,
            "review_scope": review_scope,
            "risk_assessment_id": risk["risk_assessment_id"] if risk else None,
            "purpose": purpose,
            "delta_review_of": delta_review_of,
            "delta_review_axis": delta_review_axis,
            "context_package_id": context_package["context_package_id"] if context_package else None,
            "context_package_sha256": (
                hashlib.sha256(_canonical(context_package).encode("utf-8")).hexdigest()
                if context_package else None
            ),
            "context_package_summary": _context_package_summary(context_package) if context_package else None,
            "worker_attestation_required": _worker_attestation_required(config),
            "communication_policy": batch.get("communication_policy", _communication_policy(config)),
            "snapshot_commit": candidate or batch["base_commit"],
            # Absolute, so a role never resolves a relative reporting path against a guessed
            # current directory and never invents a home-directory folder of its own.
            "report_staging_path": str(_agent_inbox(repo) / f"{dispatch_id}.json"),
            "transition": transition,
            "transition_digest": digest,
            "retry_idempotency_key": idempotency_key,
            "orchestration_policy": _orchestration_policy(config),
        }
        _reject_sensitive(brief, "dispatch brief")
        # The immutable dispatch file is itself the approved brief.  Keeping the brief at the
        # top level lets any runtime-neutral adapter consume exactly the reviewed contract.
        dispatch = dict(brief)
        dispatch["state"] = "approved"
        dispatch["created_at"] = utils._now()
        _safe_id(dispatch_id, "dispatch")
        _write_record(ledger, DispatchRecord.from_dict(dispatch))
        _write_record(ledger, DispatchStatusRecord.from_dict(
            {"dispatch_id": dispatch_id, "state": "approved", "updated_at": utils._now()}
        ))
        batch["dispatches"].append({
            "dispatch_id": dispatch_id,
            "role": role_name,
            "state": "approved",
            "brief_sha256": hashlib.sha256(_canonical(dispatch).encode("utf-8")).hexdigest(),
        })
        if required_role and role_name == required_role:
            batch.pop("required_next_role", None)
        batch["state"] = "active"
        _safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, BatchRecord.from_dict(batch))
    _prepare_agent_inbox(repo)
    return {
        "dispatch_id": dispatch_id, "batch_id": batch["batch_id"], "state": "approved", "brief": brief,
        "report_staging_path": brief["report_staging_path"],
        "context_package_freshness": context_package_freshness,
    }

