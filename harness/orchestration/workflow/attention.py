"""Attention and context pressure: when a batch needs a human, and what a worker must do about its
own context budget.

`needs_attention` is a flag on a batch, never a lifecycle state -- raising it changes no dispatch,
deletes no evidence and moves no candidate.  Findings are derived from ledger facts only, so the
same call that raises a flag can also report why.
"""

from __future__ import annotations

import argparse
import hashlib
import uuid
from pathlib import Path

from harness.orchestration import extensions, operational_guards
from harness.orchestration.core import config as core_config
from harness.orchestration.core import utils
from harness.orchestration.core.config import (
    _adaptive_continuation_policy,
    _attention_policy,
    _extension_names,
    _reject_sensitive,
    _role,
)
from harness.orchestration.core.constants import (
    ATTENTION_STATE_FIELDS,
    CONTEXT_TELEMETRY_SOURCES,
    LIVE_DISPATCH_STATES,
    OPERATIONAL_REASON_CATEGORIES,
    TERMINAL_BATCH_STATES,
)
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _canonical,
    _moment,
    _non_empty,
    _repo,
    _safe_id,
    _sanitise,
    _short,
    _silent_seconds,
)
from harness.orchestration.ledger.ledger_ops import (
    _ledger_lock,
    _load_batch,
    _load_dispatch,
    _load_dispatch_status,
    _replace_record,
    _state_root,
)
from harness.orchestration.ledger.lifecycle import BatchRecord, LifecycleLedger
from harness.orchestration.workflow.approval import _approval
from harness.orchestration.workflow.history import (
    _pinned_package_stale,
    _settled,
    _validate_batch_integrity,
)


def _stale_dispatch_finding(dispatch_id: str, silent: int) -> dict[str, str]:
    return operational_guards.attention_finding(
        "stale-dispatch",
        dispatch_id,
        last_safe_action=f"dispatch {dispatch_id} has been silent for {silent}s; its brief and any report stay as immutable evidence and nothing was cancelled",
        recommended_human_action="decide whether to keep waiting, cancel the unsent brief, or abandon the batch; then resolve this attention",
    )


def _attention_findings(
    repo: Path, root: Path, config: JsonObject, batch: JsonObject, moment: str
) -> list[dict[str, str]]:
    """Every reason the batch currently needs a human, from ledger facts only. Nothing here changes
    the lifecycle, deletes evidence or touches the candidate."""
    policy = _attention_policy(config)
    now = _moment(moment, "attention moment")
    findings: list[dict[str, str]] = []
    entries = batch.get("dispatches", [])
    last = entries[-1] if entries else None
    decision = last.get("decision") if last else None
    routing = decision.get("routing") if isinstance(decision, dict) else None
    if last and isinstance(routing, dict):
        subject = last["dispatch_id"]
        category = routing.get("reason_category")
        candidate = routing.get("candidate_commit")
        kept = f"the {routing.get('previous_role')} brief and report of {subject} stay as immutable evidence, candidate {_short(candidate)} is unchanged and no dispatch was created"
        if category == "unknown":
            findings.append(
                operational_guards.attention_finding(
                    "unknown-reason",
                    subject,
                    last_safe_action=kept,
                    recommended_human_action="establish why the role stopped from its structured evidence, then resolve this attention so a developer retry can be approved",
                )
            )
        if category in OPERATIONAL_REASON_CATEGORIES and isinstance(candidate, str):
            repeated = sum(
                1
                for item in batch.get("coordinator_decisions", [])
                if isinstance(item.get("routing"), dict)
                and item["routing"].get("reason_category")
                in OPERATIONAL_REASON_CATEGORIES
                and item["routing"].get("candidate_commit") == candidate
            )
            if repeated > policy["max_infrastructure_retries"]:
                findings.append(
                    operational_guards.attention_finding(
                        "infrastructure-retry-repeated",
                        subject,
                        last_safe_action=kept,
                        recommended_human_action=f"{repeated} operational retries ran for candidate {_short(candidate)}; verify the transport and verification environment before another re-run, then resolve this attention",
                    )
                )
        queued_at = routing.get("decided_at")
        if batch.get("state") == "awaiting-approval" and isinstance(queued_at, str):
            waited = (now - _moment(queued_at, "retry decision time")).total_seconds()
            if waited > policy["retry_queue_seconds"]:
                findings.append(
                    operational_guards.attention_finding(
                        "retry-queued-too-long",
                        subject,
                        last_safe_action=kept,
                        recommended_human_action=f"the {routing.get('next_action')} retry has waited {int(waited)}s; confirm it is still wanted and its evidence fresh, then resolve this attention and approve a new proposal",
                    )
                )
    for entry in entries:
        if _settled(entry):
            continue
        dispatch = _load_dispatch(root, entry["dispatch_id"])
        if entry.get("state") != "reported" and _pinned_package_stale(
            repo, root, batch, dispatch
        ):
            findings.append(
                operational_guards.attention_finding(
                    "stale-evidence",
                    entry["dispatch_id"],
                    last_safe_action=f"dispatch {entry['dispatch_id']} pins a Context Package older than the batch's base or accepted candidate; the brief is unchanged",
                    recommended_human_action="cancel the unsent brief and propose a new dispatch so a fresh Context Package is pinned; then resolve this attention",
                )
            )
        status = _load_dispatch_status(root, entry["dispatch_id"])
        frozen = (
            dispatch.get("orchestration_policy", {}).get("attention", {})
            if isinstance(dispatch.get("orchestration_policy"), dict)
            else {}
        )
        threshold = frozen.get(
            "stale_dispatch_seconds", policy["stale_dispatch_seconds"]
        )
        if (
            status.get("state") in LIVE_DISPATCH_STATES
            and _silent_seconds(status) >= threshold
        ):
            findings.append(
                _stale_dispatch_finding(entry["dispatch_id"], _silent_seconds(status))
            )
    return findings


