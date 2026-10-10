"""``approval_policy: auto``: the decision ``batch auto-decide`` takes on a pending report.

The coordinator computes every path decision itself (issue #643). The table is a pure function of
ledger facts and of the bounded inputs the session passes because they need judgement: a findings
file, the architect's commit plan, a bug ticket for a blocking tool, and the fact of a block
bypass. ``category`` picks accept or retry and the retry's reason category; ``finish`` maps the one
routing record ``_decide_retry_route`` computes (the same record ``route_preview`` shows) to a
retry, a stop or a refusal. The policy never chooses override-warning, block, fail or abandon, and
every decision it takes goes through ``decide_batch`` with its validation unchanged.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from harness.orchestration.core import utils
from harness.orchestration.core.config import _continuation_policy, _reject_sensitive
from harness.orchestration.core.constants import (
    AUTO_STOP_REASONS,
    BLOCK_BYPASS_REASON_CATEGORY,
    BLOCK_BYPASS_STAGES,
    INCOMPLETE_ITEM_TARGET_ROLES,
    TERMINAL_BATCH_STATES,
)
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _non_empty,
    _repo,
    _safe_id,
)
from harness.orchestration.core.workspace import _validate_harness_runtime_snapshot
from harness.orchestration.ledger.ledger_ops import (
    _load_batch,
    _load_dispatch,
    _load_dispatch_status,
    _replace_record,
    _state_root,
)
from harness.orchestration.ledger.lifecycle import BatchRecord, LifecycleLedger
from harness.orchestration.workflow import approval as approvals
from harness.orchestration.workflow import carried_items
from harness.orchestration.workflow import commit_plan as plan_rules
from harness.orchestration.workflow import decisions, resolver_state
from harness.orchestration.workflow.attention import _attention_findings
from harness.orchestration.workflow.history import (
    _latest_developer_candidate,
    _pending_report,
    _settled,
)
from harness.orchestration.workflow.reports import (
    _continuation_counts,
    report_scope_warnings,
)

# An attention reason is a stop of the closed list: the automatic path never resolves attention.
ATTENTION_STOPS = {
    "stale-dispatch": ("integrity-failure", "stale"),
    "stale-evidence": ("integrity-failure", "stale"),
    "retry-queued-too-long": ("integrity-failure", "stale"),
    "infrastructure-retry-repeated": (
        "budget-exhausted",
        "attention_policy.max_infrastructure_retries",
    ),
    "tooling-retry-repeated": ("budget-exhausted", "tooling-retry-repeated"),
    "unknown-reason": ("no-automatic-route", "unknown-reason"),
}
# Detection order: an untrustworthy ledger or runtime first, then a spent budget, then a dead end.
# Spelled out: the key order of ``AUTO_STOP_REASONS`` lists the budget first.
STOP_ORDER = ("integrity-failure", "budget-exhausted", "no-automatic-route")
CLEAN_BASIS = (
    "the report is clean: completed, no blockers, every check passed, and nothing is left "
    "uncovered, unclosed, open or outside the approved scope"
)


@dataclass(frozen=True)
class Inputs:
    """What the session passes to ``batch auto-decide``: only facts that need judgement."""

    findings_file: str | None = None
    commit_plan_file: str | None = None
    bug_ticket: str | None = None
    block_bypass: bool = False
    note: str | None = None


@dataclass(frozen=True)
class Choice:
    """The table's first half: accept or retry, and how."""

    decision: str
    reason_category: str | None
    narrowed: bool
    carry_incomplete: bool
    basis: str


@dataclass(frozen=True)
class Stop:
    """One condition of the closed stop list, with the facts that showed it."""

    category: str
    reason: str
    evidence: JsonObject

    def __post_init__(self) -> None:
        if self.reason not in AUTO_STOP_REASONS.get(self.category, ()):
            raise CoordinatorError(
                f"auto stop {self.category}/{self.reason} is not on the closed stop list",
                remedy="name a stop from AUTO_STOP_REASONS",
            )


