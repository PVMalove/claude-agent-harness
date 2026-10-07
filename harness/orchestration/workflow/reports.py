"""What a role reports back, and what the coordinator records from it.

A completion report is immutable evidence: it is validated against the brief it answers, written
once as JSON and Markdown, and never rewritten.  Checkpoints and continuations live here too --
they are the same worker-to-coordinator channel, just before the work is finished.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import uuid
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatchcase
from functools import partial
from pathlib import Path, PurePosixPath
from typing import cast

from harness.errors import INTERNAL_INVARIANT_REMEDY
from harness.orchestration.contract import NO_GATE_WORK_ROLES
from harness.orchestration.core import config as core_config
from harness.orchestration.core import utils
from harness.orchestration.core.config import (
    _adaptive_continuation_policy,
    _context_advisory,
    _continuation_policy,
    _execution_policy,
    _reject_sensitive,
    _role,
)
from harness.orchestration.core.constants import (
    CHECKPOINT_INPUT_FIELDS,
    CHECKPOINT_NO_CONTEXT_PACKAGE,
    CONTINUATION_FACTS_FIELDS,
    FINDING_SEVERITIES,
    INCOMPLETE_ITEM_FIELDS,
    INCOMPLETE_ITEM_OPTIONAL_FIELDS,
    INCOMPLETE_ITEM_TARGET_ROLES,
    LIVE_DISPATCH_STATES,
    MAX_CHECK_EVIDENCE_CHARS,
    PLANNED_TRIGGER_KINDS,
    PLANNED_TRIGGER_THRESHOLD_KEY,
    RATE_LIMIT_TERMINATION_REASONS,
    REPORT_FIELDS,
    REPORT_OPTIONAL_FIELDS,
    REPORT_OUTCOMES,
    REVIEW_SEVERITIES,
    TELEMETRY_FIELDS,
    TOOLING_BLOCKER_FIELDS,
    TOOLING_BLOCKER_UNCOMMITTED_FIELD,
)
from harness.orchestration.core.git_utils import (
    _candidate_commit,
    _changed_files_between,
    _commit_changed_files,
    _commits_between,
    _git_is_ancestor,
)
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _canonical,
    _moment,
    _non_empty,
    _read_object,
    _repo,
    _safe_id,
    _sanitise,
    _strings,
)
from harness.orchestration.core.workspace import (
    _agent_authored_file,
    _reject_non_english,
)
from harness.orchestration.ledger.ledger_ops import (
    _ledger_lock,
    _load_batch,
    _load_dispatch,
    _load_dispatch_status,
    _records_root,
    _replace_record,
    _state_root,
    _write_exclusive,
    _write_record,
    _write_text_exclusive,
)
from harness.orchestration.ledger.lifecycle import (
    BatchRecord,
    CheckpointRecord,
    DispatchStatusRecord,
    LedgerError,
    LifecycleLedger,
)
from harness.orchestration.runtime_attestation import (
    AttestationError,
)
from harness.orchestration.runtime_attestation import (
    attest as attest_runtime_worktree,
)
from harness.orchestration.workflow import carried_items
from harness.orchestration.workflow import commit_plan as plan_rules
from harness.orchestration.workflow.approval import (
    _approval,
)
from harness.orchestration.workflow.history import (
    _latest_checkpoint_for_dispatch,
    _latest_context_package,
    _live_status,
    _validate_batch_integrity,
    _validate_dispatch,
)
from harness.orchestration.workflow.risk import (
    _risk_triggers,
    _validate_trigger_names,
)


def _continuation_counts(batch: JsonObject, dispatch_id: str) -> tuple[int, int]:
    decisions = [
        decision
        for decision in batch.get("coordinator_decisions", [])
        if decision.get("dispatch_id") == dispatch_id
        and decision.get("decision") in {"continue", "continue-automatic"}
    ]
    automatic = sum(
        1 for decision in decisions if decision.get("decision") == "continue-automatic"
    )
    return len(decisions), automatic


def self_report_dispatch(args: argparse.Namespace) -> JsonObject:
    """Compare the model a dispatched role is actually running against its immutable brief.

    A mismatch blocks the dispatch here, at first contact, instead of letting a misconfigured
    worker spend the coordinator's budget producing nothing."""
    repo = _repo(args)
    root = _state_root(args, repo)
    reported = args.model.strip() if _non_empty(args.model) else ""
    if not reported:
        raise CoordinatorError(
            "a model self-report must name the actually active model",
            remedy="pass --model naming the model actually running this session",
        )
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        dispatch, status = _live_status(root, args.dispatch)
        expected = dispatch["resolved_model"]
        model_matched = reported == expected
        attestation: JsonObject | None = None
        worktree_matched = True
        if dispatch.get("worker_attestation_required", False):
            supplied_worktree = getattr(args, "worktree", None)
            if not _non_empty(supplied_worktree):
                worktree_matched = False
                attestation = {
                    "match": False,
                    "error": "runtime worktree attestation is required",
                }
            else:
                try:
                    attestation = {
                        "match": True,
                        **attest_runtime_worktree(repo, dispatch, supplied_worktree),
                    }
                except AttestationError as exc:
                    worktree_matched = False
                    attestation = {"match": False, "error": str(exc)}
        matched = model_matched and worktree_matched
        moment = utils._now()
        status["state"] = "working" if matched else "blocked"
        status["updated_at"] = moment
        status["heartbeat_at"] = moment
        status["model_self_report"] = {
            "reported_model": reported,
            "expected_model": expected,
            "match": model_matched,
            "reported_at": moment,
        }
        if attestation is not None:
            status["worktree_attestation"] = {**attestation, "reported_at": moment}
        _safe_id(dispatch["dispatch_id"], "dispatch")
        _replace_record(ledger, DispatchStatusRecord.from_dict(status))
        if not matched:
            batch = _load_batch(root, dispatch["batch_id"])
            for entry in batch.get("dispatches", []):
                if entry["dispatch_id"] == dispatch["dispatch_id"]:
                    entry["state"] = "blocked"
            batch["state"] = "blocked"
            _safe_id(batch["batch_id"], "batch")
            _replace_record(ledger, BatchRecord.from_dict(batch))
    if not matched:
        mismatch = []
        if not model_matched:
            mismatch.append(
                f"running {reported!r} but approved brief resolved {expected!r}"
            )
        if not worktree_matched:
            assert (
                attestation is not None
            )  # a mismatched worktree always sets the attestation
            mismatch.append(str(attestation["error"]))
        raise CoordinatorError(
            "dispatch "
            + "; ".join(mismatch)
            + "; the dispatch is blocked and needs a new coordinator decision",
            remedy="resolve the listed mismatch(es) and get a fresh coordinator decision before continuing this dispatch",
        )
    return {
        "dispatch_id": dispatch["dispatch_id"],
        "state": "working",
        "model": reported,
        "worktree": attestation.get("worktree") if attestation else None,
    }


def heartbeat_dispatch(args: argparse.Namespace) -> JsonObject:
    repo = _repo(args)
    root = _state_root(args, repo)
    note = args.note.strip() if _non_empty(args.note) else "none"
    _reject_sensitive({"note": note}, "dispatch heartbeat")
    context_tokens = args.context_tokens
    context_source = args.context_source
    if (context_tokens is None) != (context_source is None):
        raise CoordinatorError(
            "--context-tokens and --context-source must be given together",
            remedy="pass --context-tokens together with --context-source",
        )
    if context_tokens is not None and (
        isinstance(context_tokens, bool)
        or not isinstance(context_tokens, int)
        or context_tokens < 0
    ):
        raise CoordinatorError(
            "--context-tokens must be a non-negative integer",
            remedy="pass --context-tokens as a non-negative integer",
        )
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        dispatch, status = _live_status(root, args.dispatch)
        moment = utils._now()
        status["updated_at"] = moment
        status["heartbeat_at"] = moment
        status["heartbeat_note"] = _sanitise(note)[:240]
        if context_tokens is not None:
            status["context_tokens"] = context_tokens
            status["context_source"] = context_source
        _safe_id(dispatch["dispatch_id"], "dispatch")
        _replace_record(ledger, DispatchStatusRecord.from_dict(status))
    result = {
        "dispatch_id": dispatch["dispatch_id"],
        "state": status["state"],
        "heartbeat_at": moment,
    }
    if context_tokens is not None:
        result["context_tokens"] = context_tokens
        result["context_source"] = context_source
    return result


