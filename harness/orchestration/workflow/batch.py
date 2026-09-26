"""A batch: the unit of work the coordinator plans, approves and closes.

Planning is bounded on purpose -- the scope preflight rejects a batch whose definition of done,
dependencies or expected size exceed the project's preflight policy, because an oversized batch is
what produces an oversized dispatch.  Approval, inventory and the two ways a batch can be closed
without a decision (abandon, not-required) live here.
"""

from __future__ import annotations

import argparse
import uuid
from dataclasses import replace as _vo_replace
from pathlib import Path
from typing import cast

from harness.orchestration.core import config as core_config
from harness.orchestration.core import utils
from harness.orchestration.core.config import (
    _approval_policy,
    _communication_policy,
    _developer_verification_commands,
    _preflight_policy,
    _reject_sensitive,
    _review_verification_commands,
    _verification_commands,
)
from harness.orchestration.core.constants import (
    ATTENTION_STATE_FIELDS,
    LIVE_DISPATCH_STATES,
    PLAN_FIELDS,
    TERMINAL_BATCH_STATES,
)
from harness.orchestration.core.git_utils import (
    _fetch_ref_tip,
    _head_commit,
)
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _non_empty,
    _read_object,
    _repo,
    _safe_id,
    _strings,
)
from harness.orchestration.core.workspace import (
    _harness_runtime_sha256,
    _reject_non_english,
    _required_base_branch,
    _validate_branch,
    _validate_worktree,
)
from harness.orchestration.ledger.ledger_ops import (
    _ledger_lock,
    _load_batch,
    _load_dispatch_status,
    _records_root,
    _replace_record,
    _state_root,
    _write_record,
)
from harness.orchestration.ledger.lifecycle import (
    BatchRecord,
    DispatchStatusRecord,
    LedgerError,
    LifecycleLedger,
    PlanRecord,
)
from harness.orchestration.workflow.approval import (
    _approval,
)
from harness.orchestration.workflow.decisions import (
    _abandon_open_dispatches,
)
from harness.orchestration.workflow.history import (
    _accepted_architect,
    _settled,
    _validate_batch_integrity,
)


def _check_batch_conflicts(root: Path, config: JsonObject, batch: JsonObject) -> None:
    budget = config.get("concurrency_budget")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        raise CoordinatorError(
            "project orchestration config has an invalid concurrency_budget",
            remedy="set concurrency_budget to a positive integer in the project orchestration config",
        )
    active = 0
    for path in sorted((_records_root(root) / "batches").glob("batch-*.json")):
        other = _read_object(path, "batch record")
        if other.get("batch_id") == batch.get("batch_id") or other.get("state") not in {
            "active",
            "awaiting-approval",
        }:
            continue
        active += 1
        if other.get("zone") == batch.get("zone"):
            raise CoordinatorError(
                "another active batch already owns this backend zone",
                remedy="wait for the other active batch in this backend zone to close before creating a new one",
            )
    if active >= budget:
        raise CoordinatorError(
            "concurrency_budget is exhausted",
            remedy="wait for an active worker to finish, or raise concurrency_budget",
        )


def _scope_values(args: argparse.Namespace, name: str) -> list[str]:
    value = getattr(args, name, None)
    if value is None:
        return []
    return _strings(value, name, allow_empty=True)


def _expected_positive(args: argparse.Namespace, name: str) -> int | None:
    value = getattr(args, name, None)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CoordinatorError(
            f"{name.replace('_', '-')} must be a positive integer when provided",
            remedy=f"pass a positive integer for --{name.replace('_', '-')}",
        )
    return value