def _notify_attention(
    config: JsonObject, batch: JsonObject, finding: dict[str, str], moment: str
) -> JsonObject:
    """Tell the configured human-notification adapter. Telling a human is best effort by design: a
    broken adapter is recorded as failed and never prevents the attention state from being set."""
    name = extensions.DEFAULT_EXTENSION
    try:
        name = extensions.selected(config)["human_notifier"]
        if name == extensions.DEFAULT_EXTENSION:
            return {"status": "skipped"}
        extensions.human_notifier(name).notify(
            extensions.AttentionEvent(
                batch["batch_id"],
                finding["reason"],
                moment,
                finding["last_safe_action"],
                finding["recommended_human_action"],
            )
        )
    except Exception as exc:  # noqa: BLE001 - an adapter may fail in any way; the attention state must still be recorded
        return {"status": "failed", "adapter": name, "error": _sanitise(str(exc))[:200]}
    return {"status": "sent", "adapter": name}


def _apply_attention(
    config: JsonObject, batch: JsonObject, findings: list[dict[str, str]], moment: str
) -> bool:
    """Mark the batch as needing attention for every finding not already acknowledged by a human.

    Only the in-memory batch changes and the caller persists it, so the flag rides in the same
    ledger transition as whatever caused it. ``needs_attention`` is a flag on a batch, never a
    lifecycle state: ``state``, ``next_action`` and every dispatch stay exactly as they were.
    """
    acknowledged = set(batch.get("attention_acknowledged", []))
    fresh = [finding for finding in findings if finding["key"] not in acknowledged]
    if not fresh:
        return False
    previous_keys: list[str] = batch.get("attention_open_keys", [])
    open_keys = sorted(set(previous_keys) | {finding["key"] for finding in fresh})
    batch["attention_open_keys"] = open_keys
    if batch.get("needs_attention") is True:
        return open_keys != previous_keys
    top = operational_guards.top_finding(fresh)
    batch.update(
        {
            "needs_attention": True,
            "attention_reason": top["reason"],
            "attention_since": moment,
            "last_safe_action": top["last_safe_action"],
            "recommended_human_action": top["recommended_human_action"],
        }
    )
    batch.setdefault("attention_events", []).append(
        {
            "event": "raised",
            "reason": top["reason"],
            "keys": open_keys,
            "at": moment,
            "notification": _notify_attention(config, batch, top, moment),
        }
    )
    return True