def rate_limited_dispatch(args: argparse.Namespace) -> JsonObject:
    """Record a provider 429 without model-side polling or a lost checkpoint."""
    repo = _repo(args)
    root = _state_root(args, repo)
    retry_after = (
        args.retry_after_seconds
        if args.retry_after_seconds is not None
        else _execution_policy(core_config._config(repo))["rate_limit_retry_seconds"]
    )
    if (
        isinstance(retry_after, bool)
        or not isinstance(retry_after, int)
        or retry_after < 1
    ):
        raise CoordinatorError(
            "retry-after-seconds must be a positive integer",
            remedy="pass --retry-after-seconds as a positive integer",
        )
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        dispatch = _load_dispatch(root, args.dispatch)
        batch = _load_batch(root, dispatch["batch_id"])
        _validate_batch_integrity(root, batch)
        _latest_checkpoint_for_dispatch(root, batch, dispatch["dispatch_id"])
        status = _load_dispatch_status(root, dispatch["dispatch_id"])
        entry = next(
            (
                item
                for item in batch["dispatches"]
                if item["dispatch_id"] == dispatch["dispatch_id"]
            ),
            None,
        )
        if (
            not entry
            or entry.get("state") != "checkpointed"
            or status.get("state") != "checkpointed"
        ):
            raise CoordinatorError(
                "rate_limited requires a checkpointed write dispatch",
                remedy="only mark a checkpointed write dispatch as rate-limited",
            )
        retry_not_before = (
            datetime.now(UTC) + timedelta(seconds=retry_after)
        ).isoformat()
        status.update(
            {
                "state": "rate_limited",
                "updated_at": utils._now(),
                "retry_not_before": retry_not_before,
                "last_event": "rate_limited",
            }
        )
        entry["state"] = "rate_limited"
        batch.setdefault("liveness_events", []).append(
            {
                "dispatch_id": dispatch["dispatch_id"],
                "event": "rate_limited",
                "recorded_at": utils._now(),
                "retry_not_before": retry_not_before,
            }
        )
        _safe_id(dispatch["dispatch_id"], "dispatch")
        _replace_record(ledger, DispatchStatusRecord.from_dict(status))
        _safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, BatchRecord.from_dict(batch))
    return {
        "dispatch_id": dispatch["dispatch_id"],
        "event": "rate_limited",
        "retry_not_before": retry_not_before,
    }


def record_telemetry(args: argparse.Namespace) -> JsonObject:
    """Attach source-observed worker/coordinator metrics to the batch audit trail.

    Missing provider fields stay ``null`` rather than becoming estimated zeroes. This command is
    intentionally data-only: it cannot alter role state, scheduling, approvals or model routing.
    """
    repo = _repo(args)
    root = _state_root(args, repo)
    payload = _read_object(Path(args.file), "telemetry payload")
    if set(payload) != TELEMETRY_FIELDS:
        raise CoordinatorError(
            "telemetry payload schema mismatch",
            remedy="the telemetry payload schema is malformed -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    _reject_sensitive(payload, "telemetry payload")
    if payload["session_kind"] not in {"worker", "coordinator"}:
        raise CoordinatorError(
            "telemetry session_kind must be worker or coordinator",
            remedy="set session_kind to 'worker' or 'coordinator'",
        )
    if not _non_empty(payload["restart_reason"]) or not _non_empty(
        payload["recorded_at"]
    ):
        raise CoordinatorError(
            "telemetry restart_reason and recorded_at must be non-empty strings",
            remedy="set restart_reason and recorded_at to non-empty strings",
        )
    _moment(payload["recorded_at"], "telemetry recorded_at")
    for field in TELEMETRY_FIELDS - {
        "dispatch_id",
        "session_kind",
        "restart_reason",
        "recorded_at",
    }:
        value = payload[field]
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise CoordinatorError(
                f"telemetry {field} must be a non-negative integer or null",
                remedy=f"pass telemetry {field} as a non-negative integer or omit it",
            )
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        dispatch = _load_dispatch(root, payload["dispatch_id"])
        batch = _load_batch(root, dispatch["batch_id"])
        _validate_batch_integrity(root, batch)
        record = dict(payload)
        record["telemetry_id"] = f"telemetry-{uuid.uuid4()}"
        record["record_sha256"] = hashlib.sha256(
            _canonical(record).encode("utf-8")
        ).hexdigest()
        batch.setdefault("telemetry", []).append(record)
        _safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, BatchRecord.from_dict(batch))
    advisory = _context_advisory(
        core_config._config(repo), payload["max_context_tokens"]
    )
    return {
        "telemetry_id": record["telemetry_id"],
        "dispatch_id": payload["dispatch_id"],
        "context_advisory": advisory,
    }