def _scope_preflight(
    config: JsonObject,
    ticket: str,
    zone: str,
    definition_of_done: list[str],
    dependencies: list[str],
    args: argparse.Namespace,
) -> JsonObject:
    """Reject an oversized ticket before a batch, worktree dispatch, or model session exists."""
    policy = _preflight_policy(config)
    expected_files = sorted(set(_scope_values(args, "expected_file")))
    expected_services = sorted(set(_scope_values(args, "expected_service")))
    expected_changed_lines = _expected_positive(args, "expected_changed_lines")
    supplied_context_tokens = _expected_positive(args, "expected_context_tokens")
    real_dependencies = [item for item in dependencies if item != "none"]
    missing: list[str] = []
    if policy["require_estimates"]:
        if not expected_files:
            missing.append("--expected-file")
        if not expected_services:
            missing.append("--expected-service")
        if expected_changed_lines is None:
            missing.append("--expected-changed-lines")
    if missing:
        raise CoordinatorError(
            "batch preflight requires "
            + ", ".join(missing)
            + "; split the ticket or declare a bounded expected scope before any model dispatch",
            remedy="split the ticket or declare a bounded expected scope (definition-of-done/expected-files/etc.) before dispatching",
        )
    # A deterministic conservative admission estimate.  It prevents a tiny-looking line count
    # spread across many files from escaping the same context budget.  A caller can supply a
    # stricter observed estimate, but cannot lower this floor.
    derived_context_tokens = (
        (expected_changed_lines or 0) * policy["estimated_tokens_per_changed_line"]
        + len(expected_files) * policy["estimated_tokens_per_file"]
    )
    expected_context_tokens = max(supplied_context_tokens or 0, derived_context_tokens)
    problems: list[str] = []
    checks = {
        "definition_of_done_items": (
            len(definition_of_done),
            policy["max_definition_of_done_items"],
        ),
        "dependencies": (len(real_dependencies), policy["max_dependencies"]),
        "expected_files": (len(expected_files), policy["max_expected_files"]),
        "expected_services": (len(expected_services), policy["max_expected_services"]),
        "expected_changed_lines": (
            expected_changed_lines or 0,
            policy["max_expected_changed_lines"],
        ),
        "expected_context_tokens": (
            expected_context_tokens,
            policy["max_expected_context_tokens"],
        ),
    }
    for label, (actual, limit) in checks.items():
        if actual > limit:
            problems.append(f"{label}={actual} exceeds {limit}")
    if problems:
        raise CoordinatorError(
            "batch preflight rejected this ticket: "
            + "; ".join(problems)
            + ". Split it with /to-tickets before creating a batch.",
            remedy="split this ticket with /to-tickets before creating a batch",
        )
    return {
        "ticket": ticket,
        "zone": zone,
        "policy": policy,
        "definition_of_done_items": len(definition_of_done),
        "dependencies": real_dependencies,
        "expected_files": expected_files,
        "expected_services": expected_services,
        "expected_changed_lines": expected_changed_lines or 0,
        "expected_context_tokens": expected_context_tokens,
        "status": "pass",
        "checked_at": utils._now(),
    }


def preflight_batch(args: argparse.Namespace) -> JsonObject:
    repo = _repo(args)
    config = core_config._config(repo)
    ticket = getattr(args, "ticket", None)
    zone = getattr(args, "zone", None)
    if not _non_empty(ticket) or not _non_empty(zone):
        raise CoordinatorError(
            "ticket and zone must be non-empty strings",
            remedy="pass a non-empty --ticket and --zone",
        )
    definition_of_done = _strings(
        getattr(args, "definition_of_done", None), "definition_of_done"
    )
    dependencies = _strings(
        getattr(args, "dependency", None) or ["none"], "dependencies"
    )
    return _scope_preflight(
        config, ticket.strip(), zone.strip(), definition_of_done, dependencies, args
    )


