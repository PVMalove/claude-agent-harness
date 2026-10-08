"""The coordinator's decision on a completed dispatch, and where the batch goes next.

A decision is the only place a batch's `next_action` moves.  Retry routing is deliberately narrow:
only operational evidence (verification infrastructure, transport, context pressure) may re-run a
read-only role on the same candidate; anything about the code itself goes back to a developer.
"""

from __future__ import annotations

import argparse
import hashlib
from functools import partial
from pathlib import Path
from typing import cast

from harness.errors import INTERNAL_INVARIANT_REMEDY
from harness.orchestration import extensions, qa_lane
from harness.orchestration.contract import low_risk_eligible
from harness.orchestration.core import config as core_config
from harness.orchestration.core import utils
from harness.orchestration.core.config import (
    _extension_names,
    _reject_sensitive,
    _retry_policy,
    _role,
    _worker_attestation_required,
)
from harness.orchestration.core.constants import (
    BLOCK_BYPASS_REASON_CATEGORY,
    BLOCK_BYPASS_STAGES,
    DEVELOPER_REASON_CATEGORIES,
    INCOMPLETE_ITEM_TARGET_ROLES,
    NEXT_ACTION_DISPATCH_ROLE,
    OPERATIONAL_REASON_CATEGORIES,
    RETRY_REASON_CATEGORIES,
    TOOLING_REASON_CATEGORY,
)
from harness.orchestration.core.git_utils import (
    _candidate_commit,
    _fetch_ref_tip,
)
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _non_empty,
    _read_object,
    _repo,
    _safe_id,
)
from harness.orchestration.core.workspace import (
    _agent_authored_file,
    _agent_inbox,
    _integration_ref,
)
from harness.orchestration.ledger.ledger_ops import (
    _ledger_lock,
    _load_batch,
    _load_dispatch,
    _load_dispatch_status,
    _records_root,
    _replace_record,
    _state_root,
)
from harness.orchestration.ledger.lifecycle import (
    BatchRecord,
    DispatchStatusRecord,
    LifecycleLedger,
)
from harness.orchestration.workflow.approval import (
    _approval,
)
from harness.orchestration.workflow import carried_items
from harness.orchestration.workflow import commit_plan as plan_rules
from harness.orchestration.workflow import rebase
from harness.orchestration.workflow import resolver_state
from harness.orchestration.workflow import supersede
from harness.orchestration.workflow.attention import (
    _apply_attention,
    _attention_findings,
)
from harness.orchestration.workflow.history import (
    _latest_developer_candidate,
    _pending_report,
    _require_route,
    _risk_for_candidate,
    _settled,
    _superseding_start_commit,
    _validate_batch_integrity,
    _validate_dispatch,
)
from harness.orchestration.workflow.qa_integration import _ops
from harness.orchestration.workflow.reports import (
    _closure_base,
    _rebase_target,
    _validate_report,
    report_scope_warnings,
)

AUTO_ACCEPT_RATIONALE = "Auto-accepted due to low_risk policy and clean report"
MILESTONE_AUTO_ACCEPT_RATIONALE = (
    "Auto-accepted due to milestone policy and clean non-milestone report"
)
AUTO_POLICY_ACCEPT_RATIONALE = (
    "Auto-accepted due to auto policy and clean report without risk triggers"
)


def _blank_note(args: argparse.Namespace) -> bool:
    """Whether the decision carries no recorded note: absent, blank or ``none``."""
    return not _non_empty(args.note) or args.note.strip().lower() == "none"


def _auto_accept_policy(
    config: JsonObject, batch: JsonObject, dispatch: JsonObject, report: JsonObject
) -> str | None:
    """Return the policy authorized to decide this clean, non-milestone report."""
    policy = config.get("approval_policy")
    if policy != batch.get("approval_policy") or policy not in {
        "low_risk",
        "milestone",
        "auto",
    }:
        return None
    if (
        report.get("outcome") != "completed"
        or str(report.get("blockers", "")).strip().lower() != "none"
        or str(report.get("risks", "")).strip().lower() != "none"
        or report.get("risk_triggers")
        or dispatch.get("purpose") == "publish"
        # A conflict resolution changes code the target never reviewed: a human decides it.
        or dispatch.get("role") == resolver_state.RESOLVER_ROLE
        # A not-covered definition-of-done item is never clean; a justified divergence is.
        or plan_rules.not_covered(report)
        # Nor is a review that left a carried item omitted, unverified or open (issue #499).
        or carried_items.carried_gap(report, dispatch)
        # Nor is a read-only report that left brief items undone (issue #501): only a human
        # carries them forward or narrows a retry to them.
        or report.get("incomplete_items")
        # Nor is a developer report that changed files outside the approved scope (issue #633).
        or report_scope_warnings(report, dispatch)
    ):
        return None
    if policy == "low_risk" and not low_risk_eligible(config, batch):
        return None
    if policy in {"milestone", "auto"}:
        # `auto` also decides a clean QA report; both keep a risk milestone for a human.
        if (policy == "milestone" and dispatch.get("role") == "qa") or batch.get(
            "risk_reassessment_required"
        ):
            return None
        candidate = dispatch.get("candidate_commit")
        if isinstance(candidate, str) and any(
            item.get("candidate_commit") == candidate and item.get("matched_triggers")
            for item in batch.get("risk_assessments", [])
        ):
            return None
    for check in report.get("checks_run", []):
        result = check.get("result") if isinstance(check, dict) else None
        if result != "pass" and not (
            dispatch.get("role") == "architect"
            and result == "not_run_architect_read_only"
        ):
            return None
    review = report.get("review")
    if review is not None:
        if not isinstance(review, dict):
            return None
        for axis in ("standards", "spec"):
            evidence = review.get(axis)
            if (
                not isinstance(evidence, dict)
                or evidence.get("severity") != "clean"
                or evidence.get("findings") != []
                or str(evidence.get("blockers", "")).strip().lower() != "none"
                or str(evidence.get("risks", "")).strip().lower() != "none"
            ):
                return None
    return cast(str, policy)


def _packet_scope_warnings(
    repo: Path,
    batch: JsonObject,
    dispatch: JsonObject,
    report: JsonObject | None,
    entry: JsonObject,
    plan_file: str | None,
) -> list[str]:
    """What ``batch decide`` records as ``scope_warnings`` for this report, or already recorded."""
    if report is None:
        return []
    if "decision" in entry:
        return list(entry["decision"].get("scope_warnings", []))
    plan = (
        _read_commit_plan(repo, batch, plan_file)
        if plan_file and report.get("role") == "architect"
        else None
    )
    return _scope_warnings(batch, dispatch, report, plan)