def _validate_checkpoint(
    checkpoint: JsonObject,
    dispatch: JsonObject,
    role: JsonObject,
    repo: Path,
    base_commit: str | None,
    root: Path,
    batch: JsonObject,
) -> None:
    _reject_sensitive(checkpoint, "checkpoint")
    if set(checkpoint) != CHECKPOINT_INPUT_FIELDS:
        raise CoordinatorError(
            "checkpoint schema mismatch",
            remedy="the checkpoint schema is malformed -- " + INTERNAL_INVARIANT_REMEDY,
        )
    if checkpoint["dispatch_id"] != dispatch["dispatch_id"]:
        raise CoordinatorError(
            "checkpoint dispatch_id does not match the dispatched role",
            remedy="the checkpoint dispatch_id does not match the dispatched role -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    if role["mode"] != "write":
        raise CoordinatorError(
            "a checkpoint is only valid for a write role; a read-only role cannot span multiple worker sessions",
            remedy="only checkpoint a write role; a read-only role completes in a single worker session",
        )
    for field in ("risks", "blockers"):
        if not _non_empty(checkpoint[field]):
            raise CoordinatorError(
                f"checkpoint {field} must be a non-empty string",
                remedy=f"set checkpoint {field} to a non-empty string",
            )
    commit_sha = checkpoint["commit_sha"]
    if (
        not isinstance(commit_sha, str)
        or re.fullmatch(r"[0-9a-fA-F]{7,64}", commit_sha) is None
    ):
        raise CoordinatorError(
            "checkpoint requires a commit_sha",
            remedy="pass a commit_sha in the checkpoint payload",
        )
    changed_files = _strings(
        checkpoint["changed_files"], "checkpoint changed_files", allow_empty=True
    )
    paths = dispatch["write_paths"]
    for changed_file in changed_files:
        normalized = changed_file.replace("\\", "/")
        if (
            normalized.startswith("/")
            or ".." in Path(normalized).parts
            or not any(fnmatchcase(normalized, pattern) for pattern in paths)
        ):
            raise CoordinatorError(
                "checkpoint changed_files must remain inside the approved zone",
                remedy="keep checkpoint changed_files inside the role's approved write zone",
            )
    resolved = _candidate_commit(repo, commit_sha)
    actual_files = (
        _changed_files_between(repo, base_commit, resolved)
        if base_commit
        else _commit_changed_files(repo, resolved)
    )
    if actual_files != changed_files:
        raise CoordinatorError(
            "checkpoint changed_files must exactly match commit_sha",
            remedy="regenerate changed_files from the actual diff at commit_sha",
        )
    remaining = _strings(
        checkpoint["remaining_definition_of_done"],
        "checkpoint remaining_definition_of_done",
        allow_empty=True,
    )
    if not set(remaining) <= set(dispatch["definition_of_done"]):
        raise CoordinatorError(
            "checkpoint remaining_definition_of_done must be drawn from the dispatch definition_of_done",
            remedy="only list remaining_definition_of_done items that are part of the dispatch's own definition_of_done",
        )
    passing_checks = checkpoint["passing_checks"]
    if not isinstance(passing_checks, list):
        raise CoordinatorError(
            "checkpoint passing_checks must be a list",
            remedy="set passing_checks to a list",
        )
    for check in passing_checks:
        if not isinstance(check, dict) or set(check) != {
            "command",
            "result",
            "evidence",
        }:
            raise CoordinatorError(
                "checkpoint passing_checks has an invalid entry",
                remedy="fix the malformed passing_checks entry",
            )
        if not all(
            _non_empty(check[field]) for field in ("command", "result", "evidence")
        ):
            raise CoordinatorError(
                "checkpoint passing_checks entries must contain text evidence",
                remedy="each passing_checks entry must contain text evidence of the check result",
            )
        if check["command"] not in dispatch["verification_commands"]:
            raise CoordinatorError(
                "checkpoint passing_checks must reference an approved verification command",
                remedy="each passing_checks entry must reference one of the dispatch's approved verification commands",
            )
    package = _latest_context_package(root, batch)
    if package is None:
        if checkpoint["context_package_id"] != CHECKPOINT_NO_CONTEXT_PACKAGE:
            raise CoordinatorError(
                "checkpoint context_package_id must be the no-package sentinel; "
                "the batch has no registered context package",
                remedy="leave checkpoint context_package_id as the no-package sentinel; this batch has no registered context package",
            )
    elif checkpoint["context_package_id"] != package["context_package_id"]:
        raise CoordinatorError(
            "checkpoint context_package_id must reference the batch's latest registered context package",
            remedy="reference the batch's latest registered context package, or omit context_package_id",
        )


def checkpoint_dispatch(args: argparse.Namespace) -> JsonObject:
    """Record a non-terminal checkpoint for an in-flight write-role dispatch so its worker session
    can end here and a fresh session can resume the same dispatch later.

    A checkpoint is deliberately not a completion report: it never touches the outcome enum or the
    reporting path, and it is rejected outright for a read-only role, which may never span more
    than one worker session."""
    repo = _repo(args)
    root = _state_root(args, repo)
    checkpoint = _read_object(
        _agent_authored_file(repo, args.file, "a checkpoint"), "checkpoint"
    )
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        dispatch, status = _live_status(root, cast(str, checkpoint.get("dispatch_id")))
        batch = _load_batch(root, dispatch["batch_id"])
        _validate_batch_integrity(root, batch)
        config = core_config._config(repo)
        _validate_dispatch(repo, config, root, batch, dispatch)
        role = _role(repo, dispatch["role"])
        entry = next(
            (
                item
                for item in batch.get("dispatches", [])
                if item["dispatch_id"] == dispatch["dispatch_id"]
            ),
            None,
        )
        if not entry or entry.get("state") != "dispatched":
            raise CoordinatorError(
                "a checkpoint requires a dispatched role",
                remedy="only checkpoint a dispatched role",
            )
        self_report = status.get("model_self_report")
        if not isinstance(self_report, dict) or self_report.get("match") is not True:
            raise CoordinatorError(
                "a dispatched role must confirm its active model before checkpointing",
                remedy="pass --model naming the model actually running this session before checkpointing",
            )
        _validate_checkpoint(
            checkpoint, dispatch, role, repo, batch.get("base_commit"), root, batch
        )
        record = {
            "checkpoint_id": f"checkpoint-{uuid.uuid4()}",
            "batch_id": batch["batch_id"],
            "created_at": utils._now(),
            **checkpoint,
        }
        _safe_id(record["checkpoint_id"], "checkpoint")
        _write_record(ledger, CheckpointRecord.from_dict(record))
        entry["state"] = "checkpointed"
        # Batch-level pointer, the same ownership pattern as risk_assessments/context_packages;
        # dispatch_id is carried alongside since a checkpoint is dispatch-scoped, not batch-scoped.
        batch.setdefault("checkpoints", []).append(
            {
                "checkpoint_id": record["checkpoint_id"],
                "dispatch_id": dispatch["dispatch_id"],
                "record_sha256": hashlib.sha256(
                    _canonical(record).encode("utf-8")
                ).hexdigest(),
            }
        )
        _safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, BatchRecord.from_dict(batch))
        moment = utils._now()
        status.update({"state": "checkpointed", "updated_at": moment})
        _safe_id(dispatch["dispatch_id"], "dispatch")
        _replace_record(ledger, DispatchStatusRecord.from_dict(status))
    return {
        "dispatch_id": dispatch["dispatch_id"],
        "state": "checkpointed",
        "checkpoint_id": record["checkpoint_id"],
    }


def _authorize_rate_limit_continuation(termination_reason: str) -> JsonObject:
    return {
        "decision": "continue-automatic",
        "approved_by": "runtime-adapter",
        "approved_at": utils._now(),
        "note": f"termination_reason={termination_reason}",
    }


def _authorize_planned_continuation(
    config: JsonObject,
    dispatch: JsonObject,
    checkpoint: JsonObject,
    args: argparse.Namespace,
) -> JsonObject:
    """A planned trigger (context limit, N TDD cycles, a large failure log, or a completed
    vertical slice) is a safe-default-toward-approval path: it always requires the same explicit
    coordinator decision an accept/retry/block/fail already does, reusing that record type rather
    than inventing a new one."""
    trigger = args.trigger.strip() if _non_empty(args.trigger) else ""
    if trigger not in PLANNED_TRIGGER_KINDS:
        raise CoordinatorError(
            "a continuation without a recognized rate-limit termination reason is a planned trigger and "
            f"requires --trigger to be one of {sorted(PLANNED_TRIGGER_KINDS)}",
            remedy="pass --trigger naming one of the configured planned-trigger kinds",
        )
    threshold_key = PLANNED_TRIGGER_THRESHOLD_KEY.get(trigger)
    if threshold_key is not None:
        threshold = _adaptive_continuation_policy(config)[threshold_key]
        measured = args.measured_value
        if (
            isinstance(measured, bool)
            or not isinstance(measured, int)
            or measured < threshold
        ):
            raise CoordinatorError(
                f"planned trigger {trigger!r} requires --measured-value at least the configured "
                f"threshold ({threshold})",
                remedy=f"pass --measured-value >= {threshold} for planned trigger {trigger!r}",
            )
    _check_continuation_facts_unchanged(dispatch, checkpoint, args)
    approval = _approval(args)
    return {
        "decision": "continue",
        "approved_by": approval["approved_by"],
        "approved_at": approval["approved_at"],
        "note": args.note.strip()
        if _non_empty(args.note)
        else f"planned trigger: {trigger}",
    }