def create_batch(args: argparse.Namespace) -> JsonObject:
    repo = _repo(args)
    config = core_config._config(repo)
    ticket = getattr(args, "ticket", None)
    branch = cast(
        str, getattr(args, "branch", None)
    )  # validated non-empty by _validate_branch below
    worktree = cast(
        str, getattr(args, "worktree", None)
    )  # validated non-empty by _validate_worktree below
    zone = getattr(args, "zone", None)
    integration_ref = getattr(args, "integration_ref", None)
    dod = _strings(getattr(args, "definition_of_done", None), "definition_of_done")
    prohibited = _strings(
        getattr(args, "prohibited_change", None), "prohibited_changes"
    )
    dependencies = _strings(
        getattr(args, "dependency", None) or ["none"], "dependencies"
    )
    if not _non_empty(ticket) or not _non_empty(zone):
        raise CoordinatorError(
            "ticket and zone must be non-empty strings",
            remedy="pass a non-empty --ticket and --zone",
        )
    _validate_branch(repo, branch)
    _validate_worktree(repo, worktree)
    if (
        not isinstance(config.get("backend_zones"), dict)
        or zone not in config["backend_zones"]
    ):
        raise CoordinatorError(
            f"unknown backend zone {zone!r}",
            remedy=f"declare backend zone {zone!r} in the project orchestration config, or pass a configured zone",
        )
    # Nullable for epic-less tasks: falls back to the project's base_branch, the same field
    # `_validate_branch` falls back to, rather than inventing a second convention.
    fetch_ref = (
        integration_ref.strip()
        if _non_empty(integration_ref)
        else _required_base_branch(repo)
    )
    pinned_base = _fetch_ref_tip(repo, fetch_ref)
    _reject_non_english(dod, "definition_of_done")
    _reject_non_english(prohibited, "prohibited_changes")
    scope_preflight = _scope_preflight(
        config, ticket.strip(), zone.strip(), dod, dependencies, args
    )
    record: JsonObject = {
        "batch_id": f"batch-{uuid.uuid4()}",
        "created_at": utils._now(),
        "base_commit": pinned_base,
        "integration_ref": integration_ref.strip()
        if _non_empty(integration_ref)
        else None,
        "integration_base_commit": pinned_base,
        "branch_start_commit": _head_commit(repo),
        "state": "planned",
        "ticket": ticket.strip(),
        "branch": branch.strip(),
        "worktree": worktree.strip(),
        "zone": zone.strip(),
        "definition_of_done": dod,
        "prohibited_changes": prohibited,
        "developer_verification_commands": _developer_verification_commands(config),
        "review_verification_commands": _review_verification_commands(config),
        "verification_commands": _verification_commands(config),
        "required_gates": _strings(
            getattr(args, "required_gate", None) or ["none"], "required_gates"
        ),
        "dependencies": dependencies,
        "approval_policy": _approval_policy(config),
        "communication_policy": _communication_policy(config),
        "scope_preflight": scope_preflight,
        "harness_runtime_sha256": _harness_runtime_sha256(repo),
        "dispatches": [],
        "risk_assessments": [],
        "risk_escalations": [],
        "risk_reassessment_required": False,
    }
    _reject_sensitive(record, "batch")
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        try:
            ledger.ensure()
        except LedgerError as exc:
            raise CoordinatorError(exc.message, remedy=exc.remedy) from exc
        _safe_id(record["batch_id"], "batch")
        _write_record(
            ledger,
            PlanRecord.from_dict({field: record[field] for field in PLAN_FIELDS}),
        )
        _write_record(ledger, BatchRecord.from_dict(record))
    return record


def approve_batch(args: argparse.Namespace) -> JsonObject:
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        record = _load_batch(root, args.batch)
        _validate_batch_integrity(root, record)
        if record.get("state") != "planned":
            raise CoordinatorError(
                "only a planned batch can receive its planning approval",
                remedy="only approve a batch that is still in the planned state",
            )
        updated = _vo_replace(
            BatchRecord.from_dict(record),
            coordinator_approval=_approval(args),
            state="awaiting-approval",
        )
        _safe_id(updated.batch_id, "batch")
        _replace_record(ledger, updated)
        record = updated.to_dict()
    return record


def list_batches(args: argparse.Namespace) -> JsonObject:
    """Inventory of every batch the coordinator holds, with what is still open in each.

    Without this there is no way to find the leftovers of an earlier attempt short of reading the
    state directory by hand, which is exactly how hand-edited state starts.
    """
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        batches = []
        for path in sorted((_records_root(root) / "batches").glob("batch-*.json")):
            batch = _read_object(path, "batch record")
            dispatches = batch.get("dispatches", [])
            open_dispatches = [
                item["dispatch_id"] for item in dispatches if not _settled(item)
            ]
            state = batch.get("state")
            if args.ticket and batch.get("ticket") != args.ticket:
                continue
            if args.state and state != args.state:
                continue
            if args.open and state in TERMINAL_BATCH_STATES:
                continue
            batches.append(
                {
                    "batch_id": batch.get("batch_id"),
                    "ticket": batch.get("ticket"),
                    "branch": batch.get("branch"),
                    "zone": batch.get("zone"),
                    "state": state,
                    "created_at": batch.get("created_at"),
                    "terminal": state in TERMINAL_BATCH_STATES,
                    "dispatches": len(dispatches),
                    "open_dispatches": open_dispatches,
                    "next_action": batch.get("next_action"),
                }
            )
    return {"batches": batches}