def _require_no_attention(
    ledger: LifecycleLedger,
    repo: Path,
    root: Path,
    config: JsonObject,
    batch: JsonObject,
) -> None:
    """A batch that needs a human never gets its next dispatch created, automatically or otherwise,
    until a human resolves the attention. Evidence and the candidate stay untouched."""
    if _apply_attention(
        config,
        batch,
        _attention_findings(repo, root, config, batch, utils._now()),
        utils._now(),
    ):
        _safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, BatchRecord.from_dict(batch))
    if batch.get("needs_attention") is True:
        raise CoordinatorError(
            f"the batch needs human attention ({batch['attention_reason']}); no next dispatch is created until it is resolved",
            remedy=str(batch["recommended_human_action"])
            + "; resolve it with 'batch attention resolve'",
        )


def _flag_stale_dispatch(
    ledger: LifecycleLedger, repo: Path, root: Path, dispatch: JsonObject, silent: int
) -> None:
    batch = _load_batch(root, dispatch["batch_id"])
    if _apply_attention(
        core_config._config(repo),
        batch,
        [_stale_dispatch_finding(dispatch["dispatch_id"], silent)],
        utils._now(),
    ):
        _safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, BatchRecord.from_dict(batch))


def _attention_view(batch: JsonObject, findings: list[dict[str, str]]) -> JsonObject:
    return {
        "batch_id": batch["batch_id"],
        "state": batch["state"],
        "needs_attention": bool(batch.get("needs_attention", False)),
        **{field: batch.get(field) for field in ATTENTION_STATE_FIELDS},
        "findings": [finding["key"] for finding in findings],
    }


def attention_check(args: argparse.Namespace) -> JsonObject:
    """Evaluate a batch's operational loops and persist ``needs_attention`` when one needs a human."""
    repo = _repo(args)
    root = _state_root(args, repo)
    config = core_config._config(repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        batch = _load_batch(root, args.batch)
        _validate_batch_integrity(root, batch)
        moment = utils._now()
        findings = (
            []
            if batch.get("state") in TERMINAL_BATCH_STATES
            else _attention_findings(repo, root, config, batch, moment)
        )
        if _apply_attention(config, batch, findings, moment):
            _safe_id(batch["batch_id"], "batch")
            _replace_record(ledger, BatchRecord.from_dict(batch))
    return _attention_view(batch, findings)


def attention_resolve(args: argparse.Namespace) -> JsonObject:
    """A human acknowledges the open findings; the flag is cleared, no evidence is touched."""
    repo = _repo(args)
    root = _state_root(args, repo)
    note = args.note.strip() if _non_empty(args.note) else ""
    if not note:
        raise CoordinatorError(
            "resolving attention requires a recorded note",
            remedy="pass --note describing what was checked",
        )
    _reject_sensitive({"note": note}, "attention resolution note")
    approval = _approval(args)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        batch = _load_batch(root, args.batch)
        _validate_batch_integrity(root, batch)
        if batch.get("needs_attention") is not True:
            raise CoordinatorError(
                "this batch does not need attention",
                remedy="there is nothing to resolve; check 'batch attention check' first",
            )
        keys = list(batch.get("attention_open_keys", []))
        batch["attention_acknowledged"] = sorted(
            set(batch.get("attention_acknowledged", [])) | set(keys)
        )
        batch["attention_open_keys"] = []
        batch.setdefault("attention_events", []).append(
            {
                "event": "resolved",
                "at": utils._now(),
                "keys": keys,
                "note": note,
                **approval,
            }
        )
        batch["needs_attention"] = False
        for field in ATTENTION_STATE_FIELDS:
            batch.pop(field, None)
        _safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, BatchRecord.from_dict(batch))
    return _attention_view(batch, [])