def decision_packet(args: argparse.Namespace) -> JsonObject:
    """Return concise approval evidence, with immutable report and diff paths kept in the ledger."""
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        batch = _load_batch(root, args.batch)
        _validate_batch_integrity(root, batch)
        entry = None
        if args.dispatch:
            entry = next(
                (
                    item
                    for item in batch.get("dispatches", [])
                    if item.get("dispatch_id") == args.dispatch
                ),
                None,
            )
            if entry is None:
                raise CoordinatorError(
                    "decision packet dispatch does not belong to this batch",
                    remedy="the decision-packet dispatch does not belong to this batch -- "
                    + INTERNAL_INVARIANT_REMEDY,
                )
        else:
            pending = [
                item
                for item in batch.get("dispatches", [])
                if item.get("state") == "reported" and "decision" not in item
            ]
            entry = pending[0] if len(pending) == 1 else None
        if entry is None:
            return {
                "batch_id": batch["batch_id"],
                "ticket": batch["ticket"],
                "action": "approve next dispatch",
                "branch": batch["branch"],
                "worktree": batch["worktree"],
                "base_sha": batch["base_commit"],
                "snapshot_sha": batch["base_commit"],
                "candidate_sha": None,
                "changed_files": [],
                "checks": [],
                "risks": "not assessed yet",
                "blockers": "none",
                "report": None,
                "diff": None,
                "worker_attestation_required": _worker_attestation_required(
                    core_config._config(repo)
                ),
                "needs_attention": bool(batch.get("needs_attention", False)),
                "route_preview": {
                    "carry-over": _carry_over_preview(repo, root, batch, None, args)
                }
                if getattr(args, "findings_file", None) is not None
                else None,
                "approval_reason": "the next immutable dispatch has not been created",
                "options": ["accept", "block", "full review"],
            }
        dispatch = _load_dispatch(root, entry["dispatch_id"])
        report = (
            _pending_report(root, batch, entry)
            if entry.get("state") == "reported"
            else None
        )
        candidate = dispatch.get("candidate_commit")
        changed = (
            report.get("changed_files", [])
            if report
            else dispatch.get("review_scope", [])
        )
        risk = (
            _risk_for_candidate(root, batch, candidate)
            if isinstance(candidate, str)
            else None
        )
        resolve = partial(_candidate_commit, repo)
        coverage, coverage_source = (
            plan_rules.coverage(report, dispatch, resolve) if report else (None, None)
        )
        route_preview: JsonObject | None = None
        if report is not None and "decision" not in entry:
            # The same computation ``batch decide`` runs, with the same flags; nothing is written.
            try:
                retry_preview = _decide_retry_route(
                    repo, root, batch, dispatch, report, args
                )
            except CoordinatorError as exc:
                # ``batch decide --decision retry`` refuses with this error; the packet still renders.
                retry_preview = {
                    "route": None,
                    "refused": exc.message,
                    "remedy": exc.remedy,
                }
            route_preview = {"retry": retry_preview, "abandon": {"route": "abandon"}}
        if getattr(args, "findings_file", None) is not None:
            route_preview = {
                **(route_preview or {}),
                "carry-over": _carry_over_preview(
                    repo,
                    root,
                    batch,
                    (dispatch, report)
                    if report is not None and "decision" not in entry
                    else None,
                    args,
                ),
            }
        elif (
            report is not None
            and "decision" not in entry
            and report.get("incomplete_items")
        ):
            # What ``batch decide --decision accept --carry-incomplete`` would record (issue #501).
            route_preview = {
                **(route_preview or {}),
                "carry-over": _incomplete_carry_preview(batch, dispatch, report),
            }
        packet: JsonObject = {
            "batch_id": batch["batch_id"],
            "ticket": batch["ticket"],
            "action": "decide completion report"
            if report
            else "approve and send dispatch",
            "dispatch_id": dispatch["dispatch_id"],
            "role": dispatch["role"],
            "runtime": dispatch["resolved_runtime"],
            "branch": dispatch["branch"],
            "worktree": dispatch["worktree"],
            "base_sha": batch["base_commit"],
            "snapshot_sha": dispatch.get("snapshot_commit", batch["base_commit"]),
            "candidate_sha": candidate,
            "changed_files": changed,
            "scope": dispatch["write_paths"] or dispatch.get("review_scope", []),
            "worker_attestation_required": dispatch.get(
                "worker_attestation_required", False
            ),
            "needs_attention": bool(batch.get("needs_attention", False)),
            "route_preview": route_preview,
            "transition_digest": dispatch.get("transition_digest"),
            "summary": report.get("output") if report else "immutable brief prepared",
            "checks": report.get("checks_run", [])
            if report
            else [
                {"command": command, "result": "pending"}
                for command in dispatch["verification_commands"]
            ],
            "risks": report.get("risks")
            if report
            else (risk.get("matched_triggers") if risk else "not assessed yet"),
            "blockers": report.get("blockers") if report else "none",
            "dod_coverage": coverage,
            "dod_coverage_source": coverage_source,
            "carried_items": carried_items.packet_items(dispatch, report),
            "carried_items_gap": carried_items.carried_gap(report, dispatch)
            if report
            else [],
            "scope_warnings": _packet_scope_warnings(
                repo,
                batch,
                dispatch,
                report,
                entry,
                getattr(args, "commit_plan_file", None),
            ),
            "incomplete_items": report.get("incomplete_items", []) if report else [],
            "commit_plan_divergence": (
                plan_rules.divergence(report, dispatch, resolve) if report else None
            )
            if dispatch["role"] != "code-review"
            else dispatch.get("commit_plan_divergence"),
            "rebase_check": rebase.rebase_check(repo, report, dispatch)
            if report
            else None,
            "report": str(_records_root(root) / entry["report"]) if report else None,
            "diff": f"git diff {batch['base_commit']}..{candidate}"
            if candidate
            else None,
            "approval_reason": "a human decision is required before the ledger may advance this gate",
            "options": [
                "accept",
                "retry",
                "block",
                "abandon",
                "full review",
                "delta-review",
            ],
        }
        if report and report.get("outcome") == "blocked":
            blocked_options = ["retry", "block", "abandon"]
            packet["options"] = blocked_options
            packet["recovery_route"] = {
                "decision": "retry",
                "options": blocked_options,
                "approval_required": True,
                "next_role": dispatch.get("role"),
            }
        return packet


def _carry_over_preview(
    repo: Path,
    root: Path,
    batch: JsonObject,
    pending: tuple[JsonObject, JsonObject] | None,
    args: argparse.Namespace,
) -> JsonObject:
    """The carry-over routing record ``--findings-file`` would record now; nothing is written.

    With a pending report (its brief and report), that is ``batch decide --decision accept
    --findings-file``; without one, ``batch carry-over``. A refusal renders as the error the command
    refuses with, like the retry preview.
    """
    try:
        if pending is None:
            return carried_items.carry_over_preview(
                repo, root, batch, args.findings_file
            )
        dispatch, report = pending
        if report.get("outcome") != "completed":
            raise CoordinatorError(
                "a non-completed role report cannot be accepted or warning-overridden",
                remedy="only accept or warning-override a completed role report",
            )
        findings = _decision_findings(
            repo,
            dispatch,
            report,
            argparse.Namespace(**{**vars(args), "decision": "accept"}),
        )
        return carried_items.carry_over_routing(
            _candidate_commit(repo, report["commit_sha"]),
            carried_items.next_item_ids(batch, len(findings)),
        )
    except CoordinatorError as exc:
        return {"route": None, "refused": exc.message, "remedy": exc.remedy}


def _developer_retry_count(batch: JsonObject) -> int:
    """Count approved retry decisions, not ordinary initial developer dispatches. A
    ``tooling-retry`` re-runs work a tool blocked, so it spends no developer retry (issue #500)."""
    return sum(
        1
        for decision in batch.get("coordinator_decisions", [])
        if decision.get("decision") == "retry"
        and decision.get("next_role") == "developer"
        and not (
            isinstance(decision.get("routing"), dict)
            and decision["routing"].get("route") == "tooling-retry"
        )
    )


