"""QA workflow adapter and CLI handlers.

The lane owns FIFO, leases and gate execution. This module supplies batch validation and report
persistence directly from their owning modules, without importing the CLI coordinator.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from harness.orchestration import operation_access, qa_lane
from harness.orchestration.core.utils import JsonObject


from harness.orchestration.core import config, git_utils, utils
from harness.orchestration.ledger import ledger_ops
from harness.orchestration.workflow import approval, history, reports


class _WorkflowOps:
    """Concrete batch QA adapter; none of these methods belongs to FIFO admission."""

    _repo = staticmethod(utils._repo)
    _candidate_commit = staticmethod(git_utils._candidate_commit)
    _batch_for_ticket_branch = staticmethod(history._batch_for_ticket_branch)
    _accepted_qa_for_candidate = staticmethod(history._accepted_qa_for_candidate)
    _load_batch = staticmethod(ledger_ops._load_batch)
    _load_dispatch = staticmethod(ledger_ops._load_dispatch)
    _load_dispatch_status = staticmethod(ledger_ops._load_dispatch_status)
    _validate_batch_integrity = staticmethod(history._validate_batch_integrity)
    _validate_dispatch = staticmethod(history._validate_dispatch)
    _config = staticmethod(config._config)
    _role = staticmethod(config._role)
    _validate_report = staticmethod(reports._validate_report)
    _persist_report = staticmethod(reports._persist_report)
    _approval = staticmethod(approval._approval)


_WORKFLOW_OPS = _WorkflowOps()


def _ops() -> qa_lane.QaWorkflowOps:
    return _WORKFLOW_OPS


def qa_evidence(args: argparse.Namespace) -> JsonObject:
    return qa_lane.qa_evidence(args, _ops())


def run_qa(args: argparse.Namespace) -> JsonObject:
    """Run the repository-scoped QA lane without coupling it to CLI wiring."""
    try:
        result = qa_lane.run(args, _ops())
    except operation_access.OperationAccessError as exc:
        from harness.orchestration.infrastructure_retry import (
            pinned,
            record_operation_failure,
        )
        from harness.orchestration.core.utils import _repo
        from harness.orchestration.ledger.ledger_ops import _load_dispatch, _state_root

        repo = _repo(args)
        root = _state_root(args, repo)
        dispatch = _load_dispatch(root, args.dispatch)
        policy = pinned(dispatch)
        if policy and policy["enabled"]:
            record_operation_failure(repo, root, dispatch, "qa", exc.evidence)
        raise
    if result.get("state") != "reported":
        return result
    from harness.orchestration.core import config as core_config
    from harness.orchestration.core.utils import _read_object, _repo
    from harness.orchestration.ledger.ledger_ops import (
        _load_dispatch,
        _state_root,
    )
    from harness.orchestration.workflow.decisions import (
        _auto_accept_policy,
        decide_batch,
    )

    repo = _repo(args)
    root = _state_root(args, repo)
    dispatch = _load_dispatch(root, args.dispatch)
    batch = ledger_ops._load_batch(root, dispatch["batch_id"])
    report = _read_object(Path(result["report"]), "QA completion report")
    from harness.orchestration.infrastructure_retry import pinned
    from harness.orchestration.workflow.completion import _run_policy_chain, _completion

    policy = pinned(dispatch)
    if policy and policy["enabled"] and report.get("outcome") == "blocked":
        chain = _run_policy_chain(repo, getattr(args, "state_dir", None), args.dispatch)
        result["next_dispatch_id"] = chain["next_dispatch_id"]
        if chain["failed_step"] is not None:
            result["completion"] = _completion(
                chain, args.dispatch, getattr(args, "state_dir", None)
            )
        return result
    if _auto_accept_policy(core_config._config(repo), batch, dispatch, report) not in {
        "low_risk",
        "auto",
    }:
        return result
    decided = decide_batch(
        argparse.Namespace(
            repo=str(repo),
            state_dir=getattr(args, "state_dir", None),
            batch=batch["batch_id"],
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
    return {**result, "auto_accepted": True, "next_action": decided.get("next_action")}


def qa_status(args: argparse.Namespace) -> JsonObject:
    return qa_lane.status(args)


def clear_qa_lease(args: argparse.Namespace) -> JsonObject:
    return qa_lane.clear_stale_lease(args, _ops())