def _pressure_action(level: str, writes: bool) -> tuple[bool, str | None]:
    if level == "critical" and writes:
        return True, (
            "Create a checkpoint at the next green TDD boundary (every approved verification command passing) or return a "
            "structured blocker; continue only from that checkpoint, in a new session that attests its model again."
        )
    if level == "critical":
        return True, (
            "Return a structured blocker now (outcome blocked); a read-only role never checkpoints, and a new "
            "independent dispatch on the same candidate re-runs it."
        )
    if level == "warning":
        return (
            False,
            "Reach the next green TDD boundary; a checkpoint is not required yet.",
        )
    return False, None


def record_context_pressure(args: argparse.Namespace) -> JsonObject:
    """Record one provider/runtime-observed context measurement for a dispatch.

    The record is observation only. It never changes ``next_action``, creates a retry, moves a
    dispatch or revokes an approval; at ``critical`` it states what the worker must do. A model's
    own claim about its context is not telemetry and is rejected as a source.
    """
    repo = _repo(args)
    root = _state_root(args, repo)
    config = core_config._config(repo)
    observed, source = args.observed_tokens, args.source
    if observed is None:
        try:
            observation = extensions.context_telemetry_provider(
                _extension_names(config)["context_telemetry_provider"]
            ).observe(args.dispatch)
        except extensions.ExtensionError as exc:
            raise CoordinatorError(exc.message, remedy=exc.remedy) from exc
        if observation is None:
            raise CoordinatorError(
                "no context observation is available for this dispatch",
                remedy="pass --observed-tokens with --source, or select a context_telemetry_provider extension that observes it",
            )
        observed, source = observation.observed_tokens, observation.source
    if isinstance(observed, bool) or not isinstance(observed, int) or observed < 0:
        raise CoordinatorError(
            "observed tokens must be a non-negative integer",
            remedy="pass --observed-tokens as a non-negative integer",
        )
    if source not in CONTEXT_TELEMETRY_SOURCES:
        raise CoordinatorError(
            f"context telemetry source must be provider- or runtime-observed, one of: {', '.join(CONTEXT_TELEMETRY_SOURCES)}",
            remedy="record the token count the provider or runtime reported; a model's self-report is never telemetry",
        )
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        dispatch = _load_dispatch(root, args.dispatch)
        batch = _load_batch(root, dispatch["batch_id"])
        _validate_batch_integrity(root, batch)
        entry = next(
            (
                item
                for item in batch.get("dispatches", [])
                if item["dispatch_id"] == dispatch["dispatch_id"]
            ),
            None,
        )
        if entry is None or entry.get("state") in {"abandoned", "cancelled"}:
            raise CoordinatorError(
                "context pressure can only be recorded for a live dispatch of this batch",
                remedy="record context pressure for a dispatch that was sent and not abandoned or cancelled",
            )
        # The brief froze the limit and warning ratio the dispatch runs under; a later config edit
        # must not move its thresholds. A brief written before that falls back to the current policy.
        frozen = (
            dispatch.get("orchestration_policy", {}).get("context_pressure")
            if isinstance(dispatch.get("orchestration_policy"), dict)
            else None
        )
        adaptive = _adaptive_continuation_policy(config)
        limit = (
            dispatch.get("context_budget")
            or (frozen or {}).get("context_limit")
            or adaptive["context_limit"]
        )
        ratio = (frozen or {}).get("warning_ratio") or adaptive["context_warn_ratio"]
        warning_threshold, level = operational_guards.pressure_level(
            observed, limit, ratio
        )
        action_required, action = _pressure_action(
            level, _role(repo, dispatch["role"])["mode"] == "write"
        )
        record: JsonObject = {
            "pressure_id": f"pressure-{uuid.uuid4()}",
            "dispatch_id": dispatch["dispatch_id"],
            "observed_tokens": observed,
            "context_limit": limit,
            "warning_threshold": warning_threshold,
            "level": level,
            "recorded_at": utils._now(),
            "source": source,
            "action_required": action_required,
            "required_worker_action": action,
        }
        record["record_sha256"] = hashlib.sha256(
            _canonical(record).encode("utf-8")
        ).hexdigest()
        batch.setdefault("context_pressure", []).append(record)
        _safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, BatchRecord.from_dict(batch))
    return record
