"""Completing the policy chain a recorded report starts.

When the approval policy accepts a report, `report submit` continues after recording it: the policy
decision, the risk assessment of an accepted candidate and the next dispatch.  Each step is an
ordinary coordinator command that takes the ledger lock and re-validates on its own, so the chain can
stop after the report is already recorded.  Every step here is derived from the ledger, so the same
chain runs again through `report complete` without re-recording the report or repeating a decision,
an assessment or a dispatch that already exists.
"""

from __future__ import annotations

import argparse
import shlex
from pathlib import Path

from harness.orchestration.core.git_utils import _candidate_commit
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _read_object,
    _repo,
)
from harness.orchestration.ledger.ledger_ops import (
    _ledger_lock,
    _load_batch,
    _load_dispatch,
    _load_dispatch_status,
    _records_root,
    _state_root,
)
from harness.orchestration.ledger.lifecycle import LifecycleLedger
from harness.orchestration.workflow.decisions import decide_batch
from harness.orchestration.workflow.dispatch import create_dispatch
from harness.orchestration.workflow.history import _require_route
from harness.orchestration.workflow.risk import assess_risk

POLICY_CHAIN_STEPS = ("policy-decide", "risk-assess", "next-dispatch")
COMPLETION_ROUTE = "report-completion"
# An accepted report of these roles moves its batch to `risk-assessment` of the reported candidate.
RISK_ASSESSED_ROLES = {"developer", "verification"}


def _completion_command(dispatch_id: str, state_dir: str | None) -> str:
    command = (
        "python .harness/orchestration/coordinator.py --repo . report complete "
        f"--dispatch {dispatch_id}"
    )
    if state_dir:
        command += f" --state-dir {shlex.quote(state_dir)}"
    return command


def _chain_state(root: Path, dispatch_id: str) -> JsonObject:
    """The ledger facts the chain decides from, read under the ledger lock."""
    with _ledger_lock(LifecycleLedger(root)):
        dispatch = _load_dispatch(root, dispatch_id)
        status = _load_dispatch_status(root, dispatch_id)
        batch = _load_batch(root, dispatch["batch_id"])
        entries = batch.get("dispatches", [])
        position = next(
            (
                index
                for index, item in enumerate(entries)
                if item.get("dispatch_id") == dispatch_id
            ),
            None,
        )
        entry = entries[position] if position is not None else {}
        if entry.get("state") != "reported" or not isinstance(entry.get("report"), str):
            raise CoordinatorError(
                f"dispatch {dispatch_id} has no recorded completion report",
                remedy="run report complete only for a dispatch whose report submit recorded "
                "its report",
            )
        report_path = _records_root(root) / entry["report"]
        report = _read_object(report_path, "completion report")
    return {
        "dispatch": dispatch,
        "status": status,
        "batch": batch,
        "entry": entry,
        "following": entries[position + 1 :] if position is not None else [],
        "report": report,
        "report_path": str(report_path),
    }


def _clean_assessment(repo: Path, batch: JsonObject, report: JsonObject) -> bool:
    """Whether the latest risk assessment of the reported candidate matched no trigger."""
    candidate = _candidate_commit(repo, report["commit_sha"])
    assessments = [
        item
        for item in batch.get("risk_assessments", [])
        if item.get("candidate_commit") == candidate
    ]
    return bool(assessments) and not assessments[-1].get("matched_triggers")


