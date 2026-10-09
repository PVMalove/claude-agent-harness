"""Approval-bound infrastructure recovery; this policy never grants native permissions."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import argparse
import hashlib
import contextlib
import uuid

from . import operational_guards
from .core.utils import CoordinatorError, JsonObject

TRANSITION_FIELD = "infrastructure_retry_sha256"
COMMAND_FIELDS = ("qa_preparation", "qa_environment_probes", "qa_project_file_checks")

ATTEMPT_TRANSITION_FIELD = "infrastructure_attempt_sha256"
# Ledger-relative directory of the immutable operation refusals a dispatch status references.
_ATTEMPTS_DIRECTORY = "qa-lane/operation-attempts/"


class _RetryRefusal(CoordinatorError):
    def __init__(self, message: str, *, remedy: str, attention_reason: str | None):
        super().__init__(message, remedy=remedy)
        self.attention_reason = attention_reason


def _remote_url_sha256(repo: Path, remote: str) -> str:
    """Digest of the remote's configured URL, as an operation attempt records it."""
    from .core.git_utils import _git

    return hashlib.sha256(_git(repo, "remote", "get-url", remote).encode()).hexdigest()


def _operation_attempt(root: Path, relative: object) -> JsonObject:
    """Read the referenced immutable operation attempt; any other ledger path is refused."""
    from .core.utils import _read_object
    from .ledger.ledger_ops import _records_root

    if (
        not isinstance(relative, str)
        or not relative.startswith(_ATTEMPTS_DIRECTORY)
        or ".." in Path(relative).parts
    ):
        raise CoordinatorError(
            "invalid infrastructure evidence path",
            remedy="inspect the ledger integrity",
        )
    return _read_object(_records_root(root) / relative, "infrastructure attempt")


def _enabled_policy(brief: JsonObject) -> JsonObject:
    """The approval-bound retry policy of the brief; a disabled one needs a manual decision."""
    policy = pinned(brief)
    if not policy or not policy["enabled"]:
        raise CoordinatorError(
            "infrastructure retry is not enabled in this approval",
            remedy="use the manual coordinator decision",
        )
    return policy


def _retry_budget(batch: JsonObject, brief: JsonObject, policy: JsonObject) -> int:
    used = sum(
        1
        for decision in batch.get("coordinator_decisions", [])
        if decision.get("dispatch_id") != brief["dispatch_id"]
        and decision.get("decision") in {"retry", "cancel"}
        and decision.get("routing", {}).get("reason_category")
        == "verification-infrastructure"
        and decision.get("routing", {}).get("candidate_commit")
        == brief["candidate_commit"]
    )
    if used >= policy["max_infrastructure_retries"]:
        raise _RetryRefusal(
            "infrastructure retry budget exhausted",
            remedy="inspect and resolve attention; policy cannot authorize another retry",
            attention_reason="infrastructure-retry-repeated",
        )
    if batch.get("needs_attention"):
        raise CoordinatorError(
            "infrastructure retry requires human attention",
            remedy="resolve the batch attention before continuing",
        )
    return used


def require_same_contract(source: JsonObject, retry: JsonObject) -> None:
    # Only identity, approval, transition history and lifecycle metadata change on recovery.
    renewed = {
        "dispatch_id",
        "report_staging_path",
        "coordinator_approval",
        "transition",
        "transition_digest",
        "retry_idempotency_key",
        "state",
        "created_at",
    }
    changed = sorted(
        key
        for key in set(source) | set(retry)
        if key not in renewed and source.get(key) != retry.get(key)
    )
    if changed:
        raise CoordinatorError(
            "infrastructure retry changed the approved boundaries: "
            + ", ".join(changed),
            remedy="propose and approve the changed contract manually",
        )


def require_publish_destination(
    repo: Path, root: Path, brief: JsonObject, remote: str
) -> None:
    """Bind publication of a policy successor to the original immutable access attempt."""
    from .ledger.ledger_ops import _load_dispatch_status

    transition = brief["transition"]
    checksum = transition.get(ATTEMPT_TRANSITION_FIELD)
    if checksum is None:
        return
    status = _load_dispatch_status(root, transition["previous_dispatch_id"])
    failure = status.get("infrastructure_failure", {})
    attempt = _operation_attempt(root, failure.get("path", ""))
    if (
        failure.get("sha256") != checksum
        or operational_guards.policy_digest(attempt) != checksum
        or attempt.get("dispatch_id") != transition["previous_dispatch_id"]
        or attempt.get("operation") != "publish"
        or attempt.get("remote") != remote
        or _remote_url_sha256(repo, remote) != attempt.get("remote_url_sha256")
    ):
        raise CoordinatorError(
            "publish destination changed since the approved infrastructure retry",
            remedy="use a new manual approval for the changed destination",
        )