def _developer_retry_budget_exhausted(config: JsonObject, batch: JsonObject) -> bool:
    return (
        _developer_retry_count(batch) >= _retry_policy(config)["max_developer_retries"]
    )


def _review_severity(review: JsonObject) -> dict[str, str]:
    return {axis: review[axis]["severity"] for axis in ("standards", "spec")}


def _retry_evidence(
    report: JsonObject, candidate_moved: bool
) -> tuple[str, str] | None:
    """The reason category a report's structured data dictates by itself, and what showed it.

    Findings, a warning/blocker severity on either review axis, a failed check and a moved
    candidate all mean the work itself has to change. Free text (``blockers``, ``output``) is never
    consulted: a sentence that merely mentions infrastructure proves nothing.
    """
    review = report.get("review")
    if isinstance(review, dict):
        for axis in ("standards", "spec"):
            evidence = review.get(axis)
            if isinstance(evidence, dict) and (
                evidence.get("findings")
                or evidence.get("severity") in {"warning", "blocker"}
            ):
                return (
                    "requirements" if axis == "spec" else "code"
                ), f"the {axis} axis carries a finding or a warning/blocker severity"
    if carried_items.marks_open(report):
        return "code", "the review confirms a carried item is still open"
    checks = report.get("checks_run")
    if isinstance(checks, list) and any(
        isinstance(check, dict) and check.get("result") == "fail" for check in checks
    ):
        return "code", "a verification check failed"
    if candidate_moved:
        return "candidate-change", "the candidate changed after the dispatch was pinned"
    return None


def _retry_routing(
    stage: str,
    report: JsonObject,
    *,
    dispatch_candidate: str | None,
    current_candidate: str | None,
    explicit_category: str | None,
    pressure_recorded: bool = False,
    narrowed: bool = False,
) -> JsonObject:
    """Decide where a ``retry`` goes, from structured report data only (no I/O).

    ``stage`` is the pipeline stage that reported: architect, developer, code-review, qa or
    publish. A read-only stage (or the publish boundary) is re-run on the *same* candidate only
    when the report was blocked and nothing but an operational reason -- verification
    infrastructure or transport -- explains it. Any finding, failed check, moved candidate or
    unclear reason routes to a developer retry, which is always the safe route. ``context-pressure``
    is operational only when a critical ``context_pressure`` observation was recorded for the reported
    dispatch (``pressure_recorded``); a claim without that observation is ``unknown``. ``tooling``
    comes only from a blocked report's structured ``tooling_blocker`` (an approver may name it, never
    replace it) and routes ``tooling-retry``: the same stage again, a developer from its last commit.
    ``block-bypass`` comes only from an approver, for a read-only stage whose role worked around a
    block: the report is no evidence, so only a moved candidate overrides its ``bypass-rerun``.
    ``narrowed`` (``--narrowed``) re-runs a read-only stage on its report's incomplete items only.
    """
    if (
        explicit_category is not None
        and explicit_category not in RETRY_REASON_CATEGORIES
    ):
        raise CoordinatorError(
            f"unknown retry reason category {explicit_category!r}",
            remedy=f"pass --reason-category as one of: {', '.join(RETRY_REASON_CATEGORIES)}",
        )
    if narrowed:
        return _narrowed_routing(
            stage,
            report,
            dispatch_candidate=dispatch_candidate,
            current_candidate=current_candidate,
            explicit_category=explicit_category,
        )
    bypass_named = explicit_category == BLOCK_BYPASS_REASON_CATEGORY
    if bypass_named and stage not in BLOCK_BYPASS_STAGES:
        raise CoordinatorError(
            f"{BLOCK_BYPASS_REASON_CATEGORY} re-runs only a read-only {', '.join(BLOCK_BYPASS_STAGES)} report, not a {stage} report",
            remedy=f"retry a {stage} that worked around a hook or tool block with a developer reason category ({', '.join(DEVELOPER_REASON_CATEGORIES)}), or block the batch",
        )
    candidate_bound = stage in {"code-review", "qa", "publish"}
    unchanged = (
        dispatch_candidate is not None and dispatch_candidate == current_candidate
    )
    outcome = report.get("outcome")
    structured = _retry_evidence(
        {} if bypass_named else report, candidate_bound and not unchanged
    )
    if explicit_category in DEVELOPER_REASON_CATEGORIES:
        category, basis = explicit_category, "the approver named this reason category"
    elif structured is not None:
        category, basis = structured
    elif bypass_named:
        category = BLOCK_BYPASS_REASON_CATEGORY
        basis = "the approver found that the role worked around a hook or tool block, so its findings, checks and outcome are no evidence"
    elif (
        explicit_category in {None, TOOLING_REASON_CATEGORY}
        and outcome == "blocked"
        and "tooling_blocker" in report
    ):
        category = TOOLING_REASON_CATEGORY
        basis = "the blocked report carries a structured tooling_blocker (tool, command, message)"
    elif explicit_category == TOOLING_REASON_CATEGORY:
        category = "unknown"
        basis = f"the approver named tooling, but outcome={outcome} carries no structured tooling_blocker"
    elif explicit_category == "context-pressure" and not pressure_recorded:
        category = "unknown"
        basis = "context-pressure was named, but no critical context_pressure observation exists for the reported dispatch"
    elif explicit_category in OPERATIONAL_REASON_CATEGORIES and outcome == "blocked":
        category = explicit_category
        basis = f"the report is blocked with no finding, no failed check and an unchanged candidate, and the approver named {category}"
    elif explicit_category in OPERATIONAL_REASON_CATEGORIES:
        category = "unknown"
        basis = f"the approver named {explicit_category}, but outcome={outcome} does not show a blocked run"
    else:
        category, basis = "unknown", "no structured evidence classifies the cause"
    if category == TOOLING_REASON_CATEGORY:
        next_action = "developer-retry" if stage == "developer" else stage
        route = "tooling-retry"
        restart = (
            "continues from the developer's last commit"
            if stage == "developer"
            else "repeats the stage on the same SHA"
        )
        outcome_sentence = (
            f"a new {stage} dispatch {restart}; it spends no developer retry and the "
            "earlier brief, report and tooling blocker stay as audit evidence"
        )
    elif category == BLOCK_BYPASS_REASON_CATEGORY:
        next_action, route = stage, "bypass-rerun"
        outcome_sentence = (
            f"a new independent {stage} dispatch runs on the same SHA under an explicit approval; "
            "the bypassing report is neither accepted nor overridden and stays only as audit evidence"
        )
    elif stage == "architect":
        next_action, route = "architect", "architect-retry"
        outcome_sentence = "a new architect dispatch runs; no developer starts before an architect report is accepted"
    elif (
        stage == resolver_state.RESOLVER_ROLE
        and (report.get("resolver") or {}).get("cause") != "task-defect"
    ):
        # An integration incompatibility continues with a resolver; the ticket's own defect falls
        # through to a regular developer retry below.
        next_action, route = resolver_state.RESOLVER_NEXT_ACTION, "same-candidate-rerun"
        outcome_sentence = "a new conflict-resolver dispatch retries the same target; it is a fix on that target and spends no cycle"
    elif candidate_bound and category in OPERATIONAL_REASON_CATEGORIES:
        next_action, route = stage, "same-candidate-rerun"
        outcome_sentence = (
            f"a new independent {stage} dispatch runs on the unchanged candidate; "
            "the earlier brief, report and blocker stay as audit evidence"
        )
    else:
        next_action, route = "developer-retry", "developer-retry"
        outcome_sentence = "a developer retry must produce a new candidate commit, which needs a new risk assessment"
    return {
        "route": route,
        "previous_role": stage,
        "reason_category": category,
        "next_role": {
            "developer-retry": "developer",
            resolver_state.RESOLVER_NEXT_ACTION: resolver_state.RESOLVER_ROLE,
        }.get(next_action, next_action),
        "next_action": next_action,
        "rationale": f"{stage} reported outcome={outcome}: {basis}; {outcome_sentence}.",
        "candidate_commit": dispatch_candidate if unchanged else None,
    }