def _check_continuation_facts_unchanged(
    dispatch: JsonObject,
    checkpoint: JsonObject,
    args: argparse.Namespace,
) -> None:
    """Reject a continuation whose recorded facts show scope, Definition of Done, risks, or
    dependencies changed since the checkpoint (Issue #140's continuation-authorization contract).
    The coordinator must restate those facts explicitly -- a mismatch means real drift, not
    something the coordinator can wave through, so it must close the dispatch and open a new one
    through ordinary approval instead. Blockers are deliberately not compared here: unlike the
    other four, their wording can legitimately evolve session to session without the underlying
    scope, DoD, risks or dependencies having changed at all."""
    if not _non_empty(getattr(args, "file", None)):
        raise CoordinatorError(
            "a planned-trigger continuation requires --file restating the current remaining "
            "Definition of Done, risks and dependencies",
            remedy="pass --file with the continuation facts (remaining Definition of Done, risks and dependencies)",
        )
    facts = _read_object(Path(args.file).resolve(), "continuation facts")
    _reject_sensitive(facts, "continuation facts")
    if set(facts) != CONTINUATION_FACTS_FIELDS:
        raise CoordinatorError(
            "continuation facts schema mismatch",
            remedy="the continuation facts schema is malformed -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    if facts["dispatch_id"] != dispatch["dispatch_id"]:
        raise CoordinatorError(
            "continuation facts dispatch_id does not match the dispatched role",
            remedy="the continuation facts dispatch_id does not match the dispatched role -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    unchanged = (
        facts["remaining_definition_of_done"]
        == checkpoint["remaining_definition_of_done"]
        and facts["risks"] == checkpoint["risks"]
        and facts["dependencies"] == dispatch["dependencies"]
    )
    if not unchanged:
        raise CoordinatorError(
            "continuation facts differ from the checkpointed scope, Definition of Done, risks, blockers "
            "or dependencies; close this dispatch and open a new one through ordinary approval instead "
            "of resuming it",
            remedy="open a new dispatch through ordinary approval instead of resuming one whose scope has drifted",
        )


def resume_dispatch(args: argparse.Namespace) -> JsonObject:
    """Start a new worker session for a checkpointed dispatch, under the same dispatch ID.

    The resumed session is put through the exact same liveness contract as a first session: its
    prior model self-report is discarded, so `dispatch self-report` and `dispatch heartbeat` are
    both mandatory again before any further checkpoint or completion report.

    Continuation authorization (Issue #140): a runtime adapter reporting a recognized rate-limit
    termination reason authorizes the new session automatically, without a new human/coordinator
    decision. Anything else -- no reason, or one this coordinator does not recognize -- is treated
    as a planned trigger and requires the existing coordinator-decision approval, with its recorded
    facts checked against the checkpoint for drift."""
    repo = _repo(args)
    root = _state_root(args, repo)
    termination_reason = (
        args.termination_reason.strip().lower()
        if _non_empty(args.termination_reason)
        else ""
    )
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        dispatch = _load_dispatch(root, args.dispatch)
        batch = _load_batch(root, dispatch["batch_id"])
        _validate_batch_integrity(root, batch)
        config = core_config._config(repo)
        _validate_dispatch(repo, config, root, batch, dispatch)
        status = _load_dispatch_status(root, dispatch["dispatch_id"])
        entry = next(
            (
                item
                for item in batch.get("dispatches", [])
                if item["dispatch_id"] == dispatch["dispatch_id"]
            ),
            None,
        )
        resumable_rate_limit = (
            entry
            and entry.get("state") == "rate_limited"
            and status.get("state") == "rate_limited"
        )
        if (
            not entry
            or (entry.get("state") != "checkpointed" and not resumable_rate_limit)
            or (status.get("state") != "checkpointed" and not resumable_rate_limit)
        ):
            raise CoordinatorError(
                "only a checkpointed or rate-limited dispatch may start a new worker session",
                remedy="only start a new worker session for a checkpointed or rate-limited dispatch",
            )
        continuation_policy = _continuation_policy(config)
        continuation_count, rate_limit_count = _continuation_counts(
            batch, dispatch["dispatch_id"]
        )
        if continuation_count >= continuation_policy["max_continuations"]:
            raise CoordinatorError(
                "continuation budget is exhausted for this dispatch; submit a final report or block for a new scoped batch",
                remedy="submit a final completion report, or block this batch for a new, newly scoped batch",
            )
        if termination_reason in RATE_LIMIT_TERMINATION_REASONS:
            if rate_limit_count >= continuation_policy["max_rate_limit_resumes"]:
                raise CoordinatorError(
                    "automatic rate-limit resume budget is exhausted; require a newly scoped batch instead of looping",
                    remedy="require a newly scoped batch instead of looping rate-limit resumes",
                )
            retry_not_before = status.get("retry_not_before")
            if isinstance(retry_not_before, str) and _moment(
                retry_not_before, "retry_not_before"
            ) > datetime.now(UTC):
                raise CoordinatorError(
                    "rate-limit retry window has not elapsed",
                    remedy="wait for the recorded retry-after window to elapse before resuming",
                )
            authorization = _authorize_rate_limit_continuation(termination_reason)
        else:
            checkpoint = _latest_checkpoint_for_dispatch(
                root, batch, dispatch["dispatch_id"]
            )
            authorization = _authorize_planned_continuation(
                config, dispatch, checkpoint, args
            )
        entry["state"] = "dispatched"
        batch.setdefault("coordinator_decisions", []).append(
            {
                "dispatch_id": dispatch["dispatch_id"],
                **authorization,
            }
        )
        _safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, BatchRecord.from_dict(batch))
        moment = utils._now()
        _safe_id(dispatch["dispatch_id"], "dispatch")
        _replace_record(
            ledger,
            DispatchStatusRecord.from_dict(
                {
                    "dispatch_id": dispatch["dispatch_id"],
                    "state": "dispatched",
                    "updated_at": moment,
                    "heartbeat_at": moment,
                    "last_event": "resumed",
                }
            ),
        )
    return {
        "dispatch_id": dispatch["dispatch_id"],
        "state": "dispatched",
        "authorization": authorization["decision"],
        "next_role_action": "dispatch self-report",
    }