@dataclass(frozen=True)
class Resolution:
    """What ``batch auto-decide`` does with the pending report: decide or stop."""

    fields: JsonObject = field(default_factory=dict)
    choice: Choice | None = None
    stop: Stop | None = None


def inputs_from(args: argparse.Namespace) -> Inputs:
    """The validated session inputs of ``batch auto-decide``."""
    note = args.note.strip() if _non_empty(getattr(args, "note", None)) else None
    if note is not None and note.lower() == "none":
        note = None
    block_bypass = bool(getattr(args, "block_bypass", False))
    if block_bypass and note is None:
        raise CoordinatorError(
            "--block-bypass requires a --note naming the violation",
            remedy="pass --note (other than 'none') naming the hook or tool block the role "
            "worked around and how",
        )
    ticket = getattr(args, "bug_ticket", None)
    ticket = ticket.strip() if _non_empty(ticket) else None
    _reject_sensitive({"note": note, "bug_ticket": ticket}, "auto-decide input")
    return Inputs(
        findings_file=getattr(args, "findings_file", None),
        commit_plan_file=getattr(args, "commit_plan_file", None),
        bug_ticket=ticket,
        block_bypass=block_bypass,
        note=note,
    )


def _own_incomplete(stage: str, report: JsonObject) -> list[JsonObject]:
    return [
        item
        for item in report.get("incomplete_items") or []
        if isinstance(item, dict) and item.get("target_role") == stage
    ]


def accept_obstacles(
    stage: str,
    report: JsonObject,
    dispatch: JsonObject,
    *,
    scope_warnings: list[str],
    block_bypass: bool,
) -> list[str]:
    """Why the policy cannot accept this report; none means it accepts (no I/O).

    Every report content ``batch decide`` refuses for a plain accept is an obstacle here; so are
    a review axis that names blockers and a block bypass the session found: the policy accepts
    only what is clean.
    """
    obstacles = []
    if report.get("outcome") != "completed":
        obstacles.append(f"outcome is {report.get('outcome')}")
    if str(report.get("blockers", "")).strip().lower() != "none":
        obstacles.append("the report names blockers")
    for check in report.get("checks_run", []):
        result = check.get("result") if isinstance(check, dict) else None
        if result != "pass" and not (
            report.get("role") == "architect"
            and result == "not_run_architect_read_only"
        ):
            obstacles.append(f"a check did not pass ({result})")
            break
    if plan_rules.not_covered(report):
        obstacles.append("definition-of-done items are not covered")
    if carried_items.carried_gap(report, dispatch):
        obstacles.append("carried items are not closed")
    if scope_warnings:
        obstacles.append("files changed outside the approved scope")
    review = report.get("review")
    if isinstance(review, dict) and any(
        isinstance(review.get(axis), dict)
        and (
            review[axis].get("findings")
            or review[axis].get("severity") in {"warning", "blocker"}
        )
        for axis in ("standards", "spec")
    ):
        obstacles.append("the review has findings or a warning/blocker severity")
    if isinstance(review, dict) and any(
        isinstance(review.get(axis), dict)
        and str(review[axis].get("blockers", "none")).strip().lower() != "none"
        for axis in ("standards", "spec")
    ):
        # A clean severity does not hide blockers an axis names (as for the policy auto-accept).
        obstacles.append("a review axis names blockers")
    if _own_incomplete(stage, report):
        obstacles.append(f"incomplete items target the {stage} role itself")
    if block_bypass:
        obstacles.append("the role worked around a hook or tool block")
    return obstacles