def _narrowed_routing(
    stage: str,
    report: JsonObject,
    *,
    dispatch_candidate: str | None,
    current_candidate: str | None,
    explicit_category: str | None,
) -> JsonObject:
    """A retry of the same read-only stage on the incomplete items its report listed (issue #501).

    The items alone decide the route: ``narrowed-retry``, or ``tooling-retry`` (category
    ``tooling``) when any item carries a ``tooling_blocker``. Structured evidence that the work
    itself must change (a finding, a warning/blocker severity, an open carried item, a failed
    check, a moved candidate) is never set aside by narrowing. No developer retry is spent.
    """
    if stage not in INCOMPLETE_ITEM_TARGET_ROLES:
        raise CoordinatorError(
            f"--narrowed re-runs only a {', '.join(INCOMPLETE_ITEM_TARGET_ROLES)} report, "
            f"not a {stage} report",
            remedy="drop --narrowed; the retry then routes by the report's structured evidence",
        )
    items = report.get("incomplete_items") or []
    if not items:
        raise CoordinatorError(
            f"--narrowed needs a {stage} report that lists incomplete_items",
            remedy="drop --narrowed; the retry then routes by the report's structured evidence",
        )
    if explicit_category is not None:
        raise CoordinatorError(
            "--narrowed takes no --reason-category: the incomplete items decide the route",
            remedy="drop --reason-category; an item's tooling_blocker makes the narrowed retry "
            "a tooling-retry",
        )
    unchanged = (
        dispatch_candidate is not None and dispatch_candidate == current_candidate
    )
    structured = _retry_evidence(
        report, stage in {"code-review", "qa"} and not unchanged
    )
    if structured is not None:
        raise CoordinatorError(
            f"a narrowed retry cannot set aside structured evidence: {structured[1]}",
            remedy=f"drop --narrowed; the retry then routes by that evidence "
            f"(reason category {structured[0]})",
        )
    tooled = sum(1 for item in items if "tooling_blocker" in item)
    if tooled:
        category: str | None = TOOLING_REASON_CATEGORY
        route = "tooling-retry"
        basis = f"{tooled} of its {len(items)} incomplete items carry a tooling_blocker"
    else:
        category, route = None, "narrowed-retry"
        basis = f"it lists {len(items)} incomplete items"
    return {
        "route": route,
        "previous_role": stage,
        "reason_category": category,
        "next_role": stage,
        "next_action": stage,
        "rationale": f"{stage} reported outcome={report.get('outcome')}: {basis}; a new {stage} "
        "dispatch on the same SHA carries only those items and spends no developer retry; the "
        "earlier brief and report stay as audit evidence.",
        "candidate_commit": dispatch_candidate if unchanged else None,
    }


def _reporting_stage(dispatch: JsonObject, report: JsonObject) -> str:
    """The pipeline stage a report closes: ``publish`` is a purpose of the developer role."""
    return (
        "publish" if dispatch.get("purpose") == "publish" else cast(str, report["role"])
    )


def _abandon_routing(stage: str, report: JsonObject, moment: str) -> JsonObject:
    """The routing record of an ``abandon`` decision (no I/O).

    It has the shape of a retry's record but starts nothing, so it names no reason, role, action or
    candidate. Its rationale holds structural facts only; the approver's ``--reason`` stays the
    decision note.
    """
    return {
        "route": _require_route("abandon"),
        "previous_role": stage,
        "reason_category": None,
        "next_role": None,
        "next_action": None,
        "candidate_commit": None,
        "rationale": f"{stage} reported outcome={report.get('outcome')}: the batch is abandoned; "
        "no next dispatch starts and every brief, report and candidate stays as audit evidence.",
        "decided_at": moment,
    }


def _abandon_open_dispatches(
    ledger: LifecycleLedger, root: Path, batch: JsonObject, moment: str
) -> list[str]:
    """Mark every dispatch that can no longer settle as ``abandoned``; nothing is deleted."""
    abandoned = []
    for entry in batch.get("dispatches", []):
        if _settled(entry):
            continue
        entry["state"] = "abandoned"
        abandoned.append(entry["dispatch_id"])
        status_path = (
            _records_root(root)
            / DispatchStatusRecord.directory
            / f"{_safe_id(entry['dispatch_id'], 'dispatch')}.json"
        )
        if status_path.exists():
            status = _load_dispatch_status(root, entry["dispatch_id"])
            status.update({"state": "abandoned", "updated_at": moment})
            _replace_record(ledger, DispatchStatusRecord.from_dict(status))
    return abandoned


def _discard_batch_leftovers(
    repo: Path, ledger: LifecycleLedger, batch: JsonObject, abandoned: list[str]
) -> list[str]:
    """Delete what an abandoned batch left behind that is not audit evidence.

    That is the staged copy of each report in the agent inbox (the immutable, hash-checked report
    lives in the ledger) and the QA queue entry of a dispatch that will never run, which would
    otherwise hold every later QA run behind it. Briefs, reports, Context Packages, the worktree
    and the candidate are never touched.
    """
    inbox = _agent_inbox(repo)
    removed = []
    for entry in batch.get("dispatches", []):
        staged = inbox / f"{_safe_id(entry['dispatch_id'], 'dispatch')}.json"
        if staged.is_file():
            staged.unlink()
            removed.append(staged.name)
    removed.extend(qa_lane.release_queue(ledger, abandoned, _ops()))
    return removed


def _last_accepted(repo: Path, root: Path, batch: JsonObject) -> JsonObject | None:
    """The newest accepted stage of a batch: the point a fresh batch can be built from.

    Its candidate is the latest accepted developer candidate. A superseding batch without one
    still holds the candidate it started from (``supersedes.start_commit``), so a batch that
    supersedes it in turn resumes there too (issue #506)."""
    entry = next(
        (
            item
            for item in reversed(batch.get("dispatches", []))
            if item.get("state") == "reported"
            and isinstance(item.get("decision"), dict)
            and item["decision"].get("decision") in {"accept", "override-warning"}
        ),
        None,
    )
    if entry is None:
        return None
    try:
        candidate: str | None = _latest_developer_candidate(repo, root, batch)
    except CoordinatorError:
        candidate = _superseding_start_commit(batch)
    return {
        "dispatch_id": entry["dispatch_id"],
        "role": entry["role"],
        "candidate_commit": candidate,
    }