def snapshot(config: JsonObject) -> JsonObject:
    from .core.config import _attention_policy, _qa_preparation_commands

    policy = config.get("infrastructure_retry_policy", {})
    return {
        "enabled": policy.get("enabled", False),
        "max_infrastructure_retries": _attention_policy(config)[
            "max_infrastructure_retries"
        ],
        **{key: _qa_preparation_commands(config, key) for key in COMMAND_FIELDS},
    }


def pinned(brief: Mapping[str, object]) -> JsonObject | None:
    policy = brief.get("orchestration_policy")
    value = policy.get("infrastructure_retry") if isinstance(policy, dict) else None
    transition = brief.get("transition")
    bound = transition.get(TRANSITION_FIELD) if isinstance(transition, dict) else None
    if value is None and bound is None:
        return None  # historical contract: never opts in retroactively
    if (
        not isinstance(value, dict)
        or set(value) != {"enabled", "max_infrastructure_retries", *COMMAND_FIELDS}
        or not isinstance(value.get("enabled"), bool)
        or isinstance(value.get("max_infrastructure_retries"), bool)
        or not isinstance(value.get("max_infrastructure_retries"), int)
        or value["max_infrastructure_retries"] < 0
        or any(
            not isinstance(value.get(key), list)
            or any(
                not isinstance(command, str) or not command.strip()
                for command in value[key]
            )
            for key in COMMAND_FIELDS
        )
        or bound != operational_guards.policy_digest(value)
    ):
        raise CoordinatorError(
            "infrastructure retry contract is malformed or not approval-bound",
            remedy="propose and approve a new dispatch; preserve the historical evidence",
        )
    return value


def authorize(
    repo: Path,
    root: Path,
    batch: JsonObject,
    brief: JsonObject,
    report: JsonObject,
) -> JsonObject:
    """Recheck a pending retry against immutable evidence and fresh environment readiness."""
    from harness.gate_runner.gate_runner import CleanRoomPolicy, run_gate
    from . import operation_access, runtime_access
    from .core import config as core_config
    from .workflow.decisions import _decide_retry_route

    policy = _enabled_policy(brief)
    stages = report.get("qa_stages", {})
    if (
        stages.get("failed_stage") != "preparation"
        or stages.get("code_checks_started") != "not_started"
        or stages.get("diagnosis", {}).get("category") != "infrastructure"
    ):
        raise CoordinatorError(
            "the report is not confirmed infrastructure",
            remedy="inspect the evidence and use a manual decision",
        )
    routing = _decide_retry_route(
        repo, root, batch, brief, report, argparse.Namespace()
    )
    if (
        brief["role"] != "qa"
        or routing["route"] != "same-candidate-rerun"
        or routing["reason_category"] != "verification-infrastructure"
    ):
        raise CoordinatorError(
            "the report is not confirmed infrastructure eligible for policy retry",
            remedy="inspect the evidence and use a manual decision",
        )
    config = core_config._config(repo)
    if snapshot(config) != policy:
        raise CoordinatorError(
            "infrastructure retry commands or policy changed",
            remedy="propose and approve the changed contract manually",
        )
    plan = runtime_access.resolve_plan(
        repo,
        Path(brief["worktree"]),
        config,
        brief["role"],
        brief["access"],
        operation="qa",
    )
    if plan != brief["runtime_access"]:
        raise CoordinatorError(
            "infrastructure retry access changed",
            remedy="propose and approve the changed access manually",
        )
    used = _retry_budget(batch, brief, policy)
    from harness.storage import storage_path

    access = operation_access.require(
        repo, config, "qa", brief=brief, checkout=storage_path(repo, "runs", "qa")
    )
    # A preparation failure alone proves no readiness: both independent checks must now pass.
    commands = [*policy["qa_environment_probes"], *policy["qa_project_file_checks"]]
    if not policy["qa_environment_probes"] or not policy["qa_project_file_checks"]:
        raise CoordinatorError(
            "infrastructure readiness cannot be verified",
            remedy="use a manual decision with independently confirmed environment evidence",
        )
    ready = run_gate(
        commands, CleanRoomPolicy(repo, brief["candidate_commit"]), stop_on_failure=True
    )
    if not ready.passed:
        raise _RetryRefusal(
            "infrastructure environment is still not ready",
            remedy="restore the previously approved environment, then run report complete again",
            attention_reason=None,
        )
    return {
        "source": "infrastructure-retry",
        "used_retries": used,
        "max_infrastructure_retries": policy["max_infrastructure_retries"],
        "source_dispatch_id": brief["dispatch_id"],
        "access": access,
        "checks": ready.checks,
    }