def category(
    stage: str,
    report: JsonObject,
    dispatch: JsonObject,
    *,
    scope_warnings: list[str],
    block_bypass: bool,
    candidate_moved: bool,
    pressure_recorded: bool,
) -> Choice:
    """The first half of the auto decision table (no I/O)."""
    obstacles = accept_obstacles(
        stage,
        report,
        dispatch,
        scope_warnings=scope_warnings,
        block_bypass=block_bypass,
    )
    if not obstacles:
        # Incomplete items left here all target later roles: their briefs carry them.
        return Choice(
            "accept", None, False, bool(report.get("incomplete_items")), CLEAN_BASIS
        )
    found = "; ".join(obstacles)
    if block_bypass:
        reason = (
            BLOCK_BYPASS_REASON_CATEGORY if stage in BLOCK_BYPASS_STAGES else "code"
        )
        return Choice(
            "retry", reason, False, False, f"{found}; its report is no evidence"
        )
    if (
        _own_incomplete(stage, report)
        and stage in INCOMPLETE_ITEM_TARGET_ROLES
        and decisions._retry_evidence(report, candidate_moved) is None
    ):
        return Choice(
            "retry",
            None,
            True,
            False,
            f"{found}; nothing else needs a change, so the same role retries those items",
        )
    if (
        stage == "developer"
        and report.get("outcome") == "completed"
        and (
            plan_rules.not_covered(report)
            or scope_warnings
            or carried_items.carried_gap(report, dispatch)
        )
    ):
        return Choice(
            "retry",
            "requirements",
            False,
            False,
            f"{found}; the developer must finish the requirements",
        )
    if pressure_recorded and report.get("outcome") == "blocked":
        return Choice(
            "retry",
            "context-pressure",
            False,
            False,
            f"{found}; a critical context_pressure observation exists for the dispatch",
        )
    return Choice(
        "retry", None, False, False, f"{found}; the structured report data routes it"
    )


def finish(
    routing: JsonObject | None,
    refusal: CoordinatorError | None,
    *,
    budget_exhausted: bool,
    bug_ticket: str | None,
) -> Stop | None:
    """The second half of the table: ``None`` retries with ``routing`` (no I/O).

    A route the coordinator refuses and an unknown reason are dead ends a human resolves; an
    exhausted developer-retry budget stops too. A tooling-retry without a bug ticket for the
    blocking tool is refused instead: the session can create one and run the command again.
    """
    if refusal is not None or routing is None:
        return Stop(
            "no-automatic-route",
            "abandon-dead-end",
            {
                "refused": refusal.message if refusal else None,
                "remedy": refusal.remedy if refusal else None,
            },
        )
    if routing.get("reason_category") == "unknown":
        return Stop("no-automatic-route", "unknown-reason", {"route_preview": routing})
    if routing["route"] == "tooling-retry" and bug_ticket is None:
        raise CoordinatorError(
            "a tooling-retry under approval_policy auto needs a bug ticket for the blocking tool",
            remedy="create or reuse a bug ticket for the tool named in the tooling_blocker "
            "through the tracker CLI, then run batch auto-decide again with --bug-ticket",
        )
    if (
        routing["next_action"] == "developer-retry"
        and routing["route"] != "tooling-retry"
        and budget_exhausted
    ):
        return Stop(
            "budget-exhausted",
            "retry_policy.max_developer_retries",
            {"route_preview": routing},
        )
    return None


def attention_stops(batch: JsonObject, findings: list[dict[str, str]]) -> list[Stop]:
    """The stops the batch's attention state shows: the persisted flag and every finding not
    acknowledged by a human (no I/O)."""
    acknowledged = set(batch.get("attention_acknowledged", []))
    keys = {
        finding["reason"]: finding["key"]
        for finding in findings
        if finding["key"] not in acknowledged
    }
    if batch.get("needs_attention") is True:
        keys.setdefault(str(batch.get("attention_reason")), "needs_attention")
    stops = []
    for reason, key in keys.items():
        if reason in ATTENTION_STOPS:
            category_name, stop_reason = ATTENTION_STOPS[reason]
            stops.append(
                Stop(category_name, stop_reason, {"attention": reason, "key": key})
            )
    return stops


