"""Proven startup progress before the first checkpoint; never fabricated checkpoint evidence."""

from __future__ import annotations

import argparse
import hashlib
from datetime import UTC, datetime
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, cast

from harness.orchestration.worker_observation import (
    observe_worker,
    STARTUP_TIMEOUT_SECONDS,
)

from harness.orchestration.core import utils
from harness.orchestration.core.constants import LIVE_DISPATCH_STATES
from harness.orchestration.core.git_utils import _git
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _canonical,
    _read_object,
)
from harness.orchestration.ledger.ledger_ops import _load_dispatch, _records_root
from harness.orchestration.workflow.recovery import verified_checkout
from harness.orchestration.workflow.approval import sealed

FIELDS = frozenset(
    {
        "dispatch_id",
        "commit_sha",
        "brief_sha256",
        "scope_sha256",
        "worktree",
        "recorded_at",
        "record_sha256",
    }
)


WorkerProcessState = Literal[
    "absent", "reported", "rejected-at-startup", "stopped", "live", "unknown"
]
# A restart needs a known worker outcome; silence, a timeout or a stop ACK is never one.
RESTARTABLE_WORKER_STATES: frozenset[WorkerProcessState] = frozenset(
    {"absent", "reported", "rejected-at-startup", "stopped"}
)


def worker_process_state(
    status: Mapping[str, object],
    *,
    runtime_stopped: bool = False,
    repo: Path | None = None,
) -> WorkerProcessState:
    """Return the strongest recorded fact about the worker process, never a guess from silence.

    ``absent``: the adapter could not start the runtime, so no worker exists.  ``reported``: the
    worker returned its final report.  ``rejected-at-startup``: the coordinator refused its
    model/worktree self-report; recovery retires that dispatch and the ledger rejects its late
    writes, which is the existing ``resume-stop`` contract for a mismatch.  ``stopped``: the
    operator confirmed with ``--runtime-stopped`` that the worker has stopped.  ``live`` and
    ``unknown`` never allow a restart.
    """
    state = status.get("state")
    if state == "reported":
        return "reported"
    observation = observe_worker(
        status.get("adapter_receipt"), status.get("dispatch_id"), repo=repo
    )
    if observation is not None and state in LIVE_DISPATCH_STATES | {"blocked"}:
        return cast(WorkerProcessState, observation["state"])
    if state in LIVE_DISPATCH_STATES:
        # A legacy handoff with no process receipt or first self-report proves no liveness.
        return (
            "unknown"
            if state == "dispatched" and not status.get("model_self_report")
            else "live"
        )
    failure = status.get("runtime_failure")
    if isinstance(failure, Mapping) and failure.get("worker_started") is False:
        return "absent"
    if state == "reported":
        return "reported"
    if runtime_stopped:
        return "stopped"
    if any(
        isinstance(status.get(field), Mapping)
        and cast(Mapping[str, object], status[field]).get("match") is False
        for field in ("model_self_report", "worktree_attestation")
    ):
        return "rejected-at-startup"
    return "unknown"