def _pinned_commit_plan(
    repo: Path, batch: JsonObject, report: JsonObject, args: argparse.Namespace
) -> list[JsonObject] | None:
    """The operator-supplied commit plan an architect accept pins on the batch, validated.

    The policy auto-accept builds its own namespace without ``commit_plan_file``, so it never pins.
    """
    plan_file = getattr(args, "commit_plan_file", None)
    if plan_file is None:
        return None
    if args.decision != "accept" or report.get("role") != "architect":
        raise CoordinatorError(
            "--commit-plan-file is only valid when accepting an architect report",
            remedy="drop --commit-plan-file, or pass it with --decision accept on the pending architect report",
        )
    return _read_commit_plan(repo, batch, plan_file)


def _read_commit_plan(
    repo: Path, batch: JsonObject, plan_file: str
) -> list[JsonObject]:
    document = _read_object(
        _agent_authored_file(repo, plan_file, "a commit plan"), "commit plan"
    )
    _reject_sensitive(document, "commit plan")
    return plan_rules.pinned_plan(document, batch["definition_of_done"])


def _scope_warnings(
    batch: JsonObject,
    dispatch: JsonObject,
    report: JsonObject,
    plan: list[JsonObject] | None,
) -> list[str]:
    """The paths outside the batch's approved scope that a decision on this report must surface.

    An architect accept with a pinned plan reports the plan's ``expected_paths`` outside the batch
    ``allowed_paths``; a completed developer report reports its ``changed_files`` outside the
    brief's ``write_paths`` (issue #633). Neither is a refusal by itself.
    """
    if report.get("role") == "architect":
        return (
            plan_rules.paths_outside_scope(plan, batch.get("allowed_paths"))
            if plan is not None
            else []
        )
    return report_scope_warnings(report, dispatch)


def _scope_finding(paths: list[str]) -> JsonObject:
    """The carried item an override of out-of-scope developer changes hands to code-review."""
    return {
        "summary": "The developer changed files outside the approved write_paths and the "
        "coordinator accepted them with override-warning.",
        "files": paths,
        "expected_evidence": "Confirm each listed file change is necessary for the ticket, "
        "stays within its intent and breaks no boundary the approved scope protected.",
    }


def _decision_findings(
    repo: Path, dispatch: JsonObject, report: JsonObject, args: argparse.Namespace
) -> list[JsonObject]:
    """The coordinator findings an accept of a developer work report carries into review.

    The policy auto-accept builds its own namespace without ``findings_file``, so it never carries
    any; ``batch carry-over`` attaches them after such an accept.
    """
    findings_file = getattr(args, "findings_file", None)
    if findings_file is None:
        return []
    if (
        args.decision not in {"accept", "override-warning"}
        or report.get("role") != "developer"
        or dispatch.get("purpose") != "work"
    ):
        raise CoordinatorError(
            "--findings-file is only valid when accepting a completed developer work report",
            remedy="drop --findings-file; to carry findings into review after a developer "
            "report is accepted, run batch carry-over --batch <batch-id> --findings-file <path> "
            "before its code-review dispatch is created",
        )
    return carried_items.read_findings(repo, findings_file)


def _incomplete_carry(
    batch: JsonObject,
    dispatch: JsonObject,
    report: JsonObject,
    args: argparse.Namespace,
) -> JsonObject | None:
    """The carry-over routing record an accept with ``--carry-incomplete`` records (issue #501).

    A read-only report that left brief items undone is never accepted as if it were whole: a plain
    accept or override-warning is refused, and the items go either into their target roles'
    briefs (``--carry-incomplete``) or into a narrowed retry of the same stage. ``None`` means the
    decision carries nothing.
    """
    items = report.get("incomplete_items") or []
    carry = bool(getattr(args, "carry_incomplete", False))
    accepting = args.decision in {"accept", "override-warning"}
    if carry and (not accepting or not items):
        raise CoordinatorError(
            "--carry-incomplete is only valid when accepting a report that lists incomplete_items",
            remedy="drop --carry-incomplete; it carries the incomplete items of a pending "
            "read-only report into later briefs with --decision accept or override-warning",
        )
    if not accepting or not items:
        return None
    stage = _reporting_stage(dispatch, report)
    if not carry:
        raise CoordinatorError(
            f"the {stage} report left {len(items)} brief item(s) undone, so a plain "
            f"{args.decision} would drop them",
            remedy="pass --carry-incomplete to carry the items into the briefs of their target "
            "roles, or retry the same role on these items only with --decision retry --narrowed",
        )
    own = [
        position
        for position, item in enumerate(items, start=1)
        if item["target_role"] == stage
    ]
    if own:
        raise CoordinatorError(
            f"incomplete items {own} target the {stage} role itself, so no later brief can "
            "carry them",
            remedy=f"retry the {stage} on its incomplete items with --decision retry --narrowed",
        )
    candidate = dispatch.get("candidate_commit")
    return carried_items.incomplete_carry_routing(
        stage,
        candidate if isinstance(candidate, str) else None,
        items,
        carried_items.next_incomplete_item_ids(batch, len(items)),
    )


def _incomplete_carry_preview(
    batch: JsonObject, dispatch: JsonObject, report: JsonObject
) -> JsonObject:
    """The carry-over record ``batch decide --decision accept --carry-incomplete`` would record
    now, or the refusal it would raise; nothing is written."""
    try:
        if report.get("outcome") != "completed":
            raise CoordinatorError(
                "a non-completed role report cannot be accepted or warning-overridden",
                remedy="only accept or warning-override a completed role report",
            )
        return cast(
            JsonObject,
            _incomplete_carry(
                batch,
                dispatch,
                report,
                argparse.Namespace(decision="accept", carry_incomplete=True),
            ),
        )
    except CoordinatorError as exc:
        return {"route": None, "refused": exc.message, "remedy": exc.remedy}