def _status_stops(root: Path, batch: JsonObject) -> list[Stop]:
    """An open dispatch whose worker ran another model or another worktree than its brief
    approved; a dispatch a human already settled (abandoned on resume) is no stop."""
    stops = []
    for entry in batch.get("dispatches", []):
        if _settled(entry):
            continue
        try:
            status = _load_dispatch_status(root, entry["dispatch_id"])
        except CoordinatorError:
            continue
        model = status.get("model_self_report")
        failure = status.get("runtime_failure", {})
        if (
            failure.get("kind") == "runtime-unavailable"
            and failure.get("worker_started") is False
        ):
            stops.append(
                Stop(
                    "integrity-failure",
                    "runtime-unavailable",
                    {"dispatch_id": entry["dispatch_id"], "runtime_failure": failure},
                )
            )
        if isinstance(model, dict) and model.get("match") is False:
            stops.append(
                Stop(
                    "integrity-failure",
                    "model-mismatch",
                    {
                        "dispatch_id": entry["dispatch_id"],
                        "reported_model": model.get("reported_model"),
                        "expected_model": model.get("expected_model"),
                    },
                )
            )
        worktree = status.get("worktree_attestation")
        if isinstance(worktree, dict) and worktree.get("match") is False:
            stops.append(
                Stop(
                    "integrity-failure",
                    "worktree-mismatch",
                    {
                        "dispatch_id": entry["dispatch_id"],
                        "error": worktree.get("error"),
                    },
                )
            )
    return stops


def _continuation_stops(config: JsonObject, batch: JsonObject) -> list[Stop]:
    """A checkpointed or rate-limited dispatch whose continuation budget is spent."""
    policy = _continuation_policy(config)
    stops = []
    for entry in batch.get("dispatches", []):
        if entry.get("state") not in {"checkpointed", "rate_limited"}:
            continue
        spent, automatic = _continuation_counts(batch, entry["dispatch_id"])
        evidence = {
            "dispatch_id": entry["dispatch_id"],
            "continuations": spent,
            "rate_limit_resumes": automatic,
        }
        if spent >= policy["max_continuations"]:
            stops.append(
                Stop(
                    "budget-exhausted",
                    "continuation_policy.max_continuations",
                    evidence,
                )
            )
        elif (
            entry.get("state") == "rate_limited"
            and automatic >= policy["max_rate_limit_resumes"]
        ):
            stops.append(
                Stop(
                    "budget-exhausted",
                    "continuation_policy.max_rate_limit_resumes",
                    evidence,
                )
            )
    return stops


def fact_stops(
    repo: Path, root: Path, config: JsonObject, batch: JsonObject, moment: str
) -> list[Stop]:
    """Every stop the ledger facts of a validated batch show, in detection order."""
    stops: list[Stop] = []
    try:
        _validate_harness_runtime_snapshot(repo, batch)
    except CoordinatorError as exc:
        stops.append(
            Stop(
                "integrity-failure",
                "harness-snapshot-changed",
                {"refused": exc.message},
            )
        )
    stops.extend(_status_stops(root, batch))
    findings = (
        []
        if batch.get("state") in TERMINAL_BATCH_STATES
        else _attention_findings(repo, root, config, batch, moment)
    )
    stops.extend(attention_stops(batch, findings))
    stops.extend(_continuation_stops(config, batch))
    if batch.get("state") in {"blocked", "failed"}:
        stops.append(
            Stop("no-automatic-route", "abandon-dead-end", {"state": batch["state"]})
        )
    if batch.get("state") == "abandoned":
        stops.append(
            Stop(
                "no-automatic-route",
                "supersede-dead-end",
                {"state": "abandoned", "next": "batch create --supersedes, by a human"},
            )
        )
    return sorted(stops, key=lambda stop: STOP_ORDER.index(stop.category))


def validation_stop(repo: Path, batch: JsonObject, refusal: CoordinatorError) -> Stop:
    """The stop a failed revalidation of the pending report or its dispatch shows."""
    try:
        _validate_harness_runtime_snapshot(repo, batch)
    except CoordinatorError as exc:
        return Stop(
            "integrity-failure", "harness-snapshot-changed", {"refused": exc.message}
        )
    return Stop(
        "integrity-failure",
        "deterministic-gate-failed",
        {"gate": "report-revalidation", "refused": refusal.message},
    )


