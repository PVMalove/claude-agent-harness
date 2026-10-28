"""The coordinator's reaction to the observed execution of a supervised dispatch (ADR 0021,
section 6).

The runtime writes the execution and never a ``DispatchState``; Python chooses the transition from
what the runtime observed:

- ``launching``, ``running``, ``stopping``, ``unknown``: no transition. A report submitted now is
  recorded on the status (``pending_report``) and waits for the exit, because the store refuses a
  terminal dispatch state while the execution may still run.
- ``exited`` with a recorded report: ``reported``, through the transition code of ``report
  submit``.
- ``exited`` without a report: ``blocked``, domain ``timeout`` when the runtime stopped the
  worker at its deadline, otherwise the classification ``worker-exited-without-report``.
- ``lost``: ``blocked``, domain ``infrastructure``; the developer retry budget is untouched.

``dispatch status --dispatch``, ``dispatch wait`` and ``report submit`` call it.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from harness.orchestration.core import utils
from harness.orchestration.core.constants import LIVE_DISPATCH_STATES
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.ledger.ledger_ops import (
    _ledger_lock,
    _load_batch,
    _load_dispatch,
    _load_dispatch_status,
    _replace_record,
    store_is_authority,
)
from harness.orchestration.ledger.lifecycle import (
    BatchRecord,
    DispatchStatusRecord,
    LifecycleLedger,
)

# The execution states after which the process may still run: no dispatch transition.
UNSETTLED_EXECUTION_STATES = frozenset({"launching", "running", "stopping", "unknown"})


def supervised(root: Path, dispatch: JsonObject) -> bool:
    """Whether the runtime supervises the execution of ``dispatch``."""
    return bool(dispatch.get("execution")) and store_is_authority(root)


def observe(repo: Path, root: Path, dispatch_id: str) -> JsonObject | None:
    """Observe the execution of a supervised dispatch and apply the reaction; ``None`` for a
    dispatch the runtime does not supervise or never launched."""
    from harness.orchestration.workflow.delivery import observed_execution

    dispatch = _load_dispatch(root, dispatch_id)
    if not supervised(root, dispatch):
        return None
    execution = observed_execution(repo, dispatch_id)
    if (
        execution is not None
        and execution.get("state") not in UNSETTLED_EXECUTION_STATES
    ):
        react(repo, root, dispatch, execution)
    return execution


def awaits_exit(repo: Path, root: Path, dispatch: JsonObject) -> bool:
    """Whether a report of ``dispatch`` must wait for its execution to end."""
    from harness.orchestration.workflow.delivery import observed_execution

    if not supervised(root, dispatch):
        return False
    execution = observed_execution(repo, dispatch["dispatch_id"])
    return (
        execution is not None and execution.get("state") in UNSETTLED_EXECUTION_STATES
    )


def hold_report(
    ledger: LifecycleLedger,
    status: JsonObject,
    report: JsonObject,
    report_relay: JsonObject | None,
) -> None:
    """Record a validated report on the status of a running execution; the state stays."""
    held = status.get("pending_report")
    pending: JsonObject = {"report": report, "report_relay": report_relay}
    if isinstance(held, dict) and held.get("report") != report:
        raise CoordinatorError(
            "a different report of this dispatch already waits for the execution to end",
            remedy="inspect dispatch status; a recorded report is never replaced",
        )
    status.update({"pending_report": pending, "updated_at": utils._now()})
    _replace_record(ledger, DispatchStatusRecord.from_dict(status))


def _failure(execution: JsonObject) -> JsonObject:
    """The ``runtime_failure`` of an execution that ended without a report."""
    exit_ = execution.get("exit")
    stopped_by = (
        cast(JsonObject, exit_).get("stopped_by") if isinstance(exit_, dict) else None
    )
    if execution.get("state") == "lost":
        return {
            "kind": "runtime-unavailable",
            "operation": "execution",
            "worker_started": None,
            "execution_state": "lost",
            "error": "the runtime cannot prove how the execution ended: it is lost",
        }
    if stopped_by == "deadline":
        return {
            "kind": "timeout",
            "operation": "execution",
            "worker_started": True,
            "execution_state": "exited",
            "error": "the runtime stopped the worker at its execution deadline without a report",
        }
    return {
        "kind": "worker-exited-without-report",
        "operation": "execution",
        "worker_started": True,
        "execution_state": "exited",
        "error": "the worker exited without a completion report",
    }


def react(repo: Path, root: Path, dispatch: JsonObject, execution: JsonObject) -> None:
    """Choose the dispatch transition of an execution that ended."""
    from harness.orchestration.pinned_configuration import read_snapshot
    from harness.orchestration.workflow.reports import _record_report

    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        status = _load_dispatch_status(root, dispatch["dispatch_id"])
        if status.get("state") not in LIVE_DISPATCH_STATES:
            return
        batch = _load_batch(root, dispatch["batch_id"])
        pending = status.pop("pending_report", None)
        if execution.get("state") == "exited" and isinstance(pending, dict):
            _replace_record(ledger, DispatchStatusRecord.from_dict(status))
            pinned = read_snapshot(dispatch)
            relay = pending.get("report_relay")
            _record_report(
                repo,
                ledger,
                root,
                batch,
                dispatch,
                cast(JsonObject, pending["report"]),
                pinned["role"],
                pinned["config"],
                cast(JsonObject, relay) if isinstance(relay, dict) else None,
            )
            return
        status.update(
            {
                "state": "blocked",
                "updated_at": utils._now(),
                "runtime_failure": _failure(execution),
            }
        )
        for item in batch["dispatches"]:
            if item["dispatch_id"] == dispatch["dispatch_id"]:
                item["state"] = "blocked"
        batch["state"] = "blocked"
        _replace_record(ledger, DispatchStatusRecord.from_dict(status))
        _replace_record(ledger, BatchRecord.from_dict(batch))
