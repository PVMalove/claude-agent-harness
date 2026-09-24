"""The QA lane's CLI surface.

`qa_lane.py` owns the lane itself and deliberately never imports the coordinator; it receives the
slice it needs as its `ops` argument.  These four handlers are that hand-off point, and nothing
else.  The coordinator facade is imported inside each call: it is the module that carries the whole
`CoordinatorOps` surface, and importing it at module scope would close an import cycle.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import cast

from harness.orchestration import qa_lane
from harness.orchestration.core.utils import JsonObject


def _ops() -> qa_lane.CoordinatorOps:
    from harness.orchestration import coordinator

    return cast(qa_lane.CoordinatorOps, coordinator)


def qa_evidence(args: argparse.Namespace) -> JsonObject:
    return qa_lane.qa_evidence(args, _ops())


def run_qa(args: argparse.Namespace) -> JsonObject:
    """Run the repository-scoped QA lane without coupling it to CLI wiring."""
    result = qa_lane.run(args, _ops())
    if result.get("state") != "reported":
        return result
    from harness.orchestration.core import config as core_config
    from harness.orchestration.core.utils import _read_object, _repo
    from harness.orchestration.ledger.ledger_ops import (
        _load_batch,
        _load_dispatch,
        _state_root,
    )
    from harness.orchestration.workflow.decisions import (
        AUTO_ACCEPT_RATIONALE,
        _clean_low_risk_report,
        decide_batch,
    )

    repo = _repo(args)
    root = _state_root(args, repo)
    dispatch = _load_dispatch(root, args.dispatch)
    batch = _load_batch(root, dispatch["batch_id"])
    report = _read_object(Path(result["report"]), "QA completion report")
    if not _clean_low_risk_report(core_config._config(repo), batch, dispatch, report):
        return result
    decided = decide_batch(
        argparse.Namespace(
            repo=str(repo),
            state_dir=getattr(args, "state_dir", None),
            batch=batch["batch_id"],
            decision="accept",
            approved_by=None,
            approved_at=None,
            note=AUTO_ACCEPT_RATIONALE,
            reason=None,
            reason_category=None,
            retry_role=None,
            _policy_auto_accept=True,
        )
    )
    return {**result, "auto_accepted": True, "next_action": decided.get("next_action")}


def qa_status(args: argparse.Namespace) -> JsonObject:
    return qa_lane.status(args, _ops())


def clear_qa_lease(args: argparse.Namespace) -> JsonObject:
    return qa_lane.clear_stale_lease(args, _ops())