def assess_startup(
    dispatch: Mapping[str, object],
    status: Mapping[str, object],
    *,
    runtime_stopped: bool = False,
    repo: Path | None = None,
) -> JsonObject:
    """Explain a stopped startup from recorded facts without inventing a model or an exit.

    The classification names the strongest cause: a refused self-report (``model-mismatch``,
    ``worktree-mismatch``, the stop reasons ``auto-report`` already uses), then a runtime failure,
    then a missing self-report.  A runtime probe never fills a missing self-report.
    """
    from harness.orchestration.runtime_attestation import requires_attestation
    from harness.orchestration.workflow import failure_classifier

    model = status.get("model_self_report")
    model_match = (
        cast(Mapping[str, object], model).get("match")
        if isinstance(model, Mapping)
        else None
    )
    tree = status.get("worktree_attestation")
    tree_match = not requires_attestation(dispatch) or (
        isinstance(tree, Mapping) and tree.get("match") is True
    )
    failure = status.get("runtime_failure")
    runtime_failure = failure_classifier.classify_runtime_failure(
        failure if isinstance(failure, Mapping) else None
    )
    worker = worker_process_state(status, runtime_stopped=runtime_stopped, repo=repo)
    if model_match is False:
        classification: str | None = "model-mismatch"
    elif model_match is True and not tree_match:
        classification = "worktree-mismatch"
    elif runtime_failure is not None:
        classification = cast(str, runtime_failure["category"])
    elif model_match is None and worker == "stopped":
        classification = "runtime-crash"
        runtime_failure = failure_classifier.classify_runtime_failure(
            {
                "kind": "runtime-crash",
                "worker_started": True,
                "error": "bound worker exited before its first model self-report",
            }
        )
    elif model_match is None:
        classification = "model-self-report-missing"
    elif worker == "stopped" and status.get("state") in LIVE_DISPATCH_STATES:
        # The worker self-reported, then its process was proven gone with no report recorded.
        classification = "worker-exited-without-report"
    else:
        classification = None
    unknowns: list[str] = []
    if model_match is None:
        unknowns.append("the active model identity is not known")
    if worker in {"live", "unknown", "rejected-at-startup"}:
        unknowns.append(f"worker termination is not proven ({worker})")
    observation = observe_worker(
        status.get("adapter_receipt"), dispatch.get("dispatch_id"), repo=repo
    )
    liveness = dispatch.get("liveness")
    timeout_value = (
        liveness.get("startup_timeout_seconds")
        if isinstance(liveness, Mapping)
        else None
    )
    timeout = (
        timeout_value
        if isinstance(timeout_value, int)
        and not isinstance(timeout_value, bool)
        and timeout_value > 0
        else STARTUP_TIMEOUT_SECONDS
    )
    sent_at = status.get("sent_at")
    elapsed = (
        max(
            0,
            int(
                (
                    datetime.now(UTC) - utils._moment(sent_at, "dispatch sent_at")
                ).total_seconds()
            ),
        )
        if sent_at
        else 0
    )
    return {
        "observation": observation,
        "startup_timeout_seconds": timeout,
        "startup_elapsed_seconds": elapsed,
        "deadline_exceeded": model_match is None and elapsed >= timeout,
        "classification": classification,
        "worker_state": worker,
        "restart_allowed": worker in RESTARTABLE_WORKER_STATES,
        "model_match": model_match,
        "worktree_match": tree_match,
        "runtime_failure": runtime_failure,
        "unknowns": unknowns,
    }


def _scope(dispatch: JsonObject) -> str:
    return hashlib.sha256(
        _canonical(
            {
                k: dispatch[k]
                for k in (
                    "definition_of_done",
                    "dependencies",
                    "write_paths",
                    "prohibited_changes",
                )
            }
        ).encode()
    ).hexdigest()


def audit(point: JsonObject) -> JsonObject:
    return {
        "decision": "startup-evidence",
        "dispatch_id": point["dispatch_id"],
        "evidence": {"startup": point},
    }


def validate(root: Path, batch: JsonObject) -> None:
    points = batch.get("startup_evidence", [])
    if not isinstance(points, list):
        raise CoordinatorError(
            "invalid startup evidence", remedy="inspect the ledger before continuing"
        )
    records = _records_root(root)
    audits = (
        [_read_object(p, "audit") for p in (records / "audit").glob("*.json")]
        if points
        else []
    )
    for point in points:
        if (
            not isinstance(point, dict)
            or set(point) != FIELDS
            or point != sealed({k: v for k, v in point.items() if k != "record_sha256"})
        ):
            raise CoordinatorError(
                "startup evidence failed integrity validation",
                remedy="inspect original startup evidence; do not rewrite its checksum",
            )
        brief = _load_dispatch(root, point["dispatch_id"])
        if (
            brief["batch_id"] != batch["batch_id"]
            or point["commit_sha"] != brief.get("snapshot_commit")
            or point["brief_sha256"]
            != hashlib.sha256(_canonical(brief).encode()).hexdigest()
            or point["scope_sha256"] != _scope(brief)
            or Path(point["worktree"]).resolve() != Path(batch["worktree"]).resolve()
        ):
            raise CoordinatorError(
                "startup evidence diverged from immutable brief",
                remedy="register proven progress or obtain a new scoped dispatch; a clean arbitrary HEAD is not evidence",
            )
        if not any(
            a.get("details", {}).get("path") == f"batches/{batch['batch_id']}.json"
            and a["details"].get("decision") == audit(point)
            for a in audits
        ):
            raise CoordinatorError(
                "startup evidence lacks its recorded audit",
                remedy="inspect the original startup transition",
            )