def _run_policy_chain(
    repo: Path, state_dir: str | None, dispatch_id: str
) -> JsonObject:
    """Run every step of a recorded report's policy chain that is still pending.

    A step already recorded in the ledger is `already-done`, a step the report's policy does not
    call for is `not-applicable`, and the first step that fails stops the chain (`failed`, the rest
    `not-run`).  Only the policy recorded at `report submit` is replayed; a report submit left for
    a human decision has no step to run."""
    root = _state_root(argparse.Namespace(state_dir=state_dir), repo)
    steps: JsonObject = {step: "not-run" for step in POLICY_CHAIN_STEPS}
    result: JsonObject = {
        "report": None,
        "report_sha256": None,
        "auto_accept_policy": None,
        "auto_accepted": False,
        "next_action": None,
        "next_dispatch_id": None,
        "steps": steps,
        "failed_step": None,
        "error": None,
    }
    step = POLICY_CHAIN_STEPS[0]
    try:
        state = _chain_state(root, dispatch_id)
        result["report"] = state["report_path"]
        result["report_sha256"] = state["entry"].get("report_sha256")
        policy = state["status"].get("auto_accept_policy")
        result["auto_accept_policy"] = policy
        if policy is None:
            steps.update(dict.fromkeys(POLICY_CHAIN_STEPS, "not-applicable"))
            result["next_action"] = state["batch"].get("next_action")
            return result
        batch_id = state["batch"]["batch_id"]
        if "decision" in state["entry"]:
            steps[step] = "already-done"
        else:
            decide_batch(
                argparse.Namespace(
                    repo=str(repo),
                    state_dir=state_dir,
                    batch=batch_id,
                    decision="accept",
                    approved_by=None,
                    approved_at=None,
                    note=None,
                    reason=None,
                    reason_category=None,
                    retry_role=None,
                    _policy_auto_accept=True,
                )
            )
            steps[step] = "done"
        step = "risk-assess"
        if steps["policy-decide"] == "done":
            state = _chain_state(root, dispatch_id)
        decision = state["entry"]["decision"]
        result["auto_accepted"] = decision.get("approved_by") == f"policy:{policy}"
        if decision.get("decision") != "accept" or not result["auto_accepted"]:
            # Someone else decided this report; whatever follows is theirs to start.
            steps.update(dict.fromkeys(POLICY_CHAIN_STEPS[1:], "not-applicable"))
            result["next_action"] = state["batch"].get("next_action")
            return result

        report = state["report"]
        assessed = report.get("role") in RISK_ASSESSED_ROLES
        if not assessed:
            steps[step] = "not-applicable"
        elif (
            state["following"] or state["batch"].get("next_action") != "risk-assessment"
        ):
            steps[step] = "already-done"
        else:
            assess_risk(
                argparse.Namespace(
                    repo=str(repo),
                    state_dir=state_dir,
                    batch=batch_id,
                    candidate_commit=report["commit_sha"],
                    base_commit=None,
                    changed_file=report["changed_files"],
                    developer_trigger=report.get("risk_triggers", []),
                    _expected_next_action="risk-assessment",
                )
            )
            steps[step] = "done"
        step = "next-dispatch"
        if steps["risk-assess"] == "done":
            state = _chain_state(root, dispatch_id)
        next_action = state["batch"].get("next_action")
        result["next_action"] = next_action
        if state["following"]:
            steps[step] = "already-done"
            result["next_dispatch_id"] = state["following"][0]["dispatch_id"]
        elif next_action == "developer" or (
            next_action == "qa"
            and policy == "low_risk"
            and assessed
            and _clean_assessment(repo, state["batch"], report)
        ):
            dispatch = state["dispatch"]
            prepared = create_dispatch(
                argparse.Namespace(
                    repo=str(repo),
                    state_dir=state_dir,
                    batch=batch_id,
                    role=next_action,
                    runtime=dispatch.get("resolved_runtime"),
                    purpose="work",
                    candidate_commit=report.get("commit_sha")
                    if next_action == "qa"
                    else None,
                    delta_review_of=None,
                    model=dispatch.get("resolved_model"),
                    effort=dispatch.get("resolved_effort"),
                    propose=False,
                    transition_digest=None,
                    approved_by=None,
                    approved_at=None,
                )
            )
            steps[step] = "done"
            result["next_dispatch_id"] = prepared["dispatch_id"]
        else:
            steps[step] = "not-applicable"
    except CoordinatorError as exc:
        steps[step] = "failed"
        result["failed_step"] = step
        result["error"] = {"message": exc.message, "remedy": exc.remedy}
    return result


def _completion(
    chain: JsonObject, dispatch_id: str, state_dir: str | None
) -> JsonObject:
    """The `completion` a report submit returns when its policy chain stopped."""
    command = _completion_command(dispatch_id, state_dir)
    error = chain["error"]
    return {
        "route": _require_route(COMPLETION_ROUTE),
        "failed_step": chain["failed_step"],
        "steps": chain["steps"],
        "error": error,
        "remedy": "the report is recorded: never submit it again. Resolve the failed step "
        f"({error['remedy']}), then the coordinator runs the completion command itself; it "
        "only runs the steps that are still pending",
        "command": command,
        "run_by": "coordinator",
    }


def complete_report(args: argparse.Namespace) -> JsonObject:
    """Run the pending steps of a recorded report's policy chain (`report complete`)."""
    repo = _repo(args)
    state_dir = getattr(args, "state_dir", None)
    chain = _run_policy_chain(repo, state_dir, args.dispatch)
    if chain["failed_step"] is not None:
        error = chain["error"]
        raise CoordinatorError(
            f"report complete stopped at step {chain['failed_step']}: {error['message']}",
            remedy=f"{error['remedy']}; then run "
            f"{_completion_command(args.dispatch, state_dir)} again",
        )
    return {
        "dispatch_id": args.dispatch,
        "route": _require_route(COMPLETION_ROUTE),
        "report": chain["report"],
        "report_sha256": chain["report_sha256"],
        "auto_accept_policy": chain["auto_accept_policy"],
        "steps": chain["steps"],
        "next_action": chain["next_action"],
        "next_dispatch_id": chain["next_dispatch_id"],
    }