def resume_batch(args: argparse.Namespace) -> JsonObject:
    """Continue a startup-blocked or stale batch without discarding accepted evidence.

    The failed immutable dispatch is retired; the next dispatch is a new brief in the
    same batch. A human `block` decision and an abandoned batch are never resumable.
    """
    repo = _repo(args)
    root = _state_root(args, repo)
    reason = args.reason.strip() if _non_empty(args.reason) else ""
    if not reason:
        raise CoordinatorError(
            "resuming a blocked batch requires a recorded reason",
            remedy="pass --reason describing the resolved startup or infrastructure failure",
        )
    _reject_sensitive({"reason": reason}, "resume reason")
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        batch = _load_batch(root, args.batch)
        _validate_batch_integrity(root, batch)
        entries = batch.get("dispatches", [])
        latest = entries[-1] if entries else None
        if not isinstance(latest, dict):
            raise CoordinatorError(
                "batch resume requires a failed dispatch",
                remedy="inspect the batch ledger before choosing a recovery path",
            )
        startup_blocked = (
            batch.get("state") == "blocked"
            and latest.get("state") == "blocked"
        )
        stale_worker = (
            batch.get("state") == "active"
            and latest.get("state") == "dispatched"
            and batch.get("needs_attention") is True
            and batch.get("attention_reason") == "stale-dispatch"
        )
        if not startup_blocked and not stale_worker:
            raise CoordinatorError(
                "batch resume requires a startup-blocked or stale dispatch",
                remedy="resume only an attestation-blocked or observed stale dispatch; use the existing decision or abandon route otherwise",
            )
        status = _load_dispatch_status(root, latest["dispatch_id"])
        if not (
            (startup_blocked and status.get("state") == "blocked")
            or (stale_worker and status.get("state") in LIVE_DISPATCH_STATES)
        ):
            raise CoordinatorError(
                "batch resume requires matching blocked or live dispatch status",
                remedy="inspect the dispatch status and preserve its immutable evidence before retrying",
            )
        next_action = batch.get("next_action")
        if not isinstance(next_action, str):
            if latest.get("role") == "developer" and _accepted_architect(batch):
                next_action = "developer"
            else:
                next_action = latest.get("role")
        if not isinstance(next_action, str) or next_action not in {
            "architect",
            "developer",
            "verification",
            "code-review",
            "qa",
            "publish",
        }:
            raise CoordinatorError(
                "blocked batch has no resumable next action",
                remedy="inspect the accepted ledger history and choose an explicit recovery path",
            )
        moment = utils._now()
        retired = _abandon_open_dispatches(ledger, root, batch, moment)
        if stale_worker:
            keys = list(batch.get("attention_open_keys", []))
            batch["attention_acknowledged"] = sorted(
                set(batch.get("attention_acknowledged", [])) | set(keys)
            )
            batch["attention_open_keys"] = []
            batch["needs_attention"] = False
            for field in ATTENTION_STATE_FIELDS:
                batch.pop(field, None)
            batch.setdefault("attention_events", []).append(
                {
                    "event": "resolved",
                    "at": moment,
                    "keys": keys,
                    "note": reason,
                    "approved_by": "policy:operational-recovery",
                    "approved_at": moment,
                }
            )
        batch["next_action"] = next_action
        batch["state"] = "awaiting-approval"
        batch.setdefault("coordinator_decisions", []).append(
            {
                "decision": "resume",
                "dispatch_id": latest["dispatch_id"],
                "approved_by": "policy:operational-recovery",
                "approved_at": moment,
                "note": reason,
                "next_role": next_action,
            }
        )
        _replace_record(ledger, BatchRecord.from_dict(batch))
    return {
        "batch_id": batch["batch_id"],
        "state": batch["state"],
        "next_action": next_action,
        "retired_dispatches": retired,
    }


