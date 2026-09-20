"""Facts read out of a batch's recorded history.

Every function here answers one question about what a batch has already been through -- its latest
accepted developer candidate, the QA report pinned to a candidate, whether a Context Package is
still fresh -- from ledger records alone.  Nothing here advances the lifecycle or writes state, so
the handler modules in this package can all depend on it without depending on each other.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import cast

from harness.errors import INTERNAL_INVARIANT_REMEDY
from harness.orchestration import operational_guards
from harness.orchestration.core import utils
from harness.orchestration.core.constants import (
    ATTENTION_EVENT_KINDS, ATTENTION_STATE_FIELDS, CONTEXT_PACKAGE_FIELDS, CONTEXT_PRESSURE_FIELDS,
    CONTEXT_TELEMETRY_SOURCES, LEGACY_CONTEXT_PACKAGE_FIELDS, LEGACY_PLAN_FIELDS, PLAN_FIELDS,
    PRE_APPROVAL_LEGACY_PLAN_FIELDS, RISK_ASSESSMENT_FIELDS, TERMINAL_BATCH_STATES,
)
from harness.orchestration.core.utils import (
    CoordinatorError, JsonObject, _canonical, _non_empty, _now, _read_object, _safe_id,
)
from harness.orchestration.core.git_utils import (
    _candidate_commit,
)
from harness.orchestration.core.config import (
    _reject_sensitive,
)
from harness.orchestration.ledger.ledger_ops import (
    _load_checkpoint, _load_context_package, _load_dispatch, _load_risk, _records_root,
)


def _validate_risk(root: Path, batch: JsonObject, risk: JsonObject) -> None:
    _reject_sensitive(risk, "risk assessment")
    if set(risk) != RISK_ASSESSMENT_FIELDS:
        raise CoordinatorError("risk assessment schema mismatch", remedy="regenerate the risk assessment record so its schema matches the current version")
    if risk["batch_id"] != batch["batch_id"]:
        raise CoordinatorError("risk assessment does not belong to its batch", remedy="the risk assessment record does not belong to this batch -- " + INTERNAL_INVARIANT_REMEDY)
    if not isinstance(risk["candidate_commit"], str) or re.fullmatch(r"[0-9a-f]{40}", risk["candidate_commit"]) is None:
        raise CoordinatorError("risk assessment has an invalid candidate commit", remedy="regenerate the risk assessment with a valid candidate_commit")
    if risk["base_commit"] is not None and (
        not isinstance(risk["base_commit"], str) or re.fullmatch(r"[0-9a-f]{40}", risk["base_commit"]) is None
    ):
        raise CoordinatorError("risk assessment has an invalid base commit", remedy="regenerate the risk assessment with a valid base_commit")
    if not isinstance(risk["changed_files"], list) or not all(_non_empty(item) for item in risk["changed_files"]):
        raise CoordinatorError("risk assessment has invalid changed files", remedy="regenerate the risk assessment with a valid changed_files list")
    if not isinstance(risk["matched_triggers"], list) or not all(_non_empty(item) for item in risk["matched_triggers"]):
        raise CoordinatorError("risk assessment has invalid matched triggers", remedy="regenerate the risk assessment with valid matched_triggers")
    if not isinstance(risk["developer_triggers"], list) or not all(_non_empty(item) for item in risk["developer_triggers"]):
        raise CoordinatorError("risk assessment has invalid developer triggers", remedy="regenerate the risk assessment with valid developer_triggers")
    if not isinstance(risk["review_required"], bool) or risk["review_scope"] != risk["changed_files"]:
        raise CoordinatorError("risk assessment review scope is invalid", remedy="regenerate the risk assessment with a valid review scope")
    entry = next(
        (item for item in batch.get("risk_assessments", []) if item.get("risk_assessment_id") == risk["risk_assessment_id"]),
        None,
    )
    expected = hashlib.sha256(_canonical(risk).encode("utf-8")).hexdigest()
    if not entry or entry.get("record_sha256") != expected:
        raise CoordinatorError("risk assessment failed immutable record integrity check", remedy="the risk assessment record was modified after its integrity hash was recorded -- " + INTERNAL_INVARIANT_REMEDY)


def _risk_for_candidate(root: Path, batch: JsonObject, candidate: str) -> JsonObject | None:
    matches = [
        item for item in batch.get("risk_assessments", []) if item.get("candidate_commit") == candidate
    ]
    if not matches:
        return None
    risk = _load_risk(root, matches[-1].get("risk_assessment_id"))
    _validate_risk(root, batch, risk)
    return risk


def _latest_checkpoint_for_dispatch(root: Path, batch: JsonObject, dispatch_id: str) -> JsonObject:
    entries = [item for item in batch.get("checkpoints", []) if item.get("dispatch_id") == dispatch_id]
    if not entries:
        raise CoordinatorError("dispatch has no recorded checkpoint to resume from", remedy="checkpoint this dispatch before attempting to resume it")
    checkpoint = _load_checkpoint(root, entries[-1]["checkpoint_id"])
    expected = hashlib.sha256(_canonical(checkpoint).encode("utf-8")).hexdigest()
    if entries[-1].get("record_sha256") != expected:
        raise CoordinatorError("checkpoint failed immutable record integrity check", remedy="the checkpoint record was modified after its integrity hash was recorded -- " + INTERNAL_INVARIANT_REMEDY)
    return checkpoint


def _validate_context_package(root: Path, batch: JsonObject, package: JsonObject) -> None:
    _reject_sensitive(package, "context package")
    if set(package) != CONTEXT_PACKAGE_FIELDS and set(package) != LEGACY_CONTEXT_PACKAGE_FIELDS:
        raise CoordinatorError("context package schema mismatch", remedy="regenerate the context package record so its schema matches the current version")
    if package["batch_id"] != batch["batch_id"]:
        raise CoordinatorError("context package does not belong to its batch", remedy="the context package record does not belong to this batch -- " + INTERNAL_INVARIANT_REMEDY)
    entry = next(
        (
            item for item in batch.get("context_packages", [])
            if item.get("context_package_id") == package["context_package_id"]
        ),
        None,
    )
    expected = hashlib.sha256(_canonical(package).encode("utf-8")).hexdigest()
    if not entry or entry.get("record_sha256") != expected:
        raise CoordinatorError("context package failed immutable record integrity check", remedy="the context package record was modified after its integrity hash was recorded -- " + INTERNAL_INVARIANT_REMEDY)


def _context_package_summary(package: JsonObject) -> JsonObject:
    """Compact portable handoff data for a new role session.

    The full immutable package remains in the ledger exactly once.  The brief carries enough
    bounded navigation to start work without rediscovering files or copying the full diff into
    every model prompt; the pinned commits let a role obtain a precise diff when it truly needs it.
    """
    return {
        "base_commit": package["base_commit"],
        "candidate_commit": package["candidate_commit"],
        "starting_files": package["starting_files"],
        "related_tests": package["related_tests"],
        "precedent_cards": package["precedent_cards"],
        "estimated_tokens": package.get("estimated_tokens"),
    }


def _reusable_context_package(
    root: Path, batch: JsonObject, base_commit: str, candidate_commit: str,
) -> JsonObject | None:
    """Return the current batch's shared package for exactly the same pinned diff.

    A package is immutable and role-neutral. Architect and developer therefore share the base
    snapshot, and a resumed worker keeps its brief's exact package ID instead of rebuilding or
    re-reading discovery. Review gets a new package only once the candidate actually changes.
    """
    for entry in reversed(batch.get("context_packages", [])):
        if entry.get("base_commit") != base_commit or entry.get("candidate_commit") != candidate_commit:
            continue
        package = _load_context_package(root, entry.get("context_package_id"))
        _validate_context_package(root, batch, package)
        if package.get("role") == "shared":
            return package
    return None


def _latest_context_package(root: Path, batch: JsonObject) -> JsonObject | None:
    entries = batch.get("context_packages", [])
    if not entries:
        return None
    package = _load_context_package(root, entries[-1]["context_package_id"])
    _validate_context_package(root, batch, package)
    return package


def _context_package_freshness(repo: Path, root: Path, batch: JsonObject) -> JsonObject | None:
    """Shadow-mode evidence only: records whether the batch's latest registered Context Package
    still matches current repository state (its base and the latest accepted developer candidate).
    Never blocks dispatch creation -- roles are not yet restricted to the package."""
    package = _latest_context_package(root, batch)
    if package is None:
        return None
    current_base = batch.get("integration_base_commit") or batch.get("base_commit")
    try:
        current_candidate = _latest_developer_candidate(repo, root, batch)
    except CoordinatorError:
        current_candidate = None
    fresh = package["base_commit"] == current_base and (
        current_candidate is None or package["candidate_commit"] == current_candidate
    )
    return {
        "context_package_id": package["context_package_id"],
        "status": "fresh" if fresh else "stale",
        "checked_at": utils._now(),
        "registered_base_commit": package["base_commit"],
        "current_base_commit": current_base,
        "registered_candidate_commit": package["candidate_commit"],
        "current_candidate_commit": current_candidate,
    }


def _latest_developer_candidate(repo: Path, root: Path, batch: JsonObject) -> str:
    accepted = [
        item for item in batch.get("dispatches", [])
        if item.get("role") == "developer"
        and item.get("state") == "reported"
        and item.get("decision", {}).get("decision") in {"accept", "override-warning"}
    ]
    if not accepted:
        raise CoordinatorError("candidate dispatch requires an accepted developer completion report", remedy="accept the developer's completion report before creating a candidate dispatch")
    report = _pending_report(root, batch, accepted[-1])
    try:
        return _candidate_commit(repo, report["commit_sha"])
    except (KeyError, CoordinatorError) as exc:
        raise CoordinatorError("accepted developer report has no resolvable candidate commit", remedy="the accepted developer report has no resolvable candidate commit -- " + INTERNAL_INVARIANT_REMEDY) from exc


def _accepted_architect(batch: JsonObject) -> bool:
    return any(
        item.get("role") == "architect"
        and item.get("state") == "reported"
        and isinstance(item.get("decision"), dict)
        and item["decision"].get("decision") in {"accept", "override-warning"}
        for item in batch.get("dispatches", [])
    )


def _accepted_qa_for_candidate(root: Path, batch: JsonObject, candidate: str) -> JsonObject:
    """Return the accepted green QA report pinned to exactly ``candidate``."""
    for entry in reversed(batch.get("dispatches", [])):
        if entry.get("role") != "qa" or entry.get("state") != "reported":
            continue
        if entry.get("decision", {}).get("decision") != "accept":
            continue
        dispatch = _load_dispatch(root, entry.get("dispatch_id"))
        if dispatch.get("candidate_commit") != candidate:
            continue
        report = _pending_report(root, batch, entry)
        if report.get("outcome") == "completed":
            return report
    raise CoordinatorError("publish requires accepted green QA evidence for the candidate commit", remedy="accept green QA evidence for this candidate commit before publishing")


def _settled(entry: JsonObject) -> bool:
    """A dispatch is settled once nothing further can happen to it.

    That is either a report the coordinator has decided on, or an abandonment — both are terminal.
    Anything else, including a dispatch blocked on a model mismatch, is still open.
    """
    if entry.get("state") in {"abandoned", "cancelled"}:
        return True
    return entry.get("state") == "reported" and isinstance(entry.get("decision"), dict)


def _validate_operational_batch_fields(batch: JsonObject) -> None:
    """Shape and integrity of the batch-level records issue #250 added. Every field is optional, so a
    batch written before them stays valid; one that carries them must carry them well-formed."""
    for entry in batch.get("context_pressure", []):
        if not isinstance(entry, dict) or set(entry) != CONTEXT_PRESSURE_FIELDS:
            raise CoordinatorError("batch context_pressure record schema mismatch", remedy="the batch context_pressure record is malformed -- " + INTERNAL_INVARIANT_REMEDY)
        body = {key: value for key, value in entry.items() if key != "record_sha256"}
        if entry["record_sha256"] != hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest():
            raise CoordinatorError("batch context_pressure record failed immutable integrity check", remedy="a context_pressure record was modified after its hash was recorded -- " + INTERNAL_INVARIANT_REMEDY)
        numbers = (entry["observed_tokens"], entry["context_limit"], entry["warning_threshold"])
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in numbers) or entry["warning_threshold"] > entry["context_limit"]:
            raise CoordinatorError("batch context_pressure record has invalid token numbers", remedy="the context_pressure record numbers are malformed -- " + INTERNAL_INVARIANT_REMEDY)
        if entry["level"] != operational_guards.level_for(*numbers) or entry["source"] not in CONTEXT_TELEMETRY_SOURCES:
            raise CoordinatorError("batch context_pressure level or source does not match its record", remedy="the context_pressure level or source is inconsistent -- " + INTERNAL_INVARIANT_REMEDY)
    if "needs_attention" in batch:
        flag = batch["needs_attention"]
        if not isinstance(flag, bool):
            raise CoordinatorError("batch needs_attention must be a boolean", remedy="the batch needs_attention flag is malformed -- " + INTERNAL_INVARIANT_REMEDY)
        if flag and (
            not all(_non_empty(batch.get(field)) for field in ATTENTION_STATE_FIELDS)
            or batch["attention_reason"] not in operational_guards.ATTENTION_REASONS
        ):
            raise CoordinatorError("a batch that needs attention must record its reason, time, last safe action and recommended human action", remedy="the batch attention state is incomplete -- " + INTERNAL_INVARIANT_REMEDY)
    for event in batch.get("attention_events", []):
        if not isinstance(event, dict) or event.get("event") not in ATTENTION_EVENT_KINDS or not _non_empty(event.get("at")):
            raise CoordinatorError("batch attention_events entry is malformed", remedy="an attention event is malformed -- " + INTERNAL_INVARIANT_REMEDY)
    for field in ("attention_open_keys", "attention_acknowledged"):
        if field in batch and not (isinstance(batch[field], list) and all(_non_empty(item) for item in batch[field])):
            raise CoordinatorError(f"batch {field} must be a list of keys", remedy=f"the batch {field} is malformed -- " + INTERNAL_INVARIANT_REMEDY)


def _validate_batch_integrity(root: Path, batch: JsonObject) -> None:
    _validate_operational_batch_fields(batch)
    plan = _read_object(
        _records_root(root) / "plans" / f"{_safe_id(batch.get('batch_id'), 'batch')}.json", "immutable batch plan",
    )
    for field in ("approval_policy", "communication_policy"):
        if (field in batch) != (field in plan):
            raise CoordinatorError("batch record is incomplete", remedy="restore the batch record so it has every required field, or run 'ledger clean'")
    for fields in (PLAN_FIELDS, LEGACY_PLAN_FIELDS):
        if all(field in batch for field in fields) and all(field in plan for field in fields):
            if {field: batch[field] for field in fields} == {field: plan[field] for field in fields}:
                return
            raise CoordinatorError("batch record does not match its immutable plan", remedy="the batch record diverged from its immutable plan -- " + INTERNAL_INVARIANT_REMEDY)
    # Batch records created before approval_policy was added are still immutable and safe to
    # continue: the transition code resolves the project default when the field is absent. Accept
    # this historical shape only when both records omit the field; a one-sided omission indicates
    # corruption or an incomplete manual migration and must remain blocked.
    if (
        all(field in batch for field in PRE_APPROVAL_LEGACY_PLAN_FIELDS)
        and all(field in plan for field in PRE_APPROVAL_LEGACY_PLAN_FIELDS)
        and "approval_policy" not in batch
        and "approval_policy" not in plan
        and {
            field: batch[field] for field in PRE_APPROVAL_LEGACY_PLAN_FIELDS
        } == {
            field: plan[field] for field in PRE_APPROVAL_LEGACY_PLAN_FIELDS
        }
    ):
        return
    raise CoordinatorError("batch record is incomplete", remedy="restore the batch record so it has every required field, or run 'ledger clean'")


def _batch_for_ticket_branch(
    root: Path, ticket: str, branch: str, candidate: str, requested_batch: object = None,
) -> JsonObject:
    """Find the batch whose accepted QA proof is pinned to this candidate.

    A coordinator can retain abandoned planning attempts for the same ticket and issue branch.
    Those records are audit evidence, not competing QA proof, so a current SHA selects the batch
    rather than making PR preparation depend on deleting its history.
    """
    batches_dir = _records_root(root) / "batches"
    if not batches_dir.is_dir():
        raise CoordinatorError("no orchestration batches exist for the ticket branch", remedy="verify the ticket/branch and that a batch exists for it")
    matches = []
    for path in sorted(batches_dir.glob("*.json")):
        batch = _read_object(path, "batch record")
        if batch.get("ticket") == ticket and batch.get("branch") == branch:
            _validate_batch_integrity(root, batch)
            matches.append(batch)
    if not matches:
        raise CoordinatorError("no orchestration batch matches the ticket and issue branch", remedy="pass a ticket and branch that match an existing orchestration batch")
    if requested_batch is not None:
        batch_id = _safe_id(requested_batch, "batch")
        selected = next((batch for batch in matches if batch.get("batch_id") == batch_id), None)
        if selected is None:
            raise CoordinatorError("requested batch does not match the ticket and issue branch", remedy="pass a batch, ticket and branch that all match one existing orchestration batch")
        return selected
    candidates = []
    for batch in matches:
        try:
            _accepted_qa_for_candidate(root, batch, candidate)
        except CoordinatorError:
            continue
        candidates.append(batch)
    if len(candidates) != 1:
        if not candidates:
            raise CoordinatorError("no batch has accepted green QA evidence for the candidate commit", remedy="accept green QA evidence for this candidate commit, or pass --batch to disambiguate")
        raise CoordinatorError("multiple batches have accepted green QA evidence for the candidate commit; pass --batch", remedy="pass --batch to disambiguate which batch's accepted QA evidence to use")
    return candidates[0]


def _pending_report(root: Path, batch: JsonObject, entry: JsonObject) -> JsonObject:
    report_path = entry.get("report")
    if not isinstance(report_path, str):
        raise CoordinatorError("reported dispatch has no completion report", remedy="the reported dispatch has no completion report on disk -- " + INTERNAL_INVARIANT_REMEDY)
    report = _read_object(_records_root(root) / report_path, "completion report")
    expected = entry.get("report_sha256")
    actual = hashlib.sha256(_canonical(report).encode("utf-8")).hexdigest()
    if not isinstance(expected, str) or expected != actual:
        raise CoordinatorError("completion report failed immutable integrity check", remedy="the completion report was modified after its integrity hash was recorded -- " + INTERNAL_INVARIANT_REMEDY)
    return report


def _effective_base(batch: JsonObject) -> str:
    return cast(str, batch.get("integration_base_commit") or batch["base_commit"])


def _pinned_package_stale(repo: Path, root: Path, batch: JsonObject, dispatch: JsonObject) -> bool:
    """Whether the Context Package a not-yet-finished brief pinned no longer matches the batch's
    current base or latest accepted candidate."""
    package_id = dispatch.get("context_package_id")
    if not isinstance(package_id, str):
        return False
    package = _load_context_package(root, package_id)
    try:
        current_candidate: str | None = _latest_developer_candidate(repo, root, batch)
    except CoordinatorError:
        current_candidate = None
    return package["base_commit"] != _effective_base(batch) or (
        current_candidate is not None and package["candidate_commit"] != current_candidate
    )