def ledger_stop(refusal: CoordinatorError) -> Stop:
    """A ledger that fails validation: the stop is shown and never recorded in that ledger."""
    return Stop(
        "integrity-failure", "ledger-validation-failed", {"refused": refusal.message}
    )


def ledger_stop_error(refusal: CoordinatorError) -> CoordinatorError:
    return CoordinatorError(
        "the automatic path stops (integrity-failure: ledger-validation-failed); the stop is "
        f"not recorded because the ledger failed validation: {refusal.message}",
        remedy=f"a human inspects the ledger before any further step: {refusal.remedy}",
    )


def record_stop(
    repo: Path,
    root: Path,
    config: JsonObject,
    batch: JsonObject,
    stop: Stop,
    *,
    detected_by: str,
    moment: str,
) -> JsonObject:
    """Record ``stop`` and the final report on the batch once: after it, every step needs a
    human. Only the in-memory batch changes; the caller writes it."""
    from harness.orchestration.workflow import auto_report

    stop_record = approvals.sealed(
        {
            "category": stop.category,
            "reason": stop.reason,
            "detected_at": moment,
            "detected_by": detected_by,
            "evidence": stop.evidence,
        }
    )
    if "auto_stop" not in batch:
        batch["auto_stop"] = stop_record
    from harness.orchestration.workflow import recovery

    recovery.pause(root, batch, stop_record, moment)
    auto_report.record(repo, root, config, batch, moment)
    return cast(JsonObject, batch["auto_stop"])


def persist_stop(
    ledger: LifecycleLedger,
    repo: Path,
    root: Path,
    config: JsonObject,
    batch: JsonObject,
    stop: Stop,
    *,
    detected_by: str,
) -> JsonObject:
    """Record ``stop`` with the final report and write the batch; the caller holds the lock."""
    record_stop(
        repo, root, config, batch, stop, detected_by=detected_by, moment=utils._now()
    )
    _safe_id(batch["batch_id"], "batch")
    from harness.orchestration.workflow.recovery import event_audit

    _replace_record(
        ledger,
        BatchRecord.from_dict(batch),
        decision=event_audit(batch["recovery_events"][-1]),
    )
    return batch


def pending_stop(
    repo: Path, root: Path, config: JsonObject, batch: JsonObject
) -> Stop | None:
    """The stop the decision table shows for the pending report without session inputs."""
    pending = [
        item
        for item in batch.get("dispatches", [])
        if item.get("state") == "reported" and "decision" not in item
    ]
    if len(pending) != 1:
        return None
    report = _pending_report(root, batch, pending[0])
    dispatch = _load_dispatch(root, pending[0]["dispatch_id"])
    try:
        decisions._revalidate_pending(repo, root, config, batch, dispatch, report)
    except CoordinatorError as exc:
        return validation_stop(repo, batch, exc)
    try:
        return resolve(repo, root, config, batch, dispatch, report, Inputs()).stop
    except CoordinatorError:
        # A refusal the session can fix (a bug ticket, a resolver for a human) is no stop.
        return None


def derive_stop(
    repo: Path, root: Path, config: JsonObject, batch: JsonObject
) -> Stop | None:
    """The first stop of the closed list a validated batch shows now: its ledger facts, then
    the decision table on its pending report."""
    stops = fact_stops(repo, root, config, batch, utils._now())
    return stops[0] if stops else pending_stop(repo, root, config, batch)


def _note(choice: Choice, inputs: Inputs) -> str:
    note = f"policy:auto {choice.decision}: {choice.basis}"
    return f"{note}; {inputs.note}" if inputs.note else note