def source(root: Path, batch: JsonObject) -> JsonObject | None:
    """Only a recorded policy decision can approve its following same-stage dispatch."""
    from .ledger.ledger_ops import _load_dispatch

    for entry in reversed(batch.get("dispatches", [])):
        cancellation = entry.get("cancellation", {})
        if cancellation.get("approved_by") == "policy:infrastructure-retry":
            return _load_dispatch(root, entry["dispatch_id"])
        decision = entry.get("decision", {})
        if decision:
            if (
                decision.get("approved_by") != "policy:infrastructure-retry"
                or decision.get("decision") != "retry"
            ):
                return None
            return _load_dispatch(root, entry["dispatch_id"])
    return None


def record_operation_failure(
    repo: Path,
    root: Path,
    brief: JsonObject,
    operation: str,
    evidence: JsonObject,
    remote: str | None = None,
    *,
    locked: bool = False,
) -> None:
    """Keep the verifier's actual refusal as an immutable artifact, referenced by status."""
    from .core import utils
    from .ledger.ledger_ops import (
        _ledger_lock,
        _load_dispatch_status,
        _records_root,
        _replace_record,
        _write_exclusive,
    )
    from .ledger.lifecycle import DispatchStatusRecord, LifecycleLedger

    ledger = LifecycleLedger(root)
    with contextlib.nullcontext() if locked else _ledger_lock(ledger):
        status = _load_dispatch_status(root, brief["dispatch_id"])
        attempt: JsonObject = {
            "dispatch_id": brief["dispatch_id"],
            "operation": operation,
            "evidence": evidence,
            "at": utils._now(),
            "remote": remote,
        }
        if remote:
            attempt["remote_url_sha256"] = _remote_url_sha256(repo, remote)
        relative = f"{_ATTEMPTS_DIRECTORY}{uuid.uuid4()}.json"
        _write_exclusive(ledger, _records_root(root) / relative, attempt)
        status["infrastructure_failure"] = {
            "path": relative,
            "sha256": operational_guards.policy_digest(attempt),
        }
        _replace_record(ledger, DispatchStatusRecord.from_dict(status))


def authorize_unsent(
    repo: Path, root: Path, batch: JsonObject, brief: JsonObject
) -> JsonObject:
    from . import operation_access, runtime_access
    from .core import config as core_config
    from .ledger.ledger_ops import _load_dispatch_status
    from .workflow.history import _latest_developer_candidate

    policy = _enabled_policy(brief)
    status = _load_dispatch_status(root, brief["dispatch_id"])
    failure = status.get("infrastructure_failure")
    if not isinstance(failure, dict):
        raise CoordinatorError(
            "no recorded infrastructure refusal for this dispatch",
            remedy="inspect the actual operation failure before retrying",
        )
    attempt = _operation_attempt(root, failure.get("path"))
    evidence = attempt.get("evidence", {})
    failed = [
        check
        for check in evidence.get("checks", [])
        if check.get("state") != "verified"
    ]
    if (
        failure.get("sha256") != operational_guards.policy_digest(attempt)
        or attempt.get("dispatch_id") != brief["dispatch_id"]
        or evidence.get("plan_digest") != brief["runtime_access"]["plan_digest"]
        or evidence.get("status") != "denied"
        or not failed
        or any(
            check.get("source") != "probe" or check.get("state") != "denied"
            for check in failed
        )
    ):
        raise CoordinatorError(
            "operation failure is not confirmed infrastructure",
            remedy="inspect the evidence; unknown, unsupported and changed plans need a manual decision",
        )
    operation = runtime_access.dispatch_operation(brief["role"], brief["purpose"])
    if operation not in {"qa", "publish"} or attempt["operation"] != operation:
        raise CoordinatorError(
            "operation changed for infrastructure retry",
            remedy="use a new manual approval for this operation",
        )
    config = core_config._config(repo)
    plan = runtime_access.resolve_plan(
        repo,
        Path(brief["worktree"]),
        config,
        brief["role"],
        brief["access"],
        operation=operation,
    )
    if (
        snapshot(config) != policy
        or plan != brief["runtime_access"]
        or _latest_developer_candidate(repo, root, batch) != brief["candidate_commit"]
    ):
        raise CoordinatorError(
            "infrastructure retry changed the approved boundaries",
            remedy="propose and approve the changed contract manually",
        )
    remote = attempt.get("remote")
    if remote and _remote_url_sha256(repo, remote) != attempt.get("remote_url_sha256"):
        raise CoordinatorError(
            "publish remote changed since its infrastructure refusal",
            remedy="use a new manual approval for the changed destination",
        )
    used = _retry_budget(batch, brief, policy)
    from harness.storage import storage_path

    ready = operation_access.require(
        repo,
        config,
        operation,
        brief=brief,
        remote=remote,
        checkout=storage_path(repo, "runs", "qa") if operation == "qa" else None,
    )
    return {
        "attempt": failure,
        "readiness": ready,
        "used_retries": used,
        "max_infrastructure_retries": policy["max_infrastructure_retries"],
    }