def decide_batch(args: argparse.Namespace) -> JsonObject:
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        batch = _load_batch(root, args.batch)
        _validate_batch_integrity(root, batch)
        pending = [
            item
            for item in batch.get("dispatches", [])
            if item.get("state") == "reported" and "decision" not in item
        ]
        if len(pending) != 1:
            raise CoordinatorError(
                "batch has no single completion report awaiting a coordinator decision",
                remedy="wait for exactly one completion report to be awaiting a coordinator decision on this batch",
            )
        report = _pending_report(root, batch, pending[0])
        dispatch = _load_dispatch(root, pending[0]["dispatch_id"])
        config = core_config._config(repo)
        _validate_dispatch(repo, config, root, batch, dispatch)
        _validate_report(
            report,
            dispatch,
            _role(repo, dispatch["role"]),
            repo,
            rebase.report_base(repo, root, batch, dispatch),
            _rebase_target(batch, dispatch),
            _closure_base(repo, root, batch, dispatch),
        )
        resolver_state.validate_report(repo, root, batch, dispatch, report)
        if report.get("outcome") != "completed" and args.decision in {
            "accept",
            "override-warning",
        }:
            raise CoordinatorError(
                "a non-completed role report cannot be accepted or warning-overridden",
                remedy="only accept or warning-override a completed role report",
            )
        if getattr(args, "narrowed", False) and args.decision != "retry":
            raise CoordinatorError(
                "--narrowed is only valid with --decision retry",
                remedy="drop --narrowed, or pass it with --decision retry to re-run the same "
                "read-only role on its incomplete items only",
            )
        pinned_plan = _pinned_commit_plan(repo, batch, report, args)
        scope_warnings = _scope_warnings(batch, dispatch, report, pinned_plan)
        developer_scope = bool(scope_warnings) and report.get("role") == "developer"
        findings = _decision_findings(repo, dispatch, report, args)
        incomplete_carry = _incomplete_carry(batch, dispatch, report, args)
        uncovered = plan_rules.not_covered(report)
        gap = carried_items.carried_gap(report, dispatch)
        if developer_scope:
            if args.decision == "accept":
                raise CoordinatorError(
                    f"the report changed files outside the approved scope {scope_warnings}, so it is not clean",
                    remedy="retry the developer, or pass --decision override-warning with a "
                    "--note (other than 'none') and --approved-by explaining why the "
                    "out-of-scope changes may be accepted",
                )
            if args.decision == "override-warning" and (_blank_note(args)):
                raise CoordinatorError(
                    "overriding changes outside the approved scope requires a recorded note",
                    remedy="pass --note (other than 'none') explaining why the files outside "
                    "the approved scope may be accepted",
                )
        if report.get("role") == "code-review":
            severities = _review_severity(report["review"])
            warned = any(value == "warning" for value in severities.values())
            if any(value == "blocker" for value in severities.values()):
                if _developer_retry_budget_exhausted(config, batch):
                    if args.decision not in {"block", "fail", "abandon"}:
                        raise CoordinatorError(
                            "a review blocker cannot be accepted and the developer retry budget is exhausted for this batch",
                            remedy="block, fail, or abandon (with --reason) this batch, then split or re-plan the work",
                        )
                elif args.decision not in {"retry", "abandon"}:
                    raise CoordinatorError(
                        "a review blocker requires a new developer retry",
                        remedy="start a new developer retry dispatch to address the review blocker, or abandon (with --reason) this batch",
                    )
            elif warned or gap:
                if args.decision == "accept" and warned:
                    raise CoordinatorError(
                        "a review warning requires override-warning or retry",
                        remedy="pass --decision override-warning (with --note) or retry for a review warning",
                    )
                if args.decision == "accept":
                    raise CoordinatorError(
                        f"the review did not close carried items {gap}, so the report is not clean",
                        remedy="retry the developer, or pass --decision override-warning with a "
                        "--note explaining why the omitted, unverified or open items may be accepted",
                    )
                if args.decision == "override-warning" and gap and (_blank_note(args)):
                    raise CoordinatorError(
                        "overriding carried items the review did not close requires a recorded note",
                        remedy="pass --note (other than 'none') explaining why the carried items "
                        "the review did not close may be accepted",
                    )
                if args.decision == "override-warning" and not _non_empty(args.note):
                    raise CoordinatorError(
                        "warning override requires a recorded note",
                        remedy="pass --note explaining the warning override",
                    )
            elif args.decision == "override-warning":
                raise CoordinatorError(
                    "override-warning requires a review warning",
                    remedy="only use override-warning to resolve a recorded review warning",
                )
        elif uncovered:
            items = [record["dod_item"] for record in uncovered]
            if args.decision == "accept":
                raise CoordinatorError(
                    f"definition-of-done items {items} are not covered, so the report is not clean",
                    remedy="retry the developer, or pass --decision override-warning with a --note "
                    "explaining why the uncovered items may be accepted",
                )
            if args.decision == "override-warning" and (_blank_note(args)):
                raise CoordinatorError(
                    "overriding not-covered definition-of-done items requires a recorded note",
                    remedy="pass --note (other than 'none') explaining why the uncovered items may be accepted",
                )
        elif gap:
            # A developer-retry report that leaves a carried item not_closed (issue #503).
            if args.decision == "accept":
                raise CoordinatorError(
                    f"carried items {gap} are not closed, so the report is not clean",
                    remedy="retry the developer, or pass --decision override-warning with a "
                    "--note explaining why the items that are not closed may be accepted",
                )
            if args.decision == "override-warning" and (_blank_note(args)):
                raise CoordinatorError(
                    "overriding carried items that are not closed requires a recorded note",
                    remedy="pass --note (other than 'none') explaining why the carried items "
                    "that are not closed may be accepted",
                )
        elif args.decision == "override-warning" and not developer_scope:
            raise CoordinatorError(
                "only a recorded review warning, a not-covered definition-of-done item, a carried item that is not closed or a change outside the approved scope can be overridden",
                remedy="only override a recorded review warning, a not-covered definition-of-done item, a carried item that is not closed or a change outside the approved scope",
            )
        routing: JsonObject | None = None
        if args.decision == "retry":
            routing = _decide_retry_route(repo, root, batch, dispatch, report, args)
            bypass_named = (
                getattr(args, "reason_category", None) == BLOCK_BYPASS_REASON_CATEGORY
            )
            if bypass_named and (_blank_note(args)):
                raise CoordinatorError(
                    "a block-bypass retry requires a recorded note naming the violation",
                    remedy="pass --note (other than 'none') naming the hook or tool block the role worked around and how",
                )
            routing["decided_at"] = utils._now()
            if routing["next_action"] == resolver_state.RESOLVER_NEXT_ACTION:
                resolver_state.require_fix_budget(root, config, batch)
            if routing["route"] == "verification":
                report_path = _records_root(root) / pending[0]["report"]
                batch.setdefault("candidate_registrations", []).append(
                    {
                        "candidate_commit": routing["candidate_commit"],
                        "source_dispatch_id": dispatch["dispatch_id"],
                        "source_report_sha256": hashlib.sha256(
                            report_path.read_bytes()
                        ).hexdigest(),
                        "reason_category": routing["reason_category"],
                        "registered_at": routing["decided_at"],
                    }
                )
            if (
                routing["next_action"] == "developer-retry"
                and routing["route"] != "tooling-retry"
                and _developer_retry_budget_exhausted(config, batch)
            ):
                raise CoordinatorError(
                    "developer retry budget is exhausted for this batch; block, fail, or abandon it instead of starting another worker",
                    remedy="block, fail, or abandon (with --reason) this batch, then split or re-plan the work",
                )
        abandon_reason = ""
        abandon_routing: JsonObject | None = None
        if args.decision == "abandon":
            abandon_reason = (
                args.reason.strip() if _non_empty(getattr(args, "reason", None)) else ""
            )
            if not abandon_reason:
                raise CoordinatorError(
                    "abandoning a batch requires a recorded reason",
                    remedy="pass --reason explaining why this batch is abandoned",
                )
            _reject_sensitive({"reason": abandon_reason}, "abandon reason")
            abandon_routing = _abandon_routing(
                _reporting_stage(dispatch, report), report, utils._now()
            )
        policy_auto_accept = getattr(args, "_policy_auto_accept", False)
        if policy_auto_accept:
            accepted_policy = _auto_accept_policy(config, batch, dispatch, report)
            if args.decision != "accept" or accepted_policy is None:
                raise CoordinatorError(
                    "policy auto-accept requires a clean non-milestone completed report",
                    remedy="leave this report for an explicit coordinator decision",
                )
            approval = {
                "approved_by": f"policy:{accepted_policy}",
                "approved_at": utils._now(),
            }
            approver = {"kind": "policy", "name": accepted_policy}
        else:
            approval = _approval(args)
            approver = {"kind": "human", "name": approval["approved_by"]}
        decision = {
            "decision": args.decision,
            "approved_by": approval["approved_by"],
            "approved_at": approval["approved_at"],
            "note": (
                AUTO_ACCEPT_RATIONALE
                if accepted_policy == "low_risk"
                else AUTO_POLICY_ACCEPT_RATIONALE
                if accepted_policy == "auto"
                else MILESTONE_AUTO_ACCEPT_RATIONALE
            )
            if policy_auto_accept
            else abandon_reason
            or (args.note.strip() if _non_empty(args.note) else "none"),
        }
        if routing is not None:
            decision["routing"] = routing
        elif abandon_routing is not None:
            decision["routing"] = abandon_routing
        if args.decision in {"accept", "override-warning"}:
            divergence = plan_rules.divergence(
                report, dispatch, partial(_candidate_commit, repo)
            )
            if divergence is not None:
                decision["commit_plan_divergence"] = divergence
            if uncovered and args.decision == "override-warning":
                decision["dod_not_covered"] = uncovered
            if gap and args.decision == "override-warning":
                decision["carried_items_gap"] = gap
            if scope_warnings:
                decision["scope_warnings"] = scope_warnings
            check = rebase.rebase_check(repo, report, dispatch)
            if check is not None:
                # Audit evidence for a later delta-review of the rebased copies (issue #504).
                decision["rebase_check"] = check
        if pinned_plan is not None:
            batch["commit_plan"] = pinned_plan
            decision["commit_plan_sha256"] = plan_rules.plan_sha256(pinned_plan)
        if (
            developer_scope
            and args.decision == "override-warning"
            and dispatch.get("purpose") == "work"
        ):
            # The accepted warning becomes an item the next code-review brief must settle.
            findings = [*findings, _scope_finding(scope_warnings)]
        if findings:
            candidate = _candidate_commit(repo, report["commit_sha"])
            records = carried_items.attach(
                batch,
                findings,
                source=carried_items.coordinator_source(pending[0], candidate),
                attached_at=decision["approved_at"],
                attached_by=decision["approved_by"],
            )
            # Recorded for audit only: ``next_action`` moves through risk assessment as on any
            # developer accept, and the open findings send the candidate to code-review there.
            decision["routing"] = carried_items.carry_over_routing(
                candidate, [record["item_id"] for record in records]
            )
        if incomplete_carry is not None:
            # Recorded, not applied: ``next_action`` moves as on any accept of this stage, and
            # each target role's brief reads the items back from the report.
            decision["routing"] = incomplete_carry
        pending[0]["decision"] = decision
        decision_entry = {"dispatch_id": pending[0]["dispatch_id"], **decision}
        if routing is not None:
            decision_entry["next_role"] = routing["next_role"]
        batch.setdefault("coordinator_decisions", []).append(decision_entry)
        if routing is not None:
            batch["required_next_role"] = NEXT_ACTION_DISPATCH_ROLE[
                routing["next_action"]
            ]
            batch["retry_candidate_required"] = (
                routing["next_action"] == "developer-retry"
            )
            batch["next_action"] = routing["next_action"]
        elif args.decision in {"accept", "override-warning"}:
            if report["role"] == "developer":
                if batch.get("base_rebase_required"):
                    # Pin the tip the accepted candidate was verified to contain; a batch blocked
                    # before the target was recorded falls back to the fetched tip.
                    target = batch.pop("rebase_target_commit", None)
                    if not isinstance(target, str):
                        target = _fetch_ref_tip(repo, _integration_ref(repo, batch))
                    batch["integration_base_commit"] = target
                    batch["base_rebase_required"] = False
                if dispatch.get("purpose", "work") == "work":
                    # The accepted candidate sits on a human-approved rebase target newer than
                    # the pinned base (issue #504): pin it, so review needs no rebase dispatch.
                    approved = rebase.approved_target(
                        repo,
                        root,
                        batch,
                        _candidate_commit(repo, report["commit_sha"]),
                    )
                    if approved is not None:
                        batch["integration_base_commit"] = approved
                if dispatch.get("purpose") == "publish":
                    batch.pop("next_action", None)
                    batch["state"] = "completed"
                else:
                    batch["next_action"] = "risk-assessment"
            elif report["role"] == resolver_state.RESOLVER_ROLE:
                # Narrow route: the resolution is assessed like a candidate, but its review is
                # waived and QA of the new pair still runs (see ``assess_risk``).
                batch["next_action"] = "risk-assessment"
                resolver_state.record_resolution(
                    ledger, repo, root, batch, report, config
                )
            elif report["role"] == "architect":
                # A superseding batch whose start commit needs a rebase starts with a
                # developer-retry (issue #506); every other batch with the initial developer.
                batch["next_action"] = supersede.developer_next_action(batch)
            elif report["role"] == "verification":
                batch["next_action"] = "risk-assessment"
            elif report["role"] == "code-review":
                batch["next_action"] = "qa"
            elif report["role"] == "qa":
                batch["next_action"] = "publish"
        if args.decision == "retry":
            # A retry that cannot be trusted to loop safely halts automatic dispatch creation for a human.
            _apply_attention(
                config,
                batch,
                _attention_findings(repo, root, config, batch, utils._now()),
                utils._now(),
            )
        abandoned: list[str] = []
        if args.decision in {"block", "fail", "abandon"}:
            # A terminal decision starts nothing: no next action is left behind to be picked up.
            batch.pop("next_action", None)
            batch.pop("required_next_role", None)
            batch["state"] = {
                "block": "blocked",
                "fail": "failed",
                "abandon": "abandoned",
            }[args.decision]
        elif batch.get("state") != "completed":
            batch["state"] = "awaiting-approval"
        if args.decision == "abandon":
            moment = utils._now()
            abandoned = _abandon_open_dispatches(ledger, root, batch, moment)
            batch["abandoned"] = {
                "approved_by": approval["approved_by"],
                "approved_at": approval["approved_at"],
                "abandoned_at": moment,
                "reason": abandon_reason,
                "open_dispatches": abandoned,
                "last_accepted": _last_accepted(repo, root, batch),
            }
        _safe_id(batch["batch_id"], "batch")
        _replace_record(
            ledger,
            BatchRecord.from_dict(batch),
            decision=_decision_audit(pending[0], decision, approver),
        )
        if args.decision == "abandon":
            _discard_batch_leftovers(repo, ledger, batch, abandoned)
    return batch


