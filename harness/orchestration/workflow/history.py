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
from harness.orchestration.contract import (
    ContractError,
    valid_tool_list,
    validate_brief_policy,
)
from harness.orchestration.core import utils
from harness.orchestration.core.config import (
    _configured,
    _project,
    _reject_sensitive,
    _resolve_assignment,
)
from harness.orchestration.core.constants import (
    ATTENTION_EVENT_KINDS,
    ATTENTION_STATE_FIELDS,
    CONTEXT_PACKAGE_FIELDS,
    CONTEXT_PRESSURE_FIELDS,
    CONTEXT_TELEMETRY_SOURCES,
    DEFAULT_COMMUNICATION_POLICY,
    DISPATCH_FIELDS,
    DISPATCH_PURPOSES,
    LEGACY_CONTEXT_PACKAGE_FIELDS,
    LEGACY_PLAN_FIELDS,
    LIVE_DISPATCH_STATES,
    PLAN_FIELDS,
    POLICY_BRIEF_FIELDS,
    PRE_APPROVAL_LEGACY_PLAN_FIELDS,
    RISK_ASSESSMENT_FIELDS,
)
from harness.orchestration.core.git_utils import (
    _candidate_commit,
)
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _canonical,
    _non_empty,
    _read_object,
    _safe_id,
)
from harness.orchestration.core.workspace import (
    _validate_branch,
    _validate_harness_runtime_snapshot,
)
from harness.orchestration.ledger.ledger_ops import (
    _load_checkpoint,
    _load_context_package,
    _load_dispatch,
    _load_dispatch_status,
    _load_risk,
    _records_root,
)


def _validate_risk(root: Path, batch: JsonObject, risk: JsonObject) -> None:
    _reject_sensitive(risk, "risk assessment")
    if set(risk) != RISK_ASSESSMENT_FIELDS:
        raise CoordinatorError(
            "risk assessment schema mismatch",
            remedy="regenerate the risk assessment record so its schema matches the current version",
        )
    if risk["batch_id"] != batch["batch_id"]:
        raise CoordinatorError(
            "risk assessment does not belong to its batch",
            remedy="the risk assessment record does not belong to this batch -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    if (
        not isinstance(risk["candidate_commit"], str)
        or re.fullmatch(r"[0-9a-f]{40}", risk["candidate_commit"]) is None
    ):
        raise CoordinatorError(
            "risk assessment has an invalid candidate commit",
            remedy="regenerate the risk assessment with a valid candidate_commit",
        )
    if risk["base_commit"] is not None and (
        not isinstance(risk["base_commit"], str)
        or re.fullmatch(r"[0-9a-f]{40}", risk["base_commit"]) is None
    ):
        raise CoordinatorError(
            "risk assessment has an invalid base commit",
            remedy="regenerate the risk assessment with a valid base_commit",
        )
    if not isinstance(risk["changed_files"], list) or not all(
        _non_empty(item) for item in risk["changed_files"]
    ):
        raise CoordinatorError(
            "risk assessment has invalid changed files",
            remedy="regenerate the risk assessment with a valid changed_files list",
        )
    if not isinstance(risk["matched_triggers"], list) or not all(
        _non_empty(item) for item in risk["matched_triggers"]
    ):
        raise CoordinatorError(
            "risk assessment has invalid matched triggers",
            remedy="regenerate the risk assessment with valid matched_triggers",
        )
    if not isinstance(risk["developer_triggers"], list) or not all(
        _non_empty(item) for item in risk["developer_triggers"]
    ):
        raise CoordinatorError(
            "risk assessment has invalid developer triggers",
            remedy="regenerate the risk assessment with valid developer_triggers",
        )
    if (
        not isinstance(risk["review_required"], bool)
        or risk["review_scope"] != risk["changed_files"]
    ):
        raise CoordinatorError(
            "risk assessment review scope is invalid",
            remedy="regenerate the risk assessment with a valid review scope",
        )
    entry = next(
        (
            item
            for item in batch.get("risk_assessments", [])
            if item.get("risk_assessment_id") == risk["risk_assessment_id"]
        ),
        None,
    )
    expected = hashlib.sha256(_canonical(risk).encode("utf-8")).hexdigest()
    if not entry or entry.get("record_sha256") != expected:
        raise CoordinatorError(
            "risk assessment failed immutable record integrity check",
            remedy="the risk assessment record was modified after its integrity hash was recorded -- "
            + INTERNAL_INVARIANT_REMEDY,
        )