def resolve(
    repo: Path,
    root: Path,
    config: JsonObject,
    batch: JsonObject,
    dispatch: JsonObject,
    report: JsonObject,
    inputs: Inputs,
) -> Resolution:
    """The auto decision on the pending report, computed under the ``decide_batch`` lock."""
    if not approvals.auto_configured(config, batch):
        raise CoordinatorError(
            "batch auto-decide applies only to a batch planned under approval_policy auto in a "
            "project that still chooses it",
            remedy="decide the report with batch decide and --approved-by",
        )
    if "auto_stop" in batch or batch.get("manual_recovery"):
        raise CoordinatorError(
            "the automatic path of this batch stopped, so a human decides every later step",
            remedy="show batch auto-report to a human; decide with batch decide and --approved-by",
        )
    if dispatch.get("role") == resolver_state.RESOLVER_ROLE:
        raise CoordinatorError(
            "a conflict-resolver report is outside the automatic path",
            remedy="a human decides it with batch decide and --approved-by",
        )
    stops = fact_stops(repo, root, config, batch, utils._now())
    if stops:
        return Resolution(stop=stops[0])
    stage = decisions._reporting_stage(dispatch, report)
    scope = (
        report_scope_warnings(report, dispatch)
        if report.get("role") != "architect"
        else []
    )
    try:
        current: str | None = _latest_developer_candidate(repo, root, batch)
    except CoordinatorError:
        current = None
    choice = category(
        stage,
        report,
        dispatch,
        scope_warnings=scope,
        block_bypass=inputs.block_bypass,
        candidate_moved=stage in {"code-review", "qa"}
        and dispatch.get("candidate_commit") != current,
        pressure_recorded=any(
            item.get("dispatch_id") == dispatch["dispatch_id"]
            and item.get("level") == "critical"
            for item in batch.get("context_pressure", [])
        ),
    )
    fields: JsonObject = {
        "decision": choice.decision,
        "note": _note(choice, inputs),
        "reason": None,
        "reason_category": choice.reason_category,
        "retry_role": None,
        "narrowed": choice.narrowed,
        "carry_incomplete": choice.carry_incomplete,
        "findings_file": inputs.findings_file,
        "commit_plan_file": inputs.commit_plan_file,
    }
    if choice.decision == "accept":
        if inputs.commit_plan_file is not None and report.get("role") == "architect":
            # A plan file that cannot be read is a session input error: the command refuses and
            # records nothing. Only the plan in the file goes through the deterministic gate.
            document = decisions._commit_plan_document(repo, inputs.commit_plan_file)
            try:
                plan = decisions._pin_commit_plan(batch, document)
            except CoordinatorError as exc:
                return Resolution(
                    stop=Stop(
                        "integrity-failure",
                        "deterministic-gate-failed",
                        {"gate": "commit-plan", "refused": exc.message},
                    )
                )
            outside = plan_rules.paths_outside_scope(plan, batch.get("allowed_paths"))
            if outside:
                return Resolution(
                    stop=Stop(
                        "integrity-failure",
                        "deterministic-gate-failed",
                        {"gate": "commit-plan", "paths_outside_scope": outside},
                    )
                )
        return Resolution(fields=fields, choice=choice)
    refusal: CoordinatorError | None = None
    routing: JsonObject | None = None
    try:
        routing = decisions._decide_retry_route(
            repo,
            root,
            batch,
            dispatch,
            report,
            argparse.Namespace(
                reason_category=choice.reason_category,
                narrowed=choice.narrowed,
                retry_role=None,
            ),
        )
    except CoordinatorError as exc:
        refusal = exc
    stop = finish(
        routing,
        refusal,
        budget_exhausted=decisions._developer_retry_budget_exhausted(config, batch),
        bug_ticket=inputs.bug_ticket,
    )
    if stop is not None:
        return Resolution(stop=stop)
    return Resolution(fields={**fields, "_auto_routing": routing}, choice=choice)


