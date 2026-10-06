"""Independent local QA of the current integration candidate, without reopening its batch."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from harness.gate_runner.gate_runner import CleanRoomPolicy, run_gate, sanitise
from harness.orchestration.core import utils
from harness.orchestration.core.config import _config, _verification_commands
from harness.orchestration.core.constants import LOCAL_QA_CI_CONDITIONS, STATE_REL
from harness.orchestration.core.git_utils import (
    _candidate_commit,
    _git,
    _remote_branch_tip,
)
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _read_object,
    _repo,
)
from harness.orchestration.ledger.ledger_ops import (
    _ledger_lock,
    _records_root,
    _write_record,
)
from harness.orchestration.ledger.lifecycle import (
    IntegrationLocalQaRecord,
    LifecycleLedger,
)
from harness.orchestration.workflow import integration, pr_refresh


def _path(root: Path, record_id: str) -> Path:
    return (
        _records_root(root) / IntegrationLocalQaRecord.directory / f"{record_id}.json"
    )


def _observe_pair(
    repo: Path, root: Path, record: JsonObject, pair: JsonObject
) -> JsonObject:
    integration._check_history_unchanged(root, record)
    if pr_refresh.current_pair(root, record) != pair:
        raise CoordinatorError(
            "the recorded integration pair changed",
            remedy="request local QA for the new pair",
        )
    identity = record["identity"]
    candidate = _candidate_commit(repo, pair["candidate_sha"])
    if candidate != pair["candidate_sha"] or _git(
        repo, "merge-base", "--is-ancestor", pair["target_sha"], candidate
    ):
        raise CoordinatorError(
            "the candidate is not based on its target",
            remedy="refresh the issue branch before local QA",
        )
    branch = _remote_branch_tip(repo, identity["remote"], identity["branch"])
    target = _remote_branch_tip(repo, identity["remote"], identity["integration_ref"])
    if branch != candidate or target != pair["target_sha"]:
        raise CoordinatorError(
            "the remote candidate or target moved",
            remedy="refresh and publish the current pair before local QA",
        )
    return {"remote_branch_sha": branch, "integration_tip": target}


def integration_local_qa(args: argparse.Namespace) -> JsonObject:
    """Pin a complete local-QA request with an explicit, recorded operator CI assertion."""
    repo = _repo(args)
    if (
        getattr(args, "state_dir", None)
        and Path(args.state_dir).resolve() != repo / STATE_REL
    ):
        raise CoordinatorError(
            "local QA is repository-scoped", remedy="run without --state-dir"
        )
    root = repo / STATE_REL
    condition = integration._choice(args, "ci_condition", LOCAL_QA_CI_CONDITIONS)
    reason = sanitise(integration._text(args, "reason")).strip()
    if not reason or len(reason) > 1600:
        raise CoordinatorError(
            "local QA requires a non-empty bounded CI reason",
            remedy="pass --reason with at most 1600 characters",
        )
    commands = _verification_commands(_config(repo))
    if not commands:
        raise CoordinatorError(
            "local QA requires full project verification_commands",
            remedy="configure the full QA gate before requesting local QA",
        )
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        record = integration._load_record(root, args.record)
        pair = pr_refresh.current_pair(root, record)
        observed = _observe_pair(repo, root, record, pair)
        members: JsonObject = {
            "integration_record_id": args.record,
            **pair,
            "ci_condition": condition,
            "reason": reason,
            "verification_commands": commands,
            "commands_sha256": integration._sha256({"commands": commands}),
        }
        request_id = IntegrationLocalQaRecord.derive_id(members)
        if getattr(args, "request", None) not in (None, request_id):
            raise CoordinatorError(
                "request ID does not match the pinned inputs",
                remedy="use the original pair, CI assertion and command configuration, or create a new request",
            )
        path = _path(root, request_id)
        if path.exists():
            request = _read_object(path, "local QA request")
            if request.get("members") != members:
                raise CoordinatorError(
                    "local QA request failed integrity check",
                    remedy="restore the immutable request from trusted evidence",
                )
        else:
            request = {
                "local_qa_id": request_id,
                "event": "request",
                "members": members,
                "created_at": utils._now(),
                "observed": observed,
            }
            _write_record(ledger, IntegrationLocalQaRecord.from_dict(request))
    result_id = IntegrationLocalQaRecord.derive_id(
        {"request_id": request_id, "event": "result"}
    )
    with _ledger_lock(ledger):
        result_path = _path(root, result_id)
        if result_path.exists():
            return _read_object(result_path, "local QA result")
        _observe_pair(repo, root, record, pair)
    gate = run_gate(
        [command for command in commands],
        CleanRoomPolicy(repo, pair["candidate_sha"]),
        stop_on_failure=True,
    )
    checksum = hashlib.sha256(gate.artifact.encode("utf-8")).hexdigest()
    artifact = _records_root(root) / "qa-artifacts" / f"{checksum}.log"
    ledger.write_artifact(artifact, gate.artifact)
    result: JsonObject = {
        "local_qa_id": result_id,
        "event": "result",
        "request_id": request_id,
        **members,
        "state": "completed" if gate.passed else "failed",
        "checks_run": gate.checks,
        "duration_seconds": gate.duration_seconds,
        "artifact": str(artifact),
        "artifact_sha256": checksum,
        "finished_at": utils._now(),
        "verification": "unverified",
    }
    with _ledger_lock(ledger):
        _write_record(ledger, IntegrationLocalQaRecord.from_dict(result))
    return result