def _risk_for_candidate(
    root: Path, batch: JsonObject, candidate: str
) -> JsonObject | None:
    matches = [
        item
        for item in batch.get("risk_assessments", [])
        if item.get("candidate_commit") == candidate
    ]
    if not matches:
        return None
    risk = _load_risk(root, matches[-1].get("risk_assessment_id"))
    _validate_risk(root, batch, risk)
    return risk


def _latest_checkpoint_for_dispatch(
    root: Path, batch: JsonObject, dispatch_id: str
) -> JsonObject:
    entries = [
        item
        for item in batch.get("checkpoints", [])
        if item.get("dispatch_id") == dispatch_id
    ]
    if not entries:
        raise CoordinatorError(
            "dispatch has no recorded checkpoint to resume from",
            remedy="checkpoint this dispatch before attempting to resume it",
        )
    checkpoint = _load_checkpoint(root, entries[-1]["checkpoint_id"])
    expected = hashlib.sha256(_canonical(checkpoint).encode("utf-8")).hexdigest()
    if entries[-1].get("record_sha256") != expected:
        raise CoordinatorError(
            "checkpoint failed immutable record integrity check",
            remedy="the checkpoint record was modified after its integrity hash was recorded -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    return checkpoint


def _validate_context_package(
    root: Path, batch: JsonObject, package: JsonObject
) -> None:
    _reject_sensitive(package, "context package")
    if (
        set(package) != CONTEXT_PACKAGE_FIELDS
        and set(package) != LEGACY_CONTEXT_PACKAGE_FIELDS
    ):
        raise CoordinatorError(
            "context package schema mismatch",
            remedy="regenerate the context package record so its schema matches the current version",
        )
    if package["batch_id"] != batch["batch_id"]:
        raise CoordinatorError(
            "context package does not belong to its batch",
            remedy="the context package record does not belong to this batch -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    entry = next(
        (
            item
            for item in batch.get("context_packages", [])
            if item.get("context_package_id") == package["context_package_id"]
        ),
        None,
    )
    expected = hashlib.sha256(_canonical(package).encode("utf-8")).hexdigest()
    if not entry or entry.get("record_sha256") != expected:
        raise CoordinatorError(
            "context package failed immutable record integrity check",
            remedy="the context package record was modified after its integrity hash was recorded -- "
            + INTERNAL_INVARIANT_REMEDY,
        )


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
    root: Path,
    batch: JsonObject,
    base_commit: str,
    candidate_commit: str,
) -> JsonObject | None:
    """Return the current batch's shared package for exactly the same pinned diff.

    A package is immutable and role-neutral. Architect and developer therefore share the base
    snapshot, and a resumed worker keeps its brief's exact package ID instead of rebuilding or
    re-reading discovery. Review gets a new package only once the candidate actually changes.
    """
    for entry in reversed(batch.get("context_packages", [])):
        if (
            entry.get("base_commit") != base_commit
            or entry.get("candidate_commit") != candidate_commit
        ):
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


