"""Proven startup progress before the first checkpoint; never fabricated checkpoint evidence."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from typing import cast

from harness.orchestration.core import utils
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