def retry_infrastructure_dispatch(args: argparse.Namespace) -> JsonObject:
    """Public recovery of an unsent operation refusal, via cancel + new immutable dispatch."""
    from .core.utils import _repo
    from .ledger.ledger_ops import (
        _ledger_lock,
        _load_batch,
        _load_dispatch,
        _state_root,
    )
    from .ledger.lifecycle import LifecycleLedger
    from .workflow.dispatch import cancel_dispatch, create_dispatch
    from .workflow.history import _validate_batch_integrity

    repo = _repo(args)
    root = _state_root(args, repo)
    with _ledger_lock(LifecycleLedger(root)):
        brief = _load_dispatch(root, args.dispatch)
        batch = _load_batch(root, brief["batch_id"])
        _validate_batch_integrity(root, batch)
        entries = batch["dispatches"]
        position = next(
            i
            for i, entry in enumerate(entries)
            if entry["dispatch_id"] == args.dispatch
        )
        entry = entries[position]
        cancelled = (
            entry.get("cancellation", {}).get("approved_by")
            == "policy:infrastructure-retry"
        )
        if cancelled and entries[position + 1 :]:
            return {
                "dispatch_id": entries[position + 1]["dispatch_id"],
                "state": "already-done",
            }
    try:
        if not cancelled:
            cancel_dispatch(
                argparse.Namespace(
                    repo=str(repo),
                    state_dir=getattr(args, "state_dir", None),
                    dispatch=args.dispatch,
                    approved_by=None,
                    approved_at=None,
                    reason="confirmed infrastructure retry",
                    _policy_infrastructure_retry=True,
                )
            )
        return create_dispatch(
            argparse.Namespace(
                repo=str(repo),
                state_dir=getattr(args, "state_dir", None),
                batch=batch["batch_id"],
                role=brief["role"],
                runtime=brief["resolved_runtime"],
                purpose=brief["purpose"],
                candidate_commit=brief["candidate_commit"],
                delta_review_of=None,
                model=brief["resolved_model"],
                effort=brief["resolved_effort"],
                propose=False,
                transition_digest=None,
                approved_by=None,
                approved_at=None,
                _policy_infrastructure_retry=True,
            )
        )
    except CoordinatorError as exc:
        stop_attention(repo, root, args.dispatch, exc)
        raise


def stop_attention(
    repo: Path,
    root: Path,
    dispatch_id: str,
    error: CoordinatorError,
    *,
    locked: bool = False,
) -> None:
    """Persist policy refusal without deciding the report or rewriting evidence."""
    from .operation_access import OperationAccessError

    reason = (
        error.attention_reason if isinstance(error, _RetryRefusal) else "unknown-reason"
    )
    if reason is None or (
        isinstance(error, OperationAccessError)
        and error.evidence.get("status") == "denied"
    ):
        return
    from .core import utils
    from .ledger.ledger_ops import (
        _ledger_lock,
        _load_batch,
        _load_dispatch,
        _replace_record,
    )
    from .ledger.lifecycle import BatchRecord, LifecycleLedger
    from .workflow.attention import _apply_attention

    ledger = LifecycleLedger(root)
    with contextlib.nullcontext() if locked else _ledger_lock(ledger):
        brief = _load_dispatch(root, dispatch_id)
        batch = _load_batch(root, brief["batch_id"])
        finding = operational_guards.attention_finding(
            reason,
            dispatch_id,
            last_safe_action=error.message,
            recommended_human_action=error.remedy,
        )
        policy = brief.get("orchestration_policy", {})
        config = {"extensions": policy.get("extensions", {})}
        if _apply_attention(config, batch, [finding], utils._now()):
            _replace_record(ledger, BatchRecord.from_dict(batch))