def point(
    repo: Path, root: Path, batch: JsonObject, dispatch: JsonObject
) -> JsonObject:
    """Read or derive a startup point and prove the issue-worktree is still clean at that SHA."""
    if dispatch.get("access") != "write" or dispatch.get("purpose") == "publish":
        raise CoordinatorError(
            "startup continuation requires a writer",
            remedy="a read-only gate or publish uses a new same-stage dispatch",
        )
    known = dispatch.get("snapshot_commit")
    if not isinstance(known, str):
        raise CoordinatorError(
            "dispatch has no known startup SHA",
            remedy="create a newly approved dispatch with an immutable startup snapshot",
        )
    checkout = Path(batch["worktree"]).resolve()
    verified_checkout(repo, dispatch, str(checkout))
    if _git(checkout, "status", "--porcelain", "--untracked-files=all"):
        raise CoordinatorError(
            "startup continuation found unknown dirty files",
            remedy="preserve and register progress with a checkpoint or a new scoped dispatch; never reset to manufacture a clean boundary",
        )
    prior = [
        p
        for p in batch.get("startup_evidence", [])
        if p["dispatch_id"] == dispatch["dispatch_id"]
    ]
    if prior:
        validate(root, batch)
        return cast(JsonObject, prior[-1])
    return sealed(
        {
            "dispatch_id": dispatch["dispatch_id"],
            "commit_sha": known,
            "brief_sha256": hashlib.sha256(_canonical(dispatch).encode()).hexdigest(),
            "scope_sha256": _scope(dispatch),
            "worktree": str(checkout),
            "recorded_at": utils._now(),
        }
    )


def recorded(root: Path, batch: JsonObject, dispatch_id: str) -> JsonObject:
    validate(root, batch)
    points = [
        p for p in batch.get("startup_evidence", []) if p["dispatch_id"] == dispatch_id
    ]
    if not points:
        raise CoordinatorError(
            "no recorded startup point",
            remedy="record dispatch rate-limited at a known clean startup SHA, or checkpoint verified progress",
        )
    return cast(JsonObject, points[-1])


def restart_authorization(dispatch: JsonObject, args: argparse.Namespace) -> JsonObject:
    """A stopped session before its first checkpoint needs human approval and unchanged facts."""
    from harness.orchestration.workflow.recovery import human_approval, _note
    from harness.orchestration.core.config import _reject_sensitive

    if not getattr(args, "runtime_stopped", False):
        raise CoordinatorError(
            "startup restart requires proof that the runtime session stopped",
            remedy="stop the worker through its runtime, then pass --runtime-stopped and human approval",
        )
    approver = human_approval(args)
    note = _note(args)
    if not getattr(args, "file", None):
        raise CoordinatorError(
            "startup restart requires unchanged contract facts",
            remedy="pass --file with dispatch_id, definition_of_done, dependencies, write_paths and prohibited_changes from the immutable brief",
        )
    facts = _read_object(Path(args.file).resolve(), "startup facts")
    _reject_sensitive(facts, "startup facts")
    keys = {
        "dispatch_id",
        "definition_of_done",
        "dependencies",
        "write_paths",
        "prohibited_changes",
    }
    if set(facts) != keys or any(facts[k] != dispatch[k] for k in keys):
        raise CoordinatorError(
            "startup facts changed the immutable contract",
            remedy="use a new human-approved scoped dispatch instead of restarting with changed scope or DoD",
        )
    return {
        "decision": "continue",
        **approver,
        "note": note,
        "trigger": "startup-failure",
        "runtime_stopped": True,
    }