def _context_package_freshness(
    repo: Path, root: Path, batch: JsonObject
) -> JsonObject | None:
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
    candidates: list[str] = []
    for item in batch.get("dispatches", []):
        if item.get("state") != "reported" or item.get("decision", {}).get(
            "decision"
        ) not in {"accept", "override-warning"}:
            continue
        # Legacy dispatch ledger entries predate the explicit ``purpose`` field.
        # They are developer work dispatches unless they explicitly identify another
        # purpose (currently only publish), so candidate history must retain them.
        if item.get("role") == "developer" and item.get("purpose", "work") == "work":
            report = _pending_report(root, batch, item)
            candidates.append(_candidate_commit(repo, report["commit_sha"]))
        elif item.get("role") == "verification":
            dispatch = _load_dispatch(root, item["dispatch_id"])
            candidate = dispatch.get("candidate_commit")
            if isinstance(candidate, str):
                candidates.append(_candidate_commit(repo, candidate))
    if not candidates:
        raise CoordinatorError(
            "candidate dispatch requires an accepted developer completion report",
            remedy="accept the developer's completion report before creating a candidate dispatch",
        )
    return candidates[-1]


def _latest_registered_verification_candidate(repo: Path, batch: JsonObject) -> str:
    """Return the append-only candidate awaiting its read-only verification dispatch."""
    registrations = batch.get("candidate_registrations", [])
    if not isinstance(registrations, list):
        raise CoordinatorError(
            "candidate registrations are malformed",
            remedy="repair the coordinator ledger before creating another dispatch",
        )
    for registration in reversed(registrations):
        if not isinstance(registration, dict):
            continue
        candidate = registration.get("candidate_commit")
        if isinstance(candidate, str):
            return _candidate_commit(repo, candidate)
    raise CoordinatorError(
        "verification dispatch requires a registered infrastructure-blocked developer candidate",
        remedy="retry a blocked developer report with verified operational evidence before creating verification",
    )


def _accepted_architect(batch: JsonObject) -> bool:
    return any(
        item.get("role") == "architect"
        and item.get("state") == "reported"
        and isinstance(item.get("decision"), dict)
        and item["decision"].get("decision") in {"accept", "override-warning"}
        for item in batch.get("dispatches", [])
    )


def _accepted_qa_for_candidate(
    root: Path, batch: JsonObject, candidate: str
) -> JsonObject:
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
    raise CoordinatorError(
        "publish requires accepted green QA evidence for the candidate commit",
        remedy="accept green QA evidence for this candidate commit before publishing",
    )


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
            raise CoordinatorError(
                "batch context_pressure record schema mismatch",
                remedy="the batch context_pressure record is malformed -- "
                + INTERNAL_INVARIANT_REMEDY,
            )
        body = {key: value for key, value in entry.items() if key != "record_sha256"}
        if (
            entry["record_sha256"]
            != hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
        ):
            raise CoordinatorError(
                "batch context_pressure record failed immutable integrity check",
                remedy="a context_pressure record was modified after its hash was recorded -- "
                + INTERNAL_INVARIANT_REMEDY,
            )
        numbers = (
            entry["observed_tokens"],
            entry["context_limit"],
            entry["warning_threshold"],
        )
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in numbers
            )
            or entry["warning_threshold"] > entry["context_limit"]
        ):
            raise CoordinatorError(
                "batch context_pressure record has invalid token numbers",
                remedy="the context_pressure record numbers are malformed -- "
                + INTERNAL_INVARIANT_REMEDY,
            )
        if (
            entry["level"] != operational_guards.level_for(*numbers)
            or entry["source"] not in CONTEXT_TELEMETRY_SOURCES
        ):
            raise CoordinatorError(
                "batch context_pressure level or source does not match its record",
                remedy="the context_pressure level or source is inconsistent -- "
                + INTERNAL_INVARIANT_REMEDY,
            )
    if "needs_attention" in batch:
        flag = batch["needs_attention"]
        if not isinstance(flag, bool):
            raise CoordinatorError(
                "batch needs_attention must be a boolean",
                remedy="the batch needs_attention flag is malformed -- "
                + INTERNAL_INVARIANT_REMEDY,
            )
        if flag and (
            not all(_non_empty(batch.get(field)) for field in ATTENTION_STATE_FIELDS)
            or batch["attention_reason"] not in operational_guards.ATTENTION_REASONS
        ):
            raise CoordinatorError(
                "a batch that needs attention must record its reason, time, last safe action and recommended human action",
                remedy="the batch attention state is incomplete -- "
                + INTERNAL_INVARIANT_REMEDY,
            )
    for event in batch.get("attention_events", []):
        if (
            not isinstance(event, dict)
            or event.get("event") not in ATTENTION_EVENT_KINDS
            or not _non_empty(event.get("at"))
        ):
            raise CoordinatorError(
                "batch attention_events entry is malformed",
                remedy="an attention event is malformed -- "
                + INTERNAL_INVARIANT_REMEDY,
            )
    for field in ("attention_open_keys", "attention_acknowledged"):
        if field in batch and not (
            isinstance(batch[field], list)
            and all(_non_empty(item) for item in batch[field])
        ):
            raise CoordinatorError(
                f"batch {field} must be a list of keys",
                remedy=f"the batch {field} is malformed -- "
                + INTERNAL_INVARIANT_REMEDY,
            )


