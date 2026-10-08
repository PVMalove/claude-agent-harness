"""Independent local QA of the current integration candidate, without reopening its batch."""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path
from typing import cast

from harness.storage import storage_path
from harness.gate_runner.gate_runner import (
    CleanRoomPolicy,
    GateResult,
    GateRunnerError,
    parse_command_log,
    run_gate,
    sanitise,
)
from harness.orchestration.core import utils
from harness.orchestration.core.config import (
    _attention_policy,
    _config,
    _verification_commands,
)
from harness.orchestration.core.config import _execution_policy
from harness.orchestration import operation_access, qa_lane
from harness.orchestration.core.constants import (
    INTEGRATION_SHA_PATTERN,
    LOCAL_QA_CI_CONDITIONS,
    STATE_REL,
)
from harness.orchestration.core.git_utils import (
    _candidate_commit,
    _git_is_ancestor,
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
    LedgerError,
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
    if candidate != pair["candidate_sha"] or not _git_is_ancestor(
        repo, pair["target_sha"], candidate
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


def _result_id(request_id: str) -> str:
    return IntegrationLocalQaRecord.derive_id(
        {"request_id": request_id, "event": "result"}
    )


def _valid_result(root: Path, result: JsonObject) -> bool:
    request_id = result["request_id"]
    request = _read_object(_path(root, request_id), "local QA request")
    members = request["members"]
    if (
        request_id != IntegrationLocalQaRecord.derive_id(members)
        or request.get("local_qa_id") != request_id
    ):
        return False
    if (
        result.get("local_qa_id") != _result_id(request_id)
        or result.get("event") != "result"
    ):
        return False
    if any(result.get(key) != value for key, value in members.items()):
        return False
    commands = members["verification_commands"]
    if not commands or members["commands_sha256"] != integration._sha256(
        {"commands": commands}
    ):
        return False
    checks = result["checks_run"]
    if [check["command"] for check in checks] != commands or any(
        check["result"] != "pass" for check in checks
    ):
        return False
    checksum = result["artifact_sha256"]
    if INTEGRATION_SHA_PATTERN.fullmatch(checksum) is None or len(checksum) != 64:
        return False
    artifact = _records_root(root) / "qa-artifacts" / f"{checksum}.log"
    if str(artifact) != result["artifact"]:
        return False
    contents = artifact.read_bytes()
    if hashlib.sha256(contents).hexdigest() != checksum:
        return False
    logs = parse_command_log(contents.decode("utf-8").splitlines())
    return len(logs) == len(commands) and all(code == 0 for _, code in logs)


def verified_evidence(root: Path, link: JsonObject) -> bool:
    """Recheck generated evidence; a manual assertion never verifies local fallback."""
    if link.get("verification") != "verified" or not link.get("local_qa_request_id"):
        return False
    return _verified_generated(root, link)


def _verified_generated(root: Path, link: JsonObject) -> bool:
    try:
        request_id = link["local_qa_request_id"]
        if (
            not isinstance(request_id, str)
            or re.fullmatch(r"local-qa-[0-9a-f]{32}", request_id) is None
        ):
            return False
        result = _read_object(_path(root, _result_id(request_id)), "local QA result")
        return (
            result.get("verification") == "verified"
            and result.get("state") == "completed"
            and link.get("local_qa_result_sha256") == integration._sha256(result)
            and all(
                link.get(key) == result.get(key)
                for key in (
                    "integration_record_id",
                    "candidate_sha",
                    "target_sha",
                    "artifact_sha256",
                )
            )
            and _valid_result(root, result)
        )
    except (OSError, CoordinatorError, KeyError, TypeError, ValueError):
        return False


def _attempts(root: Path, request_id: str) -> list[JsonObject]:
    directory = _records_root(root) / IntegrationLocalQaRecord.directory
    found = [
        _read_object(path, "local QA attempt") for path in directory.glob("*.json")
    ]
    return sorted(
        (
            item
            for item in found
            if item.get("event") == "attempt-started"
            and item.get("request_id") == request_id
        ),
        key=lambda item: item["attempt"],
    )


def _start_attempt(
    ledger: LifecycleLedger, request_id: str, attempt: int
) -> JsonObject:
    members: JsonObject = {
        "event": "attempt-started",
        "request_id": request_id,
        "attempt": attempt,
    }
    document = {
        **members,
        "local_qa_id": IntegrationLocalQaRecord.derive_id(members),
        "started_at": utils._now(),
    }
    _write_record(ledger, IntegrationLocalQaRecord.from_dict(document))
    return document


def _unavailable(
    ledger: LifecycleLedger,
    root: Path,
    request_id: str,
    started: JsonObject,
    failure: Exception,
    stage: str,
    gate: GateResult | None = None,
    access: JsonObject | None = None,
) -> JsonObject:
    """Retain operational evidence separately from deterministic findings; no retry loop."""
    details: JsonObject = {
        "request_id": request_id,
        "event": "attempt-finished",
        "attempt": started["attempt"],
        "state": "unavailable",
        "stage": stage,
        "reason": sanitise(str(failure)),
        "finished_at": utils._now(),
        "findings": [],
        "checks_run": gate.checks if gate else [],
    }
    if access is not None:
        # The structured refusal and its remedy are operational evidence, never a finding.
        details["access"] = access
        details["remedy"] = sanitise(getattr(failure, "remedy", ""))
    details["local_qa_id"] = IntegrationLocalQaRecord.derive_id(
        {
            "request_id": request_id,
            "event": "attempt-finished",
            "attempt": started["attempt"],
        }
    )
    if gate is not None:
        checksum = hashlib.sha256(gate.artifact.encode("utf-8")).hexdigest()
        artifact = _records_root(root) / "qa-artifacts" / f"{checksum}.log"
        try:
            ledger.write_artifact(artifact, gate.artifact)
            details.update({"artifact": str(artifact), "artifact_sha256": checksum})
        except (LedgerError, OSError) as exc:
            details["artifact_persistence_reason"] = sanitise(str(exc))
    try:
        _write_record(ledger, IntegrationLocalQaRecord.from_dict(details))
    except (CoordinatorError, OSError) as exc:
        details["attempt_persistence_reason"] = sanitise(str(exc))
    return details


def _link_result(ledger: LifecycleLedger, root: Path, result: JsonObject) -> JsonObject:
    linked = integration._store_evidence(
        root,
        ledger,
        {
            "integration_record_id": result["integration_record_id"],
            "kind": "local-qa",
            "candidate_sha": result["candidate_sha"],
            "target_sha": result["target_sha"],
            "result": "passed" if result["state"] == "completed" else "failed",
            "reference": str(_path(root, result["local_qa_id"])),
            "artifact_sha256": result["artifact_sha256"],
        },
        result["verification"],
        {
            "local_qa_request_id": result["request_id"],
            "local_qa_result_sha256": integration._sha256(result),
        },
    )
    if result["verification"] == "verified" and not _verified_generated(
        root, linked["evidence"]
    ):
        raise CoordinatorError(
            "existing link does not verify the generated result",
            remedy="retain the result and resolve the conflicting immutable evidence with the coordinator",
        )
    return {**result, "evidence_id": linked["evidence_id"]}


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
            integration._check_history_unchanged(root, record)
            try:
                observed = _observe_pair(repo, root, record, pair)
            except CoordinatorError as exc:
                observed = {"state": "unavailable", "reason": sanitise(exc.message)}
            request = {
                "local_qa_id": request_id,
                "event": "request",
                "members": members,
                "created_at": utils._now(),
                "observed": observed,
            }
            _write_record(ledger, IntegrationLocalQaRecord.from_dict(request))
    result_id = _result_id(request_id)
    with _ledger_lock(ledger):
        result_path = _path(root, result_id)
        if result_path.exists():
            existing = _read_object(result_path, "local QA result")
            try:
                _observe_pair(repo, root, record, pair)
                if existing.get("verification") == "verified" and not _valid_result(
                    root, existing
                ):
                    return {
                        **existing,
                        "verification": "unverified",
                        "verification_reason": "generated evidence failed integrity check",
                    }
            except (CoordinatorError, OSError) as exc:
                return {
                    **existing,
                    "verification": "unverified",
                    "verification_reason": sanitise(str(exc)),
                }
            try:
                return _link_result(ledger, root, existing)
            except (CoordinatorError, OSError) as exc:
                return {
                    **existing,
                    "state": "unavailable",
                    "verification": "unverified",
                    "reason": sanitise(str(exc)),
                }
        from harness.orchestration import coordinator

        ops = cast(qa_lane.CoordinatorOps, coordinator)
        running = qa_lane.running_owner(ledger, request_id, ops, owner_kind="local-qa")
        if running is not None:
            return {"request_id": request_id, **members, **running}
        previous = _attempts(root, request_id)
        limit = _attention_policy(_config(repo))["max_infrastructure_retries"]
        if len(previous) > limit:
            return {
                "request_id": request_id,
                "state": "exhausted",
                "attempts": len(previous),
                "findings": [],
                "reason": "infrastructure retry limit reached",
            }
        if previous and not getattr(args, "retry", False):
            return {
                "request_id": request_id,
                "state": "unavailable",
                "attempts": len(previous),
                "findings": [],
                "reason": "explicit --retry is required after an operational attempt",
            }
        try:
            observed = _observe_pair(repo, root, record, pair)
        except CoordinatorError as exc:
            started = _start_attempt(ledger, request_id, len(previous) + 1)
            qa_lane.withdraw(ledger, request_id, ops, owner_kind="local-qa")
            return _unavailable(
                ledger, root, request_id, started, exc, "pair-observation"
            )
        try:
            operation_access.require(
                repo,
                _config(repo),
                "qa",
                checkout=storage_path(repo, "runs", "qa"),
            )
        except operation_access.OperationAccessError as exc:
            started = _start_attempt(ledger, request_id, len(previous) + 1)
            qa_lane.withdraw(ledger, request_id, ops, owner_kind="local-qa")
            return _unavailable(
                ledger, root, request_id, started, exc, "access", access=exc.evidence
            )
        seconds = getattr(args, "lease_seconds", None)
        if seconds is None:
            seconds = _execution_policy(_config(repo))["qa_lease_seconds"]
        admission = qa_lane.acquire(
            ledger, request_id, ops, owner_kind="local-qa", lease_seconds=seconds
        )
        if admission["state"] == "queued":
            return {"request_id": request_id, **members, **admission}
        claimed = admission["lease"]
        try:
            started = _start_attempt(ledger, request_id, len(previous) + 1)
        except (CoordinatorError, OSError):
            qa_lane.release(ledger, claimed, ops)
            raise
    gate: GateResult | None = None
    stage = "gate-run"
    try:
        gate = run_gate(
            [command for command in commands],
            CleanRoomPolicy(repo, pair["candidate_sha"]),
            stop_on_failure=True,
        )
        stage = "artifact-persistence"
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
            "observed_before_run": observed,
            "attempt": started["attempt"],
            "findings": [
                {
                    "severity": "blocker",
                    "command": check["command"],
                    "summary": sanitise(check["evidence"]),
                }
                for check in gate.checks
                if check["result"] == "fail"
            ],
        }
        stage = "result-finalization"
        with _ledger_lock(ledger):
            try:
                result["observed_at_finish"] = _observe_pair(repo, root, record, pair)
                if gate.passed and _valid_result(root, result):
                    result["verification"] = "verified"
            except CoordinatorError as exc:
                result["verification_reason"] = sanitise(exc.message)
            _write_record(ledger, IntegrationLocalQaRecord.from_dict(result))
            return _link_result(ledger, root, result)
    except (GateRunnerError, CoordinatorError, LedgerError, OSError) as exc:
        if isinstance(exc, GateRunnerError) and exc.partial_result is not None:
            gate = exc.partial_result
        with _ledger_lock(ledger):
            return _unavailable(ledger, root, request_id, started, exc, stage, gate)
    finally:
        with _ledger_lock(ledger):
            qa_lane.release(ledger, claimed, ops)
