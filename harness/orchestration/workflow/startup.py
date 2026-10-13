"""Proven startup progress before the first checkpoint; never fabricated checkpoint evidence."""

from __future__ import annotations

import argparse
import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import cast

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


def worker_outcome(status: Mapping[str, object]) -> str:
    """Return the strongest process-state fact available to startup recovery.

    A dispatch state alone is not a termination proof.  Explicit adapter evidence wins; a
    completion report proves the worker returned, while a blocked or silent record without an exit
    flag remains ``unknown`` and is never eligible for restart.
    """
    failure = status.get("runtime_failure")
    failure = failure if isinstance(failure, Mapping) else {}
    explicit_state = status.get("worker_state") or failure.get("worker_state")
    if isinstance(explicit_state, str) and explicit_state in {
        "absent",
        "never-started",
        "exited",
        "terminated",
        "live",
        "unknown",
    }:
        return "absent" if explicit_state == "never-started" else explicit_state
    if failure.get("worker_started") is False:
        return "absent"
    if failure.get("worker_exited") is True or failure.get("terminated") is True:
        return "exited"
    if failure.get("worker_started") is True:
        return "live" if status.get("state") in LIVE_DISPATCH_STATES else "unknown"
    if status.get("state") in LIVE_DISPATCH_STATES:
        return "live"
    if status.get("state") == "reported":
        return "exited"
    model = status.get("model_self_report")
    if isinstance(model, Mapping) and model.get("match") is False:
        # The coordinator rejected the session at the startup gate.  This is not a physical
        # termination claim, but it is distinct from a valid worker whose process may continue.
        return "startup-rejected"
    return "unknown"


def assess_startup(
    dispatch: Mapping[str, object],
    status: Mapping[str, object],
    runtime_evidence: Mapping[str, object] | None = None,
) -> JsonObject:
    """Assess startup evidence without inventing a model, readiness or process exit.

    ``runtime_evidence`` is intentionally advisory: a model/list or thread/start observation is
    recorded as unsupported for self-report purposes until the worker reports through the normal
    coordinator command.
    """
    from harness.orchestration.runtime_attestation import (
        requires_attestation,
        validate_runtime_evidence,
    )
    from harness.orchestration.workflow import failure_classifier

    observed_model = status.get("model_self_report")
    model_dispatch = dict(dispatch)
    model_dispatch.pop("worktree", None)
    model_dispatch.pop("worker_worktree", None)
    # ``dispatch self-report`` records the model identity only.  Effort/provider/CWD may be
    # observed by a runtime probe, but requiring them here would turn a valid model self-report
    # into a fabricated mismatch.
    model_dispatch.pop("resolved_effort", None)
    model_dispatch.pop("effort", None)
    model = (
        validate_runtime_evidence(model_dispatch, observed_model)
        if isinstance(observed_model, Mapping)
        else {
            "status": "missing",
            "match": False,
            "model_self_report_required": True,
            "reasons": ["model self-report is missing"],
        }
    )
    probe = (
        validate_runtime_evidence(dispatch, runtime_evidence)
        if isinstance(runtime_evidence, Mapping)
        else None
    )
    tree = status.get("worktree_attestation")
    tree_match = (
        True
        if not requires_attestation(dispatch)
        else isinstance(tree, Mapping) and tree.get("match") is True
    )
    failure = status.get("runtime_failure")
    failure = failure if isinstance(failure, Mapping) else {}
    classification = failure_classifier.classify_runtime_failure(failure)
    worker = worker_outcome(status)
    if (
        worker == "unknown"
        and status.get("state") not in LIVE_DISPATCH_STATES
        and model.get("match") is True
        and requires_attestation(dispatch)
        and isinstance(tree, Mapping)
        and tree.get("match") is False
    ):
        worker = "startup-rejected"
    reasons: list[str] = []
    if probe is not None and probe.get("status") == "unsupported":
        reasons.append("runtime attestation is unsupported and cannot replace worker self-report")
    if model.get("status") == "missing":
        reasons.append("model self-report is missing")
    elif model.get("match") is not True:
        reasons.append("model self-report does not match the immutable brief")
    if requires_attestation(dispatch) and not tree_match:
        reasons.append("worktree attestation is missing or mismatched")
    if classification is not None:
        reasons.append(f"runtime failure classified as {classification['category']}")
    if worker in {"live", "unknown"}:
        reasons.append(f"worker termination is not proven ({worker})")
    if probe is not None and probe.get("status") != "verified":
        reasons.append("runtime probe evidence is advisory only")
    if probe is not None and probe.get("status") == "unsupported":
        category = "unsupported-runtime-attestation"
    elif model.get("status") == "missing":
        category = "model-self-report-missing"
    elif model.get("match") is not True:
        category = "model-self-report-mismatch"
    elif requires_attestation(dispatch) and not tree_match:
        category = "worktree-attestation-mismatch"
    elif classification is not None:
        category = classification["category"]
    else:
        category = None
    restart_allowed = worker in {"absent", "exited", "startup-rejected"}
    ready = (
        model.get("match") is True
        and tree_match
        and classification is None
        and status.get("state") == "working"
        and worker == "live"
    )
    unknowns: list[str] = []
    if worker == "unknown":
        unknowns.append("runtime has not supplied a worker exit or never-started fact")
    if model.get("status") == "missing":
        unknowns.append("the active model identity is not known")
    if probe is not None:
        unknowns.append("runtime probe fields do not satisfy the mandatory self-report")
    return {
        "classification": category,
        "worker_state": worker,
        "restart_allowed": restart_allowed,
        "recovery_allowed": restart_allowed and category is not None,
        "ready": ready,
        "model_self_report": model,
        "runtime_probe": probe,
        "worktree_match": tree_match,
        "runtime_failure": classification,
        "facts": {
            "dispatch_state": status.get("state"),
            "worker_started": failure.get("worker_started"),
            "worker_exited": failure.get("worker_exited"),
        },
        "reasons": reasons,
        "unknowns": unknowns,
    }


classify_startup = assess_startup


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