def _validate_batch_integrity(root: Path, batch: JsonObject) -> None:
    _validate_operational_batch_fields(batch)
    plan = _read_object(
        _records_root(root)
        / "plans"
        / f"{_safe_id(batch.get('batch_id'), 'batch')}.json",
        "immutable batch plan",
    )
    for field in ("approval_policy", "communication_policy"):
        if (field in batch) != (field in plan):
            raise CoordinatorError(
                "batch record is incomplete",
                remedy="restore the batch record so it has every required field, or run 'ledger clean'",
            )
    for fields in (PLAN_FIELDS, LEGACY_PLAN_FIELDS):
        if all(field in batch for field in fields) and all(
            field in plan for field in fields
        ):
            if {field: batch[field] for field in fields} == {
                field: plan[field] for field in fields
            }:
                return
            raise CoordinatorError(
                "batch record does not match its immutable plan",
                remedy="the batch record diverged from its immutable plan -- "
                + INTERNAL_INVARIANT_REMEDY,
            )
    # Batch records created before approval_policy was added are still immutable and safe to
    # continue: the transition code resolves the project default when the field is absent. Accept
    # this historical shape only when both records omit the field; a one-sided omission indicates
    # corruption or an incomplete manual migration and must remain blocked.
    if (
        all(field in batch for field in PRE_APPROVAL_LEGACY_PLAN_FIELDS)
        and all(field in plan for field in PRE_APPROVAL_LEGACY_PLAN_FIELDS)
        and "approval_policy" not in batch
        and "approval_policy" not in plan
        and {field: batch[field] for field in PRE_APPROVAL_LEGACY_PLAN_FIELDS}
        == {field: plan[field] for field in PRE_APPROVAL_LEGACY_PLAN_FIELDS}
    ):
        return
    raise CoordinatorError(
        "batch record is incomplete",
        remedy="restore the batch record so it has every required field, or run 'ledger clean'",
    )