def _persist_report(
    ledger: LifecycleLedger,
    root: Path,
    batch: JsonObject,
    dispatch: JsonObject,
    report: JsonObject,
    *,
    auto_accept_policy: str | None = None,
) -> Path:
    """Persist a role report and advance its batch atomically under the coordinator lock.

    ``auto_accept_policy`` is the policy ``report submit`` found authorized to decide the report; it
    is kept on the dispatch status so a stopped policy chain is completed with that same policy."""
    report_json = _records_root(root) / "reports" / f"{dispatch['dispatch_id']}.json"
    report_md = _records_root(root) / "reports" / f"{dispatch['dispatch_id']}.md"
    if report_json.exists() or report_md.exists():
        raise CoordinatorError(
            "refusing to overwrite immutable completion report",
            remedy="a completion report already exists at this immutable path -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    _write_exclusive(ledger, report_json, report)
    try:
        _write_text_exclusive(ledger, report_md, _report_markdown(report))
    except CoordinatorError as exc:
        try:
            ledger.delete(report_json, reason="discard incomplete immutable report")
        except LedgerError as cleanup:
            raise CoordinatorError(cleanup.message, remedy=cleanup.remedy) from exc
        raise CoordinatorError(
            "refusing to overwrite immutable Markdown report",
            remedy=f"a Markdown report already exists at this immutable path -- {INTERNAL_INVARIANT_REMEDY}",
        ) from exc
    entry = next(
        (
            item
            for item in batch["dispatches"]
            if item["dispatch_id"] == dispatch["dispatch_id"]
        ),
        None,
    )
    if entry is None:
        raise CoordinatorError(
            "dispatch is not registered in its batch",
            remedy="the dispatch is not registered in its batch -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    entry["state"] = "reported"
    entry["report"] = f"reports/{dispatch['dispatch_id']}.json"
    entry["report_sha256"] = hashlib.sha256(
        _canonical(report).encode("utf-8")
    ).hexdigest()
    batch["state"] = "awaiting-approval"
    closed = _load_dispatch_status(root, dispatch["dispatch_id"])
    closed.update(
        {
            "dispatch_id": dispatch["dispatch_id"],
            "state": "reported",
            "updated_at": utils._now(),
        }
    )
    if auto_accept_policy is not None:
        closed["auto_accept_policy"] = auto_accept_policy
    _safe_id(dispatch["dispatch_id"], "dispatch")
    _replace_record(ledger, DispatchStatusRecord.from_dict(closed))
    _safe_id(batch["batch_id"], "batch")
    _replace_record(ledger, BatchRecord.from_dict(batch))
    return report_json


def _validate_review(review: object, dispatch: JsonObject) -> None:
    # ``carried_items`` (issue #499) is optional: the review's account of the brief's carried items.
    if not isinstance(review, dict) or set(review) - {"carried_items"} != {
        "candidate_commit",
        "scope",
        "standards",
        "spec",
    }:
        raise CoordinatorError(
            "composite review must contain independent standards and spec evidence",
            remedy="submit composite review evidence with independent standards and spec findings",
        )
    if (
        review["candidate_commit"] != dispatch["candidate_commit"]
        or review["scope"] != dispatch["review_scope"]
    ):
        raise CoordinatorError(
            "composite review evidence does not match the approved review brief",
            remedy="regenerate composite review evidence from the actual approved review brief",
        )
    delta_review_of = dispatch.get("delta_review_of")
    delta_review_axis = dispatch.get("delta_review_axis")
    for axis in ("standards", "spec"):
        evidence = review[axis]
        inherited = delta_review_of is not None and axis != delta_review_axis
        expected_keys = (
            {"severity", "findings", "risks", "blockers", "inherited_from"}
            if inherited
            else {"severity", "findings", "risks", "blockers"}
        )
        if not isinstance(evidence, dict) or set(evidence) != expected_keys:
            raise CoordinatorError(
                f"composite review {axis} evidence has an invalid schema",
                remedy=f"resubmit composite review {axis} evidence with a valid schema",
            )
        if inherited:
            if evidence["severity"] != "clean" or evidence["findings"] != []:
                raise CoordinatorError(
                    f"composite review {axis} must inherit the prior Clean verdict without re-analysis",
                    remedy=f"set composite review {axis} severity=clean and findings=[] to inherit a delta-review's prior Clean verdict",
                )
            if evidence["inherited_from"] != delta_review_of:
                raise CoordinatorError(
                    f"composite review {axis} must reference the prior review as evidence",
                    remedy=f"composite review {axis} must cite the prior review as evidence for a delta-review",
                )
            if not _non_empty(evidence["risks"]) or not _non_empty(
                evidence["blockers"]
            ):
                raise CoordinatorError(
                    f"composite review {axis} must state risks and blockers",
                    remedy=f"composite review {axis} must state its risks and blockers explicitly",
                )
            continue
        if evidence["severity"] not in REVIEW_SEVERITIES:
            raise CoordinatorError(
                f"composite review {axis} severity is invalid",
                remedy=f"set composite review {axis} severity to a recognized value",
            )
        if not isinstance(evidence["findings"], list):
            raise CoordinatorError(
                f"composite review {axis} findings must be a list",
                remedy=f"set composite review {axis} findings to a list",
            )
        highest = "none"
        for finding in evidence["findings"]:
            if not isinstance(finding, dict) or set(finding) != {
                "severity",
                "summary",
                "evidence",
            }:
                raise CoordinatorError(
                    f"composite review {axis} finding has an invalid schema",
                    remedy=f"fix the malformed composite review {axis} finding",
                )
            if finding["severity"] not in FINDING_SEVERITIES or not all(
                _non_empty(finding[field]) for field in ("summary", "evidence")
            ):
                raise CoordinatorError(
                    f"composite review {axis} finding is invalid",
                    remedy=f"fix the invalid composite review {axis} finding",
                )
            if finding["severity"] == "blocker":
                highest = "blocker"
            elif finding["severity"] == "warning" and highest == "none":
                highest = "warning"
        if highest == "blocker" and evidence["severity"] != "blocker":
            raise CoordinatorError(
                f"composite review {axis} hides a blocker finding",
                remedy=f"composite review {axis} must disclose every blocker finding, not hide it",
            )
        if highest == "warning" and evidence["severity"] in {"none", "clean"}:
            raise CoordinatorError(
                f"composite review {axis} hides a warning finding",
                remedy=f"composite review {axis} must disclose every warning finding, not hide it",
            )
        if not _non_empty(evidence["risks"]) or not _non_empty(evidence["blockers"]):
            raise CoordinatorError(
                f"composite review {axis} must state risks and blockers",
                remedy=f"composite review {axis} must state its risks and blockers explicitly",
            )
    carried_items.validate_review_accounting(review, dispatch)


def _rebase_target(batch: JsonObject, dispatch: JsonObject) -> str | None:
    """The integration tip a developer dispatch must rebase onto, when a stale-base block is open."""
    target = batch.get("rebase_target_commit")
    if (
        dispatch.get("role") == "developer"
        and batch.get("base_rebase_required")
        and isinstance(target, str)
    ):
        return target
    return None


def _closure_base(
    repo: Path, root: Path, batch: JsonObject, dispatch: JsonObject
) -> str | None:
    """The commit a developer-retry's carried_item_closure counts commits from (issue #503): the
    oldest snapshot of its retry chain that its own ``snapshot_commit`` still descends from, so a
    rebase inside the chain never lets a closure name upstream commits; ``None`` when none is owed.
    """
    chain = carried_items.closure_snapshots(root, batch, dispatch)
    return next(
        (base for base in chain if _git_is_ancestor(repo, base, chain[-1])), None
    )


def _require_fix_forward(repo: Path, dispatch: JsonObject, candidate: str) -> None:
    """A developer-retry continues its ``snapshot_commit`` (issue #503): without an approved rebase
    target, the reported candidate must descend from it, so the retry rewrote no history."""
    snapshot = dispatch.get("snapshot_commit")
    if isinstance(snapshot, str) and not _git_is_ancestor(repo, snapshot, candidate):
        raise CoordinatorError(
            f"developer-retry candidate {candidate} does not descend from snapshot_commit "
            f"{snapshot}: the retry rewrote the history it continues (amend, squash or reset)",
            remedy=f"a fix-forward adds new commits on top of snapshot_commit {snapshot}: recover "
            "the rewritten commits from git reflog, re-apply the fix as new commits without "
            "amend or squash, and report the new HEAD",
        )


def _validate_tooling_blocker(report: JsonObject, dispatch: JsonObject) -> None:
    """``tooling_blocker`` is the structured evidence of a tool that blocked a legitimate action
    (issue #500): exactly ``tool``, ``command`` and ``message``, each a bounded non-empty string, on a
    ``blocked`` report only. It alone lets the coordinator classify a retry as ``tooling``.

    A developer whose commit the tool blocked also lists the files it left uncommitted (issue #502),
    each inside its write zone; the restart's preflight holds the worktree to exactly that list."""
    if "tooling_blocker" not in report:
        return
    if report["outcome"] != "blocked":
        raise CoordinatorError(
            "completion report tooling_blocker is allowed only on a blocked report",
            remedy="set outcome to blocked for a tool that blocked the role, or drop tooling_blocker",
        )
    blocker = report["tooling_blocker"]
    if isinstance(blocker, dict) and TOOLING_BLOCKER_UNCOMMITTED_FIELD in blocker:
        if report["role"] != "developer":
            raise CoordinatorError(
                f"completion report tooling_blocker {TOOLING_BLOCKER_UNCOMMITTED_FIELD} is allowed only on a developer report",
                remedy=f"drop tooling_blocker {TOOLING_BLOCKER_UNCOMMITTED_FIELD}: only a developer whose commit a tool blocked lists them",
            )
        _validate_uncommitted_files(
            blocker[TOOLING_BLOCKER_UNCOMMITTED_FIELD], dispatch["write_paths"]
        )
        blocker = {
            key: value
            for key, value in blocker.items()
            if key != TOOLING_BLOCKER_UNCOMMITTED_FIELD
        }
    _check_tooling_blocker_shape(
        blocker,
        "completion report tooling_blocker",
        "tooling_blocker",
    )


def _validate_uncommitted_files(value: object, write_paths: list[str]) -> None:
    label = f"completion report tooling_blocker {TOOLING_BLOCKER_UNCOMMITTED_FIELD}"
    files = _strings(value, label)
    if len(set(files)) != len(files):
        raise CoordinatorError(
            f"{label} must not repeat a path",
            remedy=f"list each uncommitted path once in tooling_blocker {TOOLING_BLOCKER_UNCOMMITTED_FIELD}",
        )
    for path in files:
        # The restart compares each path verbatim with Git's own output, so it must be in that form.
        if (
            path != PurePosixPath(path).as_posix()
            or "\\" in path
            or path.startswith("/")
            or ".." in PurePosixPath(path).parts
            or not any(fnmatchcase(path, pattern) for pattern in write_paths)
        ):
            raise CoordinatorError(
                f"{label} must be normalized paths inside the approved zone: {path}",
                remedy=f"list in tooling_blocker {TOOLING_BLOCKER_UNCOMMITTED_FIELD} only normalized repository-relative paths (as git status prints them) inside the role's write zone",
            )


def _check_tooling_blocker_shape(blocker: object, label: str, name: str) -> None:
    """Exactly ``tool``, ``command`` and ``message``, each a bounded non-empty string. ``label``
    opens the refusal and ``name`` is the field its remedy tells the role to fix."""
    fields = ", ".join(sorted(TOOLING_BLOCKER_FIELDS))
    if not isinstance(blocker, dict) or set(blocker) != TOOLING_BLOCKER_FIELDS:
        raise CoordinatorError(
            f"{label} must carry exactly: {fields}",
            remedy=f"set {name} to an object with exactly {fields}",
        )
    for field in sorted(TOOLING_BLOCKER_FIELDS):
        if not _non_empty(blocker[field]):
            raise CoordinatorError(
                f"{label} {field} must be a non-empty string",
                remedy=f"set {name} {field} to the tool's exact text",
            )
        if len(blocker[field]) > MAX_CHECK_EVIDENCE_CHARS:
            raise CoordinatorError(
                f"{label} {field} exceeds the bounded summary limit",
                remedy=f"truncate {name} {field} to at most {MAX_CHECK_EVIDENCE_CHARS} characters",
            )


def _validate_incomplete_items(report: JsonObject, role: JsonObject) -> None:
    """``incomplete_items`` lists the brief items a read-only role left undone (issue #501).

    Each entry names the ``brief_item``, the ``reason`` and the ``target_role`` it can be handed to
    (the reporting stage itself or a later role of the pipeline), and may carry the item's own
    ``tooling_blocker``. The text reaches a later brief as agent-to-agent protocol text, so it is
    English and bounded like check evidence.
    """
    if "incomplete_items" not in report:
        return
    stage = report["role"]
    if role["mode"] != "read-only" or stage not in INCOMPLETE_ITEM_TARGET_ROLES:
        raise CoordinatorError(
            f"completion report incomplete_items is allowed only on a "
            f"{', '.join(INCOMPLETE_ITEM_TARGET_ROLES)} report, not a {stage} report",
            remedy="drop incomplete_items; a writing role reports unfinished work as a blocker "
            "or a not_covered dod_coverage record",
        )
    items = report["incomplete_items"]
    required = ", ".join(sorted(INCOMPLETE_ITEM_FIELDS))
    shape = (
        f"set each incomplete_items entry to an object with exactly {required}, "
        "plus an optional tooling_blocker"
    )
    if not isinstance(items, list):
        raise CoordinatorError(
            "completion report incomplete_items must be a list", remedy=shape
        )
    targets = INCOMPLETE_ITEM_TARGET_ROLES[stage]
    for position, item in enumerate(items, start=1):
        label = f"completion report incomplete_items entry {position}"
        if (
            not isinstance(item, dict)
            or not INCOMPLETE_ITEM_FIELDS <= set(item)
            or set(item) - INCOMPLETE_ITEM_FIELDS - INCOMPLETE_ITEM_OPTIONAL_FIELDS
        ):
            raise CoordinatorError(f"{label} has an invalid schema", remedy=shape)
        for field in ("brief_item", "reason"):
            if not _non_empty(item[field]):
                raise CoordinatorError(
                    f"{label} {field} must be a non-empty string",
                    remedy=f"set incomplete_items entry {position} {field}: name the brief "
                    "item and why it was left undone",
                )
            if len(item[field]) > MAX_CHECK_EVIDENCE_CHARS:
                raise CoordinatorError(
                    f"{label} {field} exceeds the bounded summary limit",
                    remedy=f"shorten incomplete_items entry {position} {field} to at most "
                    f"{MAX_CHECK_EVIDENCE_CHARS} characters",
                )
        try:
            _reject_non_english([item["brief_item"], item["reason"]], label)
        except CoordinatorError as exc:
            raise CoordinatorError(
                f"{label} is handed to a later brief as agent-to-agent protocol text and must "
                "be written in English",
                remedy=exc.remedy,
            ) from exc
        if item["target_role"] not in targets:
            raise CoordinatorError(
                f"{label} target_role {item['target_role']!r} is not a role a {stage} report "
                "can hand an item to",
                remedy=f"set incomplete_items entry {position} target_role to one of: "
                f"{', '.join(targets)}",
            )
        if "tooling_blocker" in item:
            _check_tooling_blocker_shape(
                item["tooling_blocker"],
                f"{label} tooling_blocker",
                f"incomplete_items entry {position} tooling_blocker",
            )


def _validate_report(
    report: JsonObject,
    dispatch: JsonObject,
    role: JsonObject,
    repo: Path | None = None,
    base_commit: str | None = None,
    rebase_target: str | None = None,
    closure_base: str | None = None,
) -> None:
    """``rebase_target`` is set only for the developer report that clears a stale-base block: the
    candidate must contain that tip, and its own commits and files are measured from it, so
    upstream commits the rebase brought in are never attributed to the ticket. ``closure_base``
    (``_closure_base``) is where a developer-retry's carried_item_closure counts commits from."""
    _reject_sensitive(report, "completion report")
    if (
        not REPORT_FIELDS <= set(report)
        or set(report) - REPORT_FIELDS - REPORT_OPTIONAL_FIELDS
    ):
        missing = sorted(REPORT_FIELDS - set(report))
        extra = sorted(set(report) - REPORT_FIELDS - REPORT_OPTIONAL_FIELDS)
        raise CoordinatorError(
            f"completion report schema mismatch (missing={missing}, extra={extra})",
            remedy=f"resubmit the completion report with exactly the required fields (missing={missing}, extra={extra})",
        )
    report_language = report.get("report_language")
    if report_language is not None and report_language != "ru":
        raise CoordinatorError(
            "completion report report_language must be ru",
            remedy="set report_language to 'ru'",
        )
    if "communication_policy" in dispatch and report_language != "ru":
        raise CoordinatorError(
            "new completion reports must set report_language to ru",
            remedy="set report_language to 'ru' in the completion report",
        )
    brief = dispatch
    for field in ("lessons", "used_memory"):
        if field in report:
            _strings(report[field], f"completion report {field}", allow_empty=True)
    for field in ("dispatch_id", "ticket", "role"):
        if report[field] != brief[field]:
            raise CoordinatorError(
                f"completion report {field} does not match the approved dispatch",
                remedy=f"resubmit completion report {field} matching the approved dispatch",
            )
    if report["outcome"] not in REPORT_OUTCOMES:
        raise CoordinatorError(
            "completion report outcome is invalid",
            remedy="set outcome to one of the accepted completion-report outcomes",
        )
    _validate_tooling_blocker(report, dispatch)
    _validate_incomplete_items(report, role)
    for field in ("output", "risks", "blockers", "next_coordinator_action"):
        if not _non_empty(report[field]):
            raise CoordinatorError(
                f"completion report {field} must be a non-empty string",
                remedy=f"set completion report {field} to a non-empty string",
            )
    changed_files = _strings(
        report["changed_files"], "completion report changed_files", allow_empty=True
    )
    checks = report["checks_run"]
    # Only a work role that owns no verification gate (the architect) reports an empty list; every
    # other role must report the non-empty command list its brief approved.
    owns_no_gate = (
        dispatch.get("purpose") == "work" and dispatch.get("role") in NO_GATE_WORK_ROLES
    )
    if not isinstance(checks, list) or (not checks and not owns_no_gate):
        raise CoordinatorError(
            "completion report checks_run must be a non-empty list",
            remedy="set checks_run to a non-empty list",
        )
    for check in checks:
        if not isinstance(check, dict) or set(check) != {
            "command",
            "result",
            "evidence",
        }:
            raise CoordinatorError(
                "completion report checks_run has an invalid entry",
                remedy="fix the malformed checks_run entry",
            )
        if not all(
            _non_empty(check[field]) for field in ("command", "result", "evidence")
        ):
            raise CoordinatorError(
                "completion report checks_run entries must contain text evidence",
                remedy="each checks_run entry must contain text evidence of the check result",
            )
        if len(check["evidence"]) > MAX_CHECK_EVIDENCE_CHARS:
            raise CoordinatorError(
                "completion report check evidence exceeds the bounded summary limit; store the full log as an artifact and report its path",
                remedy="store the full check log as an artifact and report its path instead of the raw output",
            )
    commands_run = [check["command"] for check in checks]
    if commands_run != dispatch["verification_commands"]:
        raise CoordinatorError(
            f"completion report checks_run must exactly match approved verification commands "
            f"(expected {dispatch['verification_commands']}, got {commands_run})",
            remedy="re-run exactly the approved verification_commands and report those results",
        )
    plan_rules.check_fields_allowed(report, dispatch)
    carried_items.check_closure(report, dispatch)
    commit_sha = report["commit_sha"]
    if role["mode"] == "write" and (
        not isinstance(commit_sha, str)
        or re.fullmatch(r"[0-9a-fA-F]{7,64}", commit_sha) is None
        or not changed_files
    ):
        raise CoordinatorError(
            "write-role completion reports require commit_sha and changed_files",
            remedy="a write role's completion report must include commit_sha and changed_files",
        )
    if role["mode"] == "read-only" and changed_files:
        raise CoordinatorError(
            "read-only completion reports cannot claim changed files",
            remedy="a read-only role's completion report must not claim changed_files",
        )
    if role["mode"] == "write":
        paths = dispatch["write_paths"]
        for changed_file in changed_files:
            normalized = changed_file.replace("\\", "/")
            if (
                normalized.startswith("/")
                or ".." in Path(normalized).parts
                or not any(fnmatchcase(normalized, pattern) for pattern in paths)
            ):
                raise CoordinatorError(
                    "completion report changed_files must remain inside the approved zone",
                    remedy="keep completion report changed_files inside the role's approved write zone",
                )
        if repo is not None:
            resolved = _candidate_commit(repo, commit_sha)
            if rebase_target is not None:
                if not _git_is_ancestor(repo, rebase_target, resolved):
                    raise CoordinatorError(
                        f"rebase candidate does not contain the integration tip {rebase_target}",
                        remedy=f"rebase the issue branch onto {rebase_target} and report the rebased HEAD",
                    )
                base_commit = rebase_target
            elif plan_rules.is_developer_retry(dispatch):
                _require_fix_forward(repo, dispatch, resolved)
            actual_files = (
                _changed_files_between(repo, base_commit, resolved)
                if base_commit
                else _commit_changed_files(repo, resolved)
            )
            if actual_files != changed_files:
                raise CoordinatorError(
                    "completion report changed_files must exactly match commit_sha",
                    remedy="regenerate completion report changed_files from the actual diff at commit_sha",
                )
        # The commit plan and the carried-item closure are verified against Git history, so they
        # need the repository, like the changed_files check above.
        planned = bool(dispatch.get("commit_plan"))
        closure = carried_items.CLOSURE_FIELD in report
        if (
            role.get("name") == "developer"
            and (planned or closure)
            and repo is not None
        ):
            snapshot = dispatch.get("snapshot_commit")
            if not isinstance(snapshot, str):
                raise CoordinatorError(
                    "developer dispatch lacks snapshot_commit",
                    remedy="create a new developer dispatch with an immutable snapshot",
                )
            if planned:
                plan_rules.check_report(
                    report,
                    dispatch,
                    _commits_between(repo, rebase_target or snapshot, resolved),
                    partial(_candidate_commit, repo),
                )
            if closure:
                # An earlier attempt of the retry chain may have closed an item (issue #503).
                carried_items.check_closure_commits(
                    report,
                    _commits_between(
                        repo, rebase_target or closure_base or snapshot, resolved
                    ),
                    partial(_candidate_commit, repo),
                )
    if role["mode"] == "read-only" and commit_sha != "not applicable — read-only role":
        raise CoordinatorError(
            "read-only completion reports must not claim a commit SHA",
            remedy="a read-only role's completion report must not claim a commit_sha",
        )
    if "risk_triggers" in report:
        _strings(
            report["risk_triggers"], "completion report risk_triggers", allow_empty=True
        )
    if role.get("name") == "code-review":
        _validate_review(report.get("review"), dispatch)
    elif "review" in report:
        raise CoordinatorError(
            "only the code-review role may submit composite review evidence",
            remedy="only the code-review role may submit composite review evidence",
        )


def _report_markdown(report: JsonObject) -> str:
    lines = [f"# Completion report: {report['dispatch_id']}", ""]
    lines.extend(
        [
            f"- Outcome: {report['outcome']}",
            f"- Ticket: {report['ticket']}",
            f"- Role: {report['role']}",
            f"- Output: {report['output']}",
            f"- Commit SHA: {report['commit_sha']}",
            f"- Changed files: {', '.join(report['changed_files']) or 'none'}",
            "- Checks run:",
        ]
    )
    for check in report["checks_run"]:
        lines.append(
            f"  - `{check['command']}` — {check['result']}, {check['evidence']}"
        )
    lines.extend(
        [
            f"- Risks: {report['risks']}",
            f"- Blockers: {report['blockers']}",
            f"- Next coordinator action: {report['next_coordinator_action']}",
        ]
    )
    for field, label in (
        ("lessons", "Lessons (не подтверждено человеком)"),
        ("used_memory", "Used memory (слабый сигнал, не гейт)"),
    ):
        if field in report:
            lines.append(f"- {label}:")
            lines.extend(f"  - {item}" for item in report[field])
    tooling = report.get("tooling_blocker")
    if isinstance(tooling, dict):
        lines.append(
            f"- Tooling blocker: {tooling['tool']} — `{tooling['command']}`: {tooling['message']}"
        )
        uncommitted = tooling.get(TOOLING_BLOCKER_UNCOMMITTED_FIELD)
        if uncommitted:
            lines.append(f"- Uncommitted files: {', '.join(uncommitted)}")
    incomplete = report.get("incomplete_items")
    if incomplete:
        lines.append("- Incomplete items:")
        for item in incomplete:
            line = f"  - [{item['target_role']}] {item['brief_item']}: {item['reason']}"
            blocker = item.get("tooling_blocker")
            if isinstance(blocker, dict):
                line += f" (tooling blocker: {blocker['tool']} — `{blocker['command']}`: {blocker['message']})"
            lines.append(line)
    review = report.get("review")
    if isinstance(review, dict):
        lines.extend(
            [
                f"- Review candidate commit: {review['candidate_commit']}",
                f"- Review scope: {', '.join(review['scope']) or 'none'}",
            ]
        )
        for axis in ("standards", "spec"):
            evidence = review[axis]
            lines.extend(
                [
                    f"- Review {axis.title()} severity: {evidence['severity']}",
                    f"- Review {axis.title()} findings: {len(evidence['findings'])}",
                    f"- Review {axis.title()} risks: {evidence['risks']}",
                    f"- Review {axis.title()} blockers: {evidence['blockers']}",
                ]
            )
            if "inherited_from" in evidence:
                lines.append(
                    f"- Review {axis.title()} inherited from: {evidence['inherited_from']}"
                )
            for finding in evidence["findings"]:
                lines.append(
                    f"  - [{finding['severity']}] {finding['summary']}: {finding['evidence']}"
                )
    lines.append("")
    return "\n".join(lines)


def submit_report(args: argparse.Namespace) -> JsonObject:
    repo = _repo(args)
    report = _read_object(
        _agent_authored_file(repo, args.file, "a completion report"),
        "completion report",
    )
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        dispatch = _load_dispatch(root, cast(str, report.get("dispatch_id")))
        batch = _load_batch(root, dispatch["batch_id"])
        _validate_batch_integrity(root, batch)
        config = core_config._config(repo)
        _validate_dispatch(repo, config, root, batch, dispatch)
        if dispatch["role"] == "qa":
            raise CoordinatorError(
                "QA reports must be produced by the clean-room QA runner",
                remedy="run QA through the clean-room QA runner (qa run), not by hand",
            )
        status = _load_dispatch_status(root, dispatch["dispatch_id"])
        entry = next(
            (
                item
                for item in batch.get("dispatches", [])
                if item["dispatch_id"] == dispatch["dispatch_id"]
            ),
            None,
        )
        if (
            not entry
            or entry.get("state") != "dispatched"
            or status.get("state") not in LIVE_DISPATCH_STATES
        ):
            raise CoordinatorError(
                "completion report requires a dispatched role",
                remedy="only a dispatched role may submit a completion report",
            )
        self_report = status.get("model_self_report")
        if not isinstance(self_report, dict) or self_report.get("match") is not True:
            raise CoordinatorError(
                "a dispatched role must confirm its active model before reporting",
                remedy="pass --model naming the model actually running this session before reporting",
            )
        if (
            dispatch.get("worker_attestation_required", False)
            and status.get("worktree_attestation", {}).get("match") is not True
        ):
            raise CoordinatorError(
                "a dispatched role must attest its canonical Git worktree before reporting",
                remedy="attest the canonical Git worktree (dispatch self-report) before reporting",
            )
        role = _role(repo, dispatch["role"])
        _validate_report(
            report,
            dispatch,
            role,
            repo,
            batch.get("integration_base_commit") or batch.get("base_commit"),
            _rebase_target(batch, dispatch),
            _closure_base(repo, root, batch, dispatch),
        )
        # Only a new report must carry its closure: one recorded earlier is decided as a gap.
        carried_items.require_closure(report, dispatch)
        from harness.orchestration.workflow.decisions import _auto_accept_policy

        auto_accept_policy = _auto_accept_policy(config, batch, dispatch, report)
        retry_candidate: str | None = None
        was_retry = False
        if role["name"] == "developer" and batch.get("retry_candidate_required"):
            retry_candidate = _candidate_commit(repo, report["commit_sha"])
            was_retry = True
            prior_candidates = {
                item.get("candidate_commit")
                for item in batch.get("risk_assessments", [])
                if isinstance(item.get("candidate_commit"), str)
            }
            if retry_candidate in prior_candidates:
                raise CoordinatorError(
                    "a retry must produce a new candidate commit before review or QA",
                    remedy="produce a new candidate commit (developer retry) before the next review or QA dispatch",
                )
            batch["retry_candidate_required"] = False
        if "risk_triggers" in report and role["name"] == "developer":
            triggers = _validate_trigger_names(
                report["risk_triggers"],
                "completion report risk_triggers",
                _risk_triggers(repo),
            )
            if triggers:
                escalated_candidate = _candidate_commit(repo, report["commit_sha"])
                batch.setdefault("risk_escalations", []).append(
                    {
                        "dispatch_id": dispatch["dispatch_id"],
                        "candidate_commit": escalated_candidate,
                        "triggers": triggers,
                    }
                )
                batch["risk_reassessment_required"] = True
                batch["risk_reassessment_candidate"] = escalated_candidate
                batch["risk_reassessment_triggers"] = triggers
            elif was_retry:
                inherited = sorted(
                    {
                        trigger
                        for escalation in batch.get("risk_escalations", [])
                        for trigger in escalation.get("triggers", [])
                    }
                )
                if inherited:
                    batch.setdefault("risk_escalations", []).append(
                        {
                            "dispatch_id": dispatch["dispatch_id"],
                            "candidate_commit": retry_candidate,
                            "triggers": inherited,
                        }
                    )
                    batch["risk_reassessment_required"] = True
                    batch["risk_reassessment_candidate"] = retry_candidate
                    batch["risk_reassessment_triggers"] = inherited
                else:
                    batch["risk_reassessment_required"] = False
                    batch.pop("risk_reassessment_candidate", None)
                    batch.pop("risk_reassessment_triggers", None)
            elif not batch.get("risk_reassessment_required"):
                batch["risk_reassessment_required"] = False
                batch.pop("risk_reassessment_candidate", None)
                batch.pop("risk_reassessment_triggers", None)
        report_json = _persist_report(
            ledger, root, batch, dispatch, report, auto_accept_policy=auto_accept_policy
        )
    response: JsonObject = {
        "dispatch_id": dispatch["dispatch_id"],
        "state": "reported",
        "report": str(report_json),
    }
    if auto_accept_policy is None:
        return response
    # The policy decision, risk assessment and next dispatch run after the ledger lock is released,
    # each as an ordinary command revalidated against the persisted report.  The report is
    # recorded whatever happens next, so a stopped chain is answered with its completion route
    # instead of an error that would invite submitting the report again.
    from harness.orchestration.workflow.completion import _completion, _run_policy_chain

    state_dir = getattr(args, "state_dir", None)
    chain = _run_policy_chain(repo, state_dir, dispatch["dispatch_id"])
    response.update(
        {
            "auto_accepted": chain["auto_accepted"],
            "next_action": chain["next_action"],
            "next_dispatch_id": chain["next_dispatch_id"],
        }
    )
    if chain["failed_step"] is not None:
        response["report_sha256"] = next(
            item["report_sha256"]
            for item in batch["dispatches"]
            if item["dispatch_id"] == dispatch["dispatch_id"]
        )
        response["completion"] = _completion(chain, dispatch["dispatch_id"], state_dir)
    return response
