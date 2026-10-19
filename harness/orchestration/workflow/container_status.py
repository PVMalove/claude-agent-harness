"""Worker liveness of a container dispatch: the engine's answer bound to the dispatch receipt.

A docker dispatch's receipt names the worker's container id as ``launch_id`` (ADR 0018). Reading a
dispatch asks the engine about exactly that container and records the first proof that it stopped
(``worker_exit``) in the dispatch status. Silence alone -- a missing heartbeat or report -- is never
such a proof. This module adds no lifecycle state: it only appends evidence to the status record
and, once a dispatch is terminal, releases the engine objects the dispatch owns.

Every function here runs under the caller's ledger lock; ``container_executor`` stays the only
module that talks to the engine.
"""

from __future__ import annotations

import re

from harness.orchestration import container_executor
from harness.orchestration.core import utils
from harness.orchestration.core.utils import JsonObject
from harness.orchestration.ledger.ledger_ops import _replace_record
from harness.orchestration.ledger.lifecycle import DispatchStatusRecord, LifecycleLedger

_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
# The engine answers that prove a worker stopped; ``running`` and ``unknown`` prove nothing.
STOP_PROOFS = frozenset({"exited", "missing"})


def _launch_id(status: JsonObject) -> str | None:
    """The worker container id of a docker receipt, or ``None`` for any other dispatch."""
    access = status.get("runtime_access")
    evidence = access.get("evidence") if isinstance(access, dict) else None
    if (
        not isinstance(evidence, dict)
        or evidence.get("engine") != container_executor.EXTENSION_NAME
    ):
        return None
    launch_id = evidence.get("launch_id")
    if isinstance(launch_id, str) and _CONTAINER_ID.fullmatch(launch_id):
        return launch_id
    return None


def _bound(inspected: JsonObject, dispatch: JsonObject) -> bool:
    """Whether the inspected container carries this dispatch's and this repository's labels."""
    labels = inspected.get("labels")
    plan = dispatch["runtime_access"]
    return (
        isinstance(labels, dict)
        and labels.get("harness.dispatch") == dispatch["dispatch_id"]
        and labels.get("harness.repository")
        == container_executor.plan_root(plan, "git_common")
    )


def _view(
    launch_id: str, exit_record: JsonObject | None, status: JsonObject
) -> JsonObject:
    """What ``dispatch status`` shows: the persisted proof, or the engine's live answer."""
    return {
        "launch_id": launch_id,
        "state": exit_record["state"] if exit_record else "running",
        "exit_code": exit_record["exit_code"] if exit_record else None,
        "finished_at": exit_record["finished_at"] if exit_record else None,
        "source": "ledger" if exit_record else "engine",
        "teardown": status.get("worker_teardown"),
    }


def reconcile(
    ledger: LifecycleLedger, dispatch: JsonObject, status: JsonObject
) -> JsonObject | None:
    """Bind the engine's answer to the receipt and persist the first proof that the worker stopped.

    ``status`` is the dispatch status record and is updated in place when evidence is written.
    Returns ``None`` for a dispatch without a docker receipt, which never reaches the engine.
    """
    launch_id = _launch_id(status)
    if launch_id is None:
        return None
    persisted = status.get("worker_exit")
    if isinstance(persisted, dict):
        return _view(launch_id, persisted, status)
    inspected = container_executor.inspect_worker(launch_id)
    state = inspected["state"]
    if state == "exited" and not _bound(inspected, dispatch):
        # Another object answers to this id: never act on it.
        state = "unknown"
    if state not in STOP_PROOFS:
        return {
            **_view(launch_id, None, status),
            "state": state,
        }
    exit_record: JsonObject = {
        "launch_id": launch_id,
        "state": state,
        "exit_code": inspected["exit_code"],
        "finished_at": inspected["finished_at"],
        "oom_killed": inspected["oom_killed"],
        "observed_at": utils._now(),
    }
    status["worker_exit"] = exit_record
    _replace_record(ledger, DispatchStatusRecord.from_dict(status))
    return _view(launch_id, exit_record, status)