def _decision_audit(
    entry: JsonObject, decision: JsonObject, approver: JsonObject
) -> JsonObject:
    """The ``decision`` detail stored on the batch transition audit record (no I/O).

    ``route`` is the recorded recovery route, or ``None`` for a decision that routes nothing. The
    evidence references the immutable report by its ledger path and integrity hash. ``approver``
    is ``{"kind": "policy" | "human", "name": ...}``, set by the path that approved the decision.
    """
    routing = decision.get("routing")
    return {
        "dispatch_id": entry["dispatch_id"],
        "decision": decision["decision"],
        "route": _require_route(routing["route"])
        if isinstance(routing, dict)
        else None,
        "evidence": {
            "dispatch_id": entry["dispatch_id"],
            "report": entry["report"],
            "report_sha256": entry["report_sha256"],
        },
        "approver": approver,
        "approved_at": decision["approved_at"],
    }


def _decide_retry_route(
    repo: Path,
    root: Path,
    batch: JsonObject,
    dispatch: JsonObject,
    report: JsonObject,
    args: argparse.Namespace,
) -> JsonObject:
    """Where the batch goes after a ``retry`` decision on the pending report."""
    forced = getattr(args, "retry_role", None)
    if forced not in {None, "developer"}:
        raise CoordinatorError(
            "--retry-role only accepts developer",
            remedy="omit --retry-role to let the coordinator route the retry, or pass developer to force a developer retry",
        )
    narrowed = bool(getattr(args, "narrowed", False))
    if narrowed and forced is not None:
        raise CoordinatorError(
            "--narrowed re-runs the same stage, so it cannot force a developer retry",
            remedy="drop --retry-role to retry the stage on its incomplete items only, or drop "
            "--narrowed to force a developer retry",
        )
    try:
        current_candidate: str | None = _latest_developer_candidate(repo, root, batch)
    except CoordinatorError:
        current_candidate = None
    stage = _reporting_stage(dispatch, report)
    explicit_category = getattr(args, "reason_category", None)
    # A narrowed retry is routed by its incomplete items alone, so no classifier is asked.
    hint = (
        _classifier_hint(core_config._config(repo), dispatch, stage, report)
        if explicit_category is None and not narrowed
        else None
    )
    routing = _retry_routing(
        stage,
        report,
        dispatch_candidate=dispatch.get("candidate_commit"),
        current_candidate=current_candidate,
        explicit_category=hint.category if hint is not None else explicit_category,
        pressure_recorded=any(
            item.get("dispatch_id") == dispatch["dispatch_id"]
            and item.get("level") == "critical"
            for item in batch.get("context_pressure", [])
        ),
        narrowed=narrowed,
    )
    if narrowed:
        # The ids the new brief carries the items under; the items stay in the immutable report.
        routing = {
            **routing,
            "carried_item_ids": carried_items.next_incomplete_item_ids(
                batch, len(report["incomplete_items"])
            ),
        }
    if (
        stage == "developer"
        and routing["reason_category"] in OPERATIONAL_REASON_CATEGORIES
        and report.get("outcome") == "blocked"
        and report.get("changed_files")
    ):
        candidate = _candidate_commit(repo, report["commit_sha"])
        routing = {
            **routing,
            "route": "verification",
            "next_role": "verification",
            "next_action": "verification",
            "candidate_commit": candidate,
            "rationale": (
                f"{routing['rationale']} The blocked developer candidate is registered "
                "append-only and must pass a new read-only verification dispatch before risk assessment."
            ),
        }
    if stage == "developer" and routing["route"] == "tooling-retry":
        # The developer restarts from its last commit: record it, so the audit and the tooling
        # streak name the candidate the retry continues.
        routing = {
            **routing,
            "candidate_commit": _candidate_commit(repo, report["commit_sha"]),
        }
    if stage == "verification" and routing["route"] in {
        "tooling-retry",
        "bypass-rerun",
        "narrowed-retry",
    }:
        # Verification re-runs on the registered candidate its brief pinned; no accepted
        # candidate names it, so record the pin for the audit (and the tooling streak).
        routing = {**routing, "candidate_commit": dispatch.get("candidate_commit")}
    if hint is not None:
        routing["classifier_hint"] = {"category": hint.category, "basis": hint.basis}
    if forced == "developer" and routing["next_action"] != "developer-retry":
        routing = {
            **routing,
            "route": "developer-retry",
            "next_role": "developer",
            "next_action": "developer-retry",
            "rationale": f"{routing['rationale']} The approver forced a developer retry with --retry-role developer.",
        }
    if routing["next_action"] == "developer-retry":
        # The closed list the developer-retry brief carries (issue #503), recorded with the route.
        # A developer-retry with items is a fix-forward; a tooling-retry keeps its own route.
        entry = next(
            item
            for item in batch.get("dispatches", [])
            if item.get("dispatch_id") == dispatch["dispatch_id"]
        )
        item_ids = carried_items.section_item_ids(
            carried_items.retry_section(root, batch, entry)
        )
        routing = {**routing, "retry_item_ids": item_ids}
        if item_ids and routing["route"] == "developer-retry":
            routing = {
                **routing,
                "route": "fix-forward",
                "rationale": f"{routing['rationale']} It is a fix-forward: new commits on top of "
                f"the candidate close the carried items {', '.join(item_ids)} without rewriting "
                "history.",
            }
    if routing["route"] in {"developer-retry", "fix-forward"}:
        routing = _rebase_routing(
            repo, batch, routing, stage, report, current_candidate
        )
    _require_route(routing["route"])
    return routing