def _batch_for_ticket_branch(
    root: Path,
    ticket: str,
    branch: str,
    candidate: str,
    requested_batch: object = None,
) -> JsonObject:
    """Find the batch whose accepted QA proof is pinned to this candidate.

    A coordinator can retain abandoned planning attempts for the same ticket and issue branch.
    Those records are audit evidence, not competing QA proof, so a current SHA selects the batch
    rather than making PR preparation depend on deleting its history.
    """
    batches_dir = _records_root(root) / "batches"
    if not batches_dir.is_dir():
        raise CoordinatorError(
            "no orchestration batches exist for the ticket branch",
            remedy="verify the ticket/branch and that a batch exists for it",
        )
    matches = []
    for path in sorted(batches_dir.glob("*.json")):
        batch = _read_object(path, "batch record")
        if batch.get("ticket") == ticket and batch.get("branch") == branch:
            _validate_batch_integrity(root, batch)
            matches.append(batch)
    if not matches:
        raise CoordinatorError(
            "no orchestration batch matches the ticket and issue branch",
            remedy="pass a ticket and branch that match an existing orchestration batch",
        )
    if requested_batch is not None:
        batch_id = _safe_id(requested_batch, "batch")
        selected = next(
            (batch for batch in matches if batch.get("batch_id") == batch_id), None
        )
        if selected is None:
            raise CoordinatorError(
                "requested batch does not match the ticket and issue branch",
                remedy="pass a batch, ticket and branch that all match one existing orchestration batch",
            )
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
            raise CoordinatorError(
                "no batch has accepted green QA evidence for the candidate commit",
                remedy="accept green QA evidence for this candidate commit, or pass --batch to disambiguate",
            )
        raise CoordinatorError(
            "multiple batches have accepted green QA evidence for the candidate commit; pass --batch",
            remedy="pass --batch to disambiguate which batch's accepted QA evidence to use",
        )
    return candidates[0]