def accepted_risks(
    batch: JsonObject, dispatch: JsonObject, report: JsonObject
) -> list[JsonObject]:
    """The risks an accept takes on: the report's own, its triggers, the candidate's matched
    triggers and the review axes' risks (no I/O)."""
    risks: list[JsonObject] = []
    if str(report.get("risks", "none")).strip().lower() != "none":
        risks.append({"source": "report", "risks": report["risks"]})
    if report.get("risk_triggers"):
        risks.append({"source": "risk_triggers", "triggers": report["risk_triggers"]})
    candidate = dispatch.get("candidate_commit")
    matched = sorted(
        {
            trigger
            for item in batch.get("risk_assessments", [])
            if isinstance(candidate, str) and item.get("candidate_commit") == candidate
            for trigger in item.get("matched_triggers") or []
        }
    )
    if matched:
        risks.append({"source": "risk-assessment", "triggers": matched})
    review = report.get("review")
    for axis in ("standards", "spec"):
        evidence = review.get(axis) if isinstance(review, dict) else None
        if (
            isinstance(evidence, dict)
            and str(evidence.get("risks", "none")).strip().lower() != "none"
        ):
            risks.append({"source": f"review-{axis}", "risks": evidence["risks"]})
    return risks


def record_decision(
    batch: JsonObject,
    entry: JsonObject,
    dispatch: JsonObject,
    report: JsonObject,
    decision: JsonObject,
    choice: Choice | None,
    inputs: Inputs | None,
) -> JsonObject:
    """Record a ``policy:auto`` decision with its route, reason and evidence."""
    routing = decision.get("routing")
    route = routing if isinstance(routing, dict) else None
    item_ids = (
        (route.get("carried_item_ids") or route.get("retry_item_ids"))
        if route
        else None
    )
    return approvals.record_auto(
        batch,
        kind="decision",
        dispatch_id=entry["dispatch_id"],
        rationale=decision["note"],
        evidence={
            "decision": decision["decision"],
            "report_sha256": entry["report_sha256"],
            "route_preview": route,
            "reason_category": route.get("reason_category") if route else None,
            "basis": choice.basis if choice is not None else CLEAN_BASIS,
            "accepted_risks": accepted_risks(batch, dispatch, report)
            if decision["decision"] == "accept"
            else [],
            "commit_plan_sha256": decision.get("commit_plan_sha256"),
            "bug_ticket": inputs.bug_ticket if inputs is not None else None,
            "carried_item_ids": list(item_ids or []),
        },
        moment=decision["approved_at"],
    )


def auto_decide(args: argparse.Namespace) -> JsonObject:
    """``batch auto-decide``: the policy decision on the batch's pending report, or the stop of
    the closed list it shows."""
    inputs = inputs_from(args)
    repo = _repo(args)
    before = len(
        _load_batch(_state_root(args, repo), args.batch).get("auto_decisions", [])
    )
    batch = decisions.decide_batch(
        argparse.Namespace(
            **{
                **vars(args),
                "decision": None,
                "approved_by": None,
                "approved_at": None,
                "note": None,
                "reason": None,
                "reason_category": None,
                "retry_role": None,
                "narrowed": False,
                "carry_incomplete": False,
                "findings_file": None,
                "commit_plan_file": None,
                "_policy_auto": inputs,
            }
        )
    )
    record = next(
        (
            item
            for item in batch.get("auto_decisions", [])[before:]
            if item["kind"] == "decision"
        ),
        None,
    )
    route = record["evidence"]["route_preview"] if record is not None else None
    stop = batch.get("auto_stop")
    return {
        "batch_id": batch["batch_id"],
        "outcome": "stopped" if stop is not None else "decided",
        "dispatch_id": record["dispatch_id"] if record is not None else None,
        "decision": record["evidence"]["decision"] if record is not None else None,
        "route": route.get("route") if isinstance(route, dict) else None,
        "auto_decision": record,
        "stop": stop,
        "batch_state": batch["state"],
        "next_action": "show the final auto report (batch auto-report) to a human; every "
        "later step of this batch needs --approved-by"
        if stop is not None
        else batch.get("next_action"),
    }