def abandon_batch(args: argparse.Namespace) -> JsonObject:
    """Close a batch that can no longer reach a decision, with a recorded reason.

    A dispatch whose worker died before confirming its model can never report, and a batch with no
    pending report can never be decided — so an interrupted attempt would otherwise stay open for
    good, and the only way out was editing the state files by hand. This is that way out, kept inside
    the audit trail: nothing is deleted, the open dispatches are named, and the reason is stored
    beside the approval.
    """
    repo = _repo(args)
    root = _state_root(args, repo)
    approval = _approval(args)
    reason = args.reason.strip() if _non_empty(args.reason) else ""
    if not reason:
        raise CoordinatorError(
            "abandoning a batch requires a recorded reason",
            remedy="pass --reason explaining why this batch cannot be decided",
        )
    _reject_sensitive({"reason": reason}, "abandon reason")
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        batch = _load_batch(root, args.batch)
        _validate_batch_integrity(root, batch)
        open_dispatches = [
            item["dispatch_id"]
            for item in batch.get("dispatches", [])
            if not _settled(item)
        ]
        if batch.get("state") in TERMINAL_BATCH_STATES and not open_dispatches:
            raise CoordinatorError(
                f"batch is already {batch['state']} and has nothing open to close",
                remedy=f"this batch is already {batch['state']!r}; nothing further to close",
            )
        # A terminal batch that still carries an open dispatch is a repair case: its state was moved
        # without closing what it held, and that dispatch would otherwise be surfaced as live for
        # ever. Closing the remainder is exactly this command's job.
        moment = utils._now()
        _abandon_open_dispatches(ledger, root, batch, moment)
        batch["state"] = "failed"
        batch.pop("next_action", None)
        batch.pop("required_next_role", None)
        batch["abandoned"] = {
            "approved_by": approval["approved_by"],
            "approved_at": approval["approved_at"],
            "abandoned_at": moment,
            "reason": reason,
            "open_dispatches": open_dispatches,
        }
        batch.setdefault("coordinator_decisions", []).append(
            {
                "decision": "abandon",
                "approved_by": approval["approved_by"],
                "approved_at": approval["approved_at"],
                "note": reason,
            }
        )
        _safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, BatchRecord.from_dict(batch))
    return {
        "batch_id": batch["batch_id"],
        "ticket": batch["ticket"],
        "state": "failed",
        "abandoned_dispatches": open_dispatches,
    }


def mark_batch_not_required(args: argparse.Namespace) -> JsonObject:
    """Terminally record a batch whose pinned snapshot already satisfies its definition of done.

    This is deliberately distinct from abandonment: no implementation failure occurred. Open
    dispatches are cancelled because a truthful no-change write report has neither a commit nor a
    changed-file list, which ordinary write-report validation must continue to reject.
    """
    repo = _repo(args)
    root = _state_root(args, repo)
    approval = _approval(args)
    reason = args.reason.strip() if _non_empty(args.reason) else ""
    if not reason:
        raise CoordinatorError(
            "marking a batch not-required requires recorded evidence",
            remedy="pass --reason stating why the pinned snapshot requires no implementation",
        )
    _reject_sensitive({"reason": reason}, "not-required reason")
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        batch = _load_batch(root, args.batch)
        _validate_batch_integrity(root, batch)
        if batch.get("state") in TERMINAL_BATCH_STATES:
            raise CoordinatorError(
                f"batch is already {batch['state']}",
                remedy="create a new batch only if a new implementation requirement appears",
            )
        moment = utils._now()
        cancelled_dispatches = []
        for entry in batch.get("dispatches", []):
            if _settled(entry):
                continue
            entry["state"] = "cancelled"
            cancelled_dispatches.append(entry["dispatch_id"])
            status_path = (
                _records_root(root)
                / DispatchStatusRecord.directory
                / f"{_safe_id(entry['dispatch_id'], 'dispatch')}.json"
            )
            if status_path.exists():
                status = _load_dispatch_status(root, entry["dispatch_id"])
                status.update({"state": "cancelled", "updated_at": moment})
                _replace_record(ledger, DispatchStatusRecord.from_dict(status))
        batch["state"] = "not-required"
        batch.pop("next_action", None)
        batch.pop("required_next_role", None)
        batch["not_required"] = {
            "approved_by": approval["approved_by"],
            "approved_at": approval["approved_at"],
            "recorded_at": moment,
            "reason": reason,
            "tracker_resolution": "resolution::wontfix",
            "cancelled_dispatches": cancelled_dispatches,
        }
        batch.setdefault("coordinator_decisions", []).append(
            {
                "decision": "not-required",
                "approved_by": approval["approved_by"],
                "approved_at": approval["approved_at"],
                "note": reason,
            }
        )
        _replace_record(ledger, BatchRecord.from_dict(batch))
    return {
        "batch_id": batch["batch_id"],
        "ticket": batch["ticket"],
        "state": "not-required",
        "tracker_resolution": "resolution::wontfix",
        "cancelled_dispatches": cancelled_dispatches,
    }