def _pending_report(root: Path, batch: JsonObject, entry: JsonObject) -> JsonObject:
    report_path = entry.get("report")
    if not isinstance(report_path, str):
        raise CoordinatorError(
            "reported dispatch has no completion report",
            remedy="the reported dispatch has no completion report on disk -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    report = _read_object(_records_root(root) / report_path, "completion report")
    expected = entry.get("report_sha256")
    actual = hashlib.sha256(_canonical(report).encode("utf-8")).hexdigest()
    if not isinstance(expected, str) or expected != actual:
        raise CoordinatorError(
            "completion report failed immutable integrity check",
            remedy="the completion report was modified after its integrity hash was recorded -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    return report


def _effective_base(batch: JsonObject) -> str:
    return cast(str, batch.get("integration_base_commit") or batch["base_commit"])


def _pinned_package_stale(
    repo: Path, root: Path, batch: JsonObject, dispatch: JsonObject
) -> bool:
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
        current_candidate is not None
        and package["candidate_commit"] != current_candidate
    )


def _validate_transition_binding(dispatch: JsonObject, batch: JsonObject) -> None:
    """The brief's transition, digest, approval, idempotency key and policy agree with one another
    and with the brief's own fields; the brief hash already proves none of them was edited alone."""
    transition = dispatch["transition"]
    if not isinstance(transition, dict) or set(transition) != set(
        operational_guards.TRANSITION_FIELDS
    ):
        raise CoordinatorError(
            "dispatch transition schema mismatch",
            remedy="the dispatch transition is malformed -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    digest = operational_guards.transition_digest(transition)
    approval = dispatch.get("coordinator_approval")
    if (
        dispatch["transition_digest"] != digest
        or not isinstance(approval, dict)
        or approval.get("transition_digest") != digest
    ):
        raise CoordinatorError(
            "dispatch transition digest does not match its transition and approval",
            remedy="the dispatch transition digest diverged from its transition or approval -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    bound = {
        "batch_id": batch.get("batch_id"),
        "next_role": dispatch["role"],
        "purpose": dispatch["purpose"],
        "candidate_sha": dispatch.get("candidate_commit"),
        "verification_commands": dispatch["verification_commands"],
        "context_package_id": dispatch.get("context_package_id"),
        "required_gates": dispatch["required_gates"],
    }
    if any(transition[field] != value for field, value in bound.items()):
        raise CoordinatorError(
            "dispatch transition does not match its brief",
            remedy="the dispatch transition diverged from its brief -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    if dispatch["retry_idempotency_key"] != _transition_idempotency_key(
        dispatch["role"], dispatch["purpose"], transition
    ):
        raise CoordinatorError(
            "dispatch retry idempotency key does not match its transition",
            remedy="the dispatch retry idempotency key diverged from its transition -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    policy = dispatch["orchestration_policy"]
    if not isinstance(policy, dict) or set(policy) != {
        "approval_ttl_seconds",
        "attention",
        "context_pressure",
        "extensions",
    }:
        raise CoordinatorError(
            "dispatch orchestration_policy is malformed",
            remedy="the dispatch orchestration_policy is malformed -- "
            + INTERNAL_INVARIANT_REMEDY,
        )


def _validate_dispatch(
    repo: Path, config: JsonObject, root: Path, batch: JsonObject, dispatch: JsonObject
) -> None:
    _validate_harness_runtime_snapshot(repo, batch)
    _reject_sensitive(dispatch, "dispatch record")
    # Briefs are immutable. A record created before worker attestation was introduced keeps its
    # historical shape and is treated as an explicit legacy opt-out instead of being rewritten.
    pre_summary_fields = DISPATCH_FIELDS - {"context_package_summary"}
    legacy_fields = DISPATCH_FIELDS - {
        "context_package_summary",
        "worker_attestation_required",
        "snapshot_commit",
        "communication_policy",
    }
    # A brief written before the canonical reporting path existed keeps its historical shape, the
    # same way every earlier field addition is treated here.
    accepted = {
        frozenset(fields)
        for fields in (DISPATCH_FIELDS, pre_summary_fields, legacy_fields)
    }
    accepted |= {fields - {"report_staging_path"} for fields in set(accepted)}
    # The role tool policy and context budget were added together, so a brief holds both or neither.
    accepted |= {
        fields - {"allowed_tools", "context_budget"} for fields in set(accepted)
    }
    # The transition-bound approval contract (issue #250) was added as one group as well.
    accepted |= {fields - POLICY_BRIEF_FIELDS for fields in set(accepted)}
    if frozenset(dispatch) not in accepted:
        raise CoordinatorError(
            "dispatch record schema mismatch",
            remedy="the dispatch record schema is malformed -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    if dispatch.get("state") != "approved":
        raise CoordinatorError(
            "dispatch record is not an approved immutable brief",
            remedy="the dispatch record has no approved coordinator_approval -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    if dispatch.get("batch_id") != batch.get("batch_id"):
        raise CoordinatorError(
            "dispatch record does not belong to its batch",
            remedy="the dispatch record does not belong to its batch -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    expected_communication_policy = batch.get(
        "communication_policy", DEFAULT_COMMUNICATION_POLICY
    )
    if (
        dispatch.get("communication_policy", expected_communication_policy)
        != expected_communication_policy
    ):
        raise CoordinatorError(
            "dispatch communication policy does not match its batch",
            remedy="the dispatch record's communication policy diverged from its batch -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    if dispatch.get("purpose") not in DISPATCH_PURPOSES:
        raise CoordinatorError(
            "dispatch record has an invalid purpose",
            remedy="the dispatch record has an invalid purpose -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    package_id = dispatch.get("context_package_id")
    package_sha = dispatch.get("context_package_sha256")
    if dispatch["role"] in {"architect", "developer", "verification", "code-review"}:
        if not isinstance(package_id, str) or not isinstance(package_sha, str):
            raise CoordinatorError(
                "architect, developer, verification and code-review briefs require a Context Package reference",
                remedy="reference a registered Context Package in the dispatch brief for this role",
            )
        package = _load_context_package(root, package_id)
        _validate_context_package(root, batch, package)
        if package.get("role") not in {"shared", dispatch["role"]}:
            raise CoordinatorError(
                "dispatch Context Package is neither shared nor assigned to this role",
                remedy="share the Context Package with this role, or register one assigned to it",
            )
        if (
            hashlib.sha256(_canonical(package).encode("utf-8")).hexdigest()
            != package_sha
        ):
            raise CoordinatorError(
                "dispatch Context Package hash does not match its immutable package",
                remedy="the dispatch's Context Package hash does not match its immutable package -- "
                + INTERNAL_INVARIANT_REMEDY,
            )
        summary = dispatch.get("context_package_summary")
        if summary is not None and summary != _context_package_summary(package):
            raise CoordinatorError(
                "dispatch Context Package summary does not match its immutable package",
                remedy="the dispatch's Context Package summary does not match its immutable package -- "
                + INTERNAL_INVARIANT_REMEDY,
            )
    elif (
        package_id is not None
        or package_sha is not None
        or dispatch.get("context_package_summary") is not None
    ):
        raise CoordinatorError(
            "only architect, developer, verification and code-review briefs may reference a Context Package",
            remedy="only reference a Context Package from an architect, developer, verification or code-review brief",
        )
    entry = next(
        (
            item
            for item in batch.get("dispatches", [])
            if item.get("dispatch_id") == dispatch.get("dispatch_id")
        ),
        None,
    )
    if (
        not entry
        or entry.get("brief_sha256")
        != hashlib.sha256(_canonical(dispatch).encode("utf-8")).hexdigest()
    ):
        raise CoordinatorError(
            "dispatch record failed immutable brief integrity check",
            remedy="the dispatch record was modified after its brief integrity hash was recorded -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    if "transition" in dispatch:
        _validate_transition_binding(dispatch, batch)
    for field in (
        "ticket",
        "branch",
        "worktree",
        "zone",
        "definition_of_done",
        "prohibited_changes",
        "required_gates",
        "dependencies",
    ):
        if dispatch[field] != batch[field]:
            raise CoordinatorError(
                f"dispatch record {field} does not match its batch",
                remedy=f"the dispatch record's {field} does not match its batch -- "
                + INTERNAL_INVARIANT_REMEDY,
            )
    expected_commands = (
        batch["developer_verification_commands"]
        if dispatch["role"] == "developer" and dispatch["purpose"] == "work"
        else batch["verification_commands"]
    )
    if dispatch["verification_commands"] != expected_commands:
        raise CoordinatorError(
            "dispatch record verification_commands do not match its batch and role",
            remedy="the dispatch record's verification_commands diverged from its batch/role -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    if _configured(repo):
        try:
            validate_brief_policy(
                dispatch,
                _project(repo),
                config,
                repo / ".harness/orchestration/roles",
            )
        except ContractError as exc:
            raise CoordinatorError(exc.message, remedy=exc.remedy) from exc
    else:
        _validate_branch(repo, dispatch["branch"])
        # In zero-config mode the brief itself is the only record of the session-supplied runtime,
        # so it is replayed here; brief_sha256 above already protects it from being edited.
        role, zone, profile_id, model, effort, transport, resolved_runtime = (
            _resolve_assignment(
                repo,
                config,
                dispatch["role"],
                batch["zone"],
                dispatch["resolved_runtime"],
                session_model=dispatch["resolved_model"],
                session_effort=dispatch["resolved_effort"],
            )
        )
        if (
            dispatch["access"] != role["mode"]
            or dispatch["resolved_provider_profile"] != profile_id
            or dispatch["resolved_model"] != model
            or dispatch["resolved_effort"] != effort
            or dispatch["resolved_transport"] != transport
            or dispatch["resolved_runtime"] != resolved_runtime
        ):
            raise CoordinatorError(
                "dispatch record does not match the role assignment",
                remedy="the dispatch record does not match the role assignment -- "
                + INTERNAL_INVARIANT_REMEDY,
            )
        expected_paths = zone["paths"] if role["mode"] == "write" else []
        if dispatch["write_paths"] != expected_paths:
            raise CoordinatorError(
                "dispatch record write paths do not match the role boundary",
                remedy="the dispatch record write paths do not match the role boundary -- "
                + INTERNAL_INVARIANT_REMEDY,
            )
    # Only the shape is checked: the brief is the immutable record of what was selected at approval,
    # so a later project edit to `tool_policy` or `context_limit` must not invalidate it in flight.
    if "allowed_tools" in dispatch:
        if not valid_tool_list(dispatch["allowed_tools"]):
            raise CoordinatorError(
                "dispatch record allowed_tools must be a non-empty list of unique tool names",
                remedy="the dispatch record allowed_tools is malformed -- "
                + INTERNAL_INVARIANT_REMEDY,
            )
        budget = dispatch["context_budget"]
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
            raise CoordinatorError(
                "dispatch record context_budget must be a positive integer",
                remedy="the dispatch record context_budget is malformed -- "
                + INTERNAL_INVARIANT_REMEDY,
            )
    candidate = dispatch.get("candidate_commit")
    if dispatch["role"] in {"verification", "code-review", "qa"} and not isinstance(
        candidate, str
    ):
        raise CoordinatorError(
            "review and QA dispatches must pin a candidate commit",
            remedy="pass --candidate-commit for a review or QA dispatch",
        )
    if dispatch["purpose"] == "publish":
        if dispatch["role"] != "developer" or not isinstance(candidate, str):
            raise CoordinatorError(
                "publish dispatches must be pinned developer briefs",
                remedy="only a pinned developer brief may be published",
            )
        _accepted_qa_for_candidate(root, batch, candidate)
    if dispatch["role"] == "verification":
        if candidate != _latest_registered_verification_candidate(repo, batch):
            raise CoordinatorError(
                "verification dispatch must pin the latest registered candidate",
                remedy="pin the candidate_commit recorded by the infrastructure retry decision",
            )
    elif candidate is not None:
        resolved = _candidate_commit(repo, candidate)
        if resolved != candidate:
            raise CoordinatorError(
                "dispatch candidate_commit must be the full resolved commit SHA",
                remedy="set the dispatch's candidate_commit to its full resolved commit SHA",
            )
        risk = _risk_for_candidate(root, batch, candidate)
        if (
            risk is None
            or dispatch.get("risk_assessment_id") != risk["risk_assessment_id"]
        ):
            raise CoordinatorError(
                "dispatch candidate is not linked to its immutable risk assessment",
                remedy="link this dispatch candidate to its immutable risk assessment before proceeding",
            )
        if (
            dispatch["role"] == "code-review"
            and dispatch.get("review_scope") != risk["review_scope"]
        ):
            raise CoordinatorError(
                "review dispatch scope does not match its immutable risk assessment",
                remedy="regenerate the review dispatch scope from its immutable risk assessment",
            )
        if (
            dispatch["role"] == "code-review"
            and dispatch.get("review_base") != risk["base_commit"]
        ):
            raise CoordinatorError(
                "review dispatch base does not match its immutable risk assessment",
                remedy="regenerate the review dispatch base from its immutable risk assessment",
            )
    elif dispatch.get("review_scope") or dispatch.get("risk_assessment_id"):
        raise CoordinatorError(
            "dispatch contains review metadata without a candidate commit",
            remedy="remove review metadata from a dispatch with no candidate_commit, or pin one",
        )


def _live_status(root: Path, dispatch_id: str) -> tuple[JsonObject, JsonObject]:
    dispatch = _load_dispatch(root, dispatch_id)
    status = _load_dispatch_status(root, dispatch_id)
    if status.get("state") not in LIVE_DISPATCH_STATES:
        raise CoordinatorError(
            "only a dispatched role can report liveness",
            remedy="only the currently dispatched role may report liveness for this dispatch",
        )
    return dispatch, status


def _transition_idempotency_key(
    role: str, purpose: str, transition: JsonObject
) -> str | None:
    keyed = operational_guards.keyed_role(role, purpose)
    if keyed is None:
        return None
    return operational_guards.retry_idempotency_key(
        role=keyed,
        candidate_sha=transition["candidate_sha"],
        base_sha=transition["base_sha"],
        review_scope=transition["review_scope"],
        reason_category=transition["reason_category"] or "none",
        verification_commands=transition["verification_commands"],
    )