def _rebase_routing(
    repo: Path,
    batch: JsonObject,
    routing: JsonObject,
    stage: str,
    report: JsonObject,
    current_candidate: str | None,
) -> JsonObject:
    """A developer-retry whose integration base moved ahead becomes ``rebase-fix-forward`` (issue
    #504): the routing record proposes the fetched tip as the rebase target. The candidate the
    retry continues is a retried developer report's own, else the latest accepted one."""
    snapshot = (
        _candidate_commit(repo, report["commit_sha"])
        if stage == "developer"
        else current_candidate
    ) or batch["base_commit"]
    target = rebase.propose_target(repo, batch, snapshot)
    if target is None:
        return routing
    base = batch["integration_base_commit"]
    return {
        **routing,
        "route": "rebase-fix-forward",
        "rebase_target_commit": target,
        "integration_base_commit": base,
        "rationale": f"{routing['rationale']} It is a rebase-fix-forward: "
        f"origin/{_integration_ref(repo, batch)} moved from {base} to {target}, so the "
        f"developer-retry rebases the candidate onto {target} and fixes on top of it in the same "
        "dispatch, which needs an explicit approval.",
    }


def _classifier_hint(
    config: JsonObject,
    dispatch: JsonObject,
    stage: str,
    report: JsonObject,
) -> extensions.ReasonHint | None:
    """Ask the configured retry-reason classifier for a hint from structured runtime facts.

    The hint is only a proposed category: it goes through the same routing guards as one an
    approver names, so a plugin can never route around a finding, a failed check or a moved candidate.
    """
    names = _extension_names(config)
    try:
        facts = extensions.ClassificationFacts(
            stage=stage,
            outcome=str(report.get("outcome")),
            transport=extensions.transport_health(names["transport_health"]).probe(
                dispatch["dispatch_id"]
            ),
            verification=extensions.verification_environment_health(
                names["verification_environment_health"]
            ).probe(dispatch["dispatch_id"]),
        )
        hint = extensions.retry_reason_classifier(
            names["retry_reason_classifier"]
        ).classify(facts)
    except extensions.ExtensionError as exc:
        raise CoordinatorError(exc.message, remedy=exc.remedy) from exc
    if hint is not None and hint.category not in RETRY_REASON_CATEGORIES:
        raise CoordinatorError(
            f"the retry reason classifier proposed an unknown category {hint.category!r}",
            remedy=f"fix the classifier to return one of: {', '.join(RETRY_REASON_CATEGORIES)}",
        )
    return hint
