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
from harness.memory.index import context as memory_context
from harness.memory.sources import allowed_paths, read_source
from harness.orchestration import operational_guards, runtime_access
from harness.orchestration.contract import (
    REPO_MAP_TIER_ORDER,
    ContractError,
    accepted_verification_commands,
    valid_tool_list,
    validate_brief_policy,
)
from harness.orchestration.core import utils
from harness.orchestration.core.config import (
    _project,
    _reject_sensitive,
    _resolve_assignment,
    _roles_dir,
)
from harness.orchestration.core.constants import (
    ATTENTION_EVENT_KINDS,
    ATTENTION_STATE_FIELDS,
    AUTO_DECISION_FIELDS,
    AUTO_DECISION_KINDS,
    AUTO_EVIDENCE_FIELDS,
    AUTO_REPORT_FIELDS,
    AUTO_STOP_FIELDS,
    AUTO_STOP_REASONS,
    CARRIED_ITEM_BRIEF_ROLES,
    CARRIED_ITEM_FIELDS,
    CARRIED_ITEM_RECORD_FIELDS,
    CARRIED_ITEM_SOURCES,
    CHECKPOINT_NO_CONTEXT_PACKAGE,
    CONTEXT_PACKAGE_FIELDS,
    V2_CONTEXT_PACKAGE_FIELDS,
    CONTEXT_PRESSURE_FIELDS,
    CONTEXT_TELEMETRY_SOURCES,
    DEFAULT_COMMUNICATION_POLICY,
    DELTA_REVIEW_ESCALATIONS,
    DELTA_REVIEW_MODES,
    DISPATCH_FIELDS,
    DISPATCH_PURPOSES,
    LEGACY_CONTEXT_PACKAGE_FIELDS,
    LEGACY_CONTEXT_PACKAGE_FIELDS_NO_TOKENS,
    LEGACY_PLAN_FIELDS,
    LIVE_DISPATCH_STATES,
    PLAN_FIELDS,
    POLICY_BRIEF_FIELDS,
    PRE_APPROVAL_LEGACY_PLAN_FIELDS,
    PRE_SCOPE_PLAN_FIELDS,
    RECOVERY_ROUTES,
    RISK_ASSESSMENT_FIELDS,
)
from harness.orchestration.core.git_utils import (
    _candidate_commit,
    _git_is_ancestor,
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
from harness.orchestration.ledger import JsonValue
from harness.orchestration.ledger.ledger_ops import (
    _load_checkpoint,
    _load_context_package,
    _load_dispatch,
    _load_dispatch_status,
    _load_risk,
    _records_root,
)
from harness.orchestration.workflow.approval import AUTO_APPROVER


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


def _initial_developer_work(dispatch: JsonObject) -> bool:
    """Initial work owns the full plan, including progress preserved by startup recovery."""
    transition = dispatch.get("transition")
    return (
        dispatch.get("role") == "developer"
        and dispatch.get("purpose") == "work"
        and isinstance(transition, dict)
        and transition.get("next_action") in {None, "developer"}
    )


def _initial_architect_work(dispatch: JsonObject) -> bool:
    """The first architect of a batch may start from progress preserved by a replacement batch."""
    transition = dispatch.get("transition")
    return (
        dispatch.get("role") == "architect"
        and dispatch.get("purpose") == "work"
        and isinstance(transition, dict)
        and transition.get("next_action") in {"initial", "architect"}
    )


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
    if set(package) not in (
        CONTEXT_PACKAGE_FIELDS,
        V2_CONTEXT_PACKAGE_FIELDS,
        LEGACY_CONTEXT_PACKAGE_FIELDS,
        LEGACY_CONTEXT_PACKAGE_FIELDS_NO_TOKENS,
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
    summary = {
        "base_commit": package["base_commit"],
        "candidate_commit": package["candidate_commit"],
        "starting_files": package["starting_files"],
        "related_tests": package["related_tests"],
        "precedent_cards": package["precedent_cards"],
        "estimated_tokens": package.get("estimated_tokens"),
    }

    if package.get("schema_version") == 3:
        summary.update(
            {key: package[key] for key in ("goal", "definition_of_done", "memory")}
        )
    return summary


def _context_package_quality_warning(package: JsonObject) -> JsonObject | None:
    """Return the Repo Map degradation facts a human must see before approving a dispatch."""
    provenance = package.get("parser_provenance")
    if not isinstance(provenance, dict) or provenance.get("tier") == "full":
        return None
    tier = provenance.get("tier")
    degradation_reason = provenance.get("degradation_reason")
    nested_provenance = provenance.get("parser_provenance")
    if (
        not isinstance(tier, str)
        or not isinstance(degradation_reason, str)
        or not isinstance(nested_provenance, dict)
    ):
        return None
    return {
        "tier": tier,
        "degradation_reason": degradation_reason,
        "parser_provenance": nested_provenance,
    }


def _context_package_tier(package: JsonObject) -> str:
    """The actual Repo Map tier of a package, for admission comparisons.

    Mirrors `_context_package_quality_warning`'s treatment of a missing or malformed
    `parser_provenance`: a legacy package that carries none is as good as `full`, so it never
    fails a minimum-tier requirement it predates.
    """
    provenance = package.get("parser_provenance")
    if not isinstance(provenance, dict):
        return "full"
    tier = provenance.get("tier")
    if not isinstance(tier, str) or tier not in REPO_MAP_TIER_ORDER:
        return "full"
    return tier


def _reusable_context_package(
    repo: Path,
    root: Path,
    batch: JsonObject,
    base_commit: str,
    candidate_commit: str,
    memory_identity: JsonObject | None = None,
) -> JsonObject | None:
    """Return the current batch's shared package for exactly the same pinned diff.

    A package is immutable and role-neutral. Architect and developer therefore share the base
    snapshot, and a resumed worker keeps its brief's exact package ID instead of rebuilding or
    re-reading discovery. Review gets a new package only once the candidate actually changes.
    A package whose frozen memory source changed since registration is never reused: the caller
    registers a new package with current pointers and the old one stays intact for its briefs.
    """
    for entry in reversed(batch.get("context_packages", [])):
        if (
            entry.get("base_commit") != base_commit
            or entry.get("candidate_commit") != candidate_commit
        ):
            continue
        package = _load_context_package(root, entry.get("context_package_id"))
        _validate_context_package(root, batch, package)
        if memory_identity is not None and (
            package.get("schema_version") != 3
            or package.get("memory", {}).get("identity") != memory_identity
        ):
            continue
        if package.get("role") != "shared":
            continue
        if _memory_pointer_mismatch(repo, package) is not None:
            continue
        return package
    return None


def _latest_context_package(root: Path, batch: JsonObject) -> JsonObject | None:
    entries = batch.get("context_packages", [])
    if not entries:
        return None
    package = _load_context_package(root, entries[-1]["context_package_id"])
    _validate_context_package(root, batch, package)
    return package


def _memory_pointer_mismatch(repo: Path, package: JsonObject) -> JsonObject | None:
    """The first frozen memory pointer whose authoritative source no longer matches, if any.

    `actual_source_hash` is None when the source is unavailable, revoked or unreadable; `path`
    is None too when the memory policy itself cannot be read. The derived index is never
    consulted; only the source bytes decide.
    """
    pointers = package.get("memory", {}).get("pointers", [])
    if not pointers:
        return None
    current: object = None
    try:
        canonical, _, policy = memory_context(repo)
        permitted = set(allowed_paths(canonical, policy)) if policy.active else set()
        for pointer in pointers:
            current = pointer
            relative = pointer["path"]
            document = (
                read_source(canonical, relative, policy)
                if relative in permitted
                else None
            )
            if (
                document is None
                or document.source_type != pointer["source_type"]
                or document.source_hash != pointer["source_hash"]
            ):
                return {
                    "path": relative,
                    "expected_source_hash": pointer["source_hash"],
                    "actual_source_hash": document.source_hash
                    if document is not None
                    else None,
                }
    except (OSError, ValueError, KeyError, TypeError):
        known = current if isinstance(current, dict) else {}
        return {
            "path": known.get("path"),
            "expected_source_hash": known.get("source_hash"),
            "actual_source_hash": None,
        }
    return None


def _context_package_freshness(
    repo: Path,
    root: Path,
    batch: JsonObject,
    *,
    context_package_id: str | None = None,
) -> JsonObject | None:
    """Admission evidence from pinned commits and authoritative frozen source bytes.

    An explicit ID checks the package selected for admission, even when a newer package has a
    different memory identity. With no ID, retain the deliberate latest-package audit behavior.
    The derived index is neither queried nor refreshed; index-only changes are irrelevant.
    """
    if context_package_id is None:
        package = _latest_context_package(root, batch)
    else:
        package = _load_context_package(root, context_package_id)
        _validate_context_package(root, batch, package)
    if package is None:
        return None
    current_base = batch.get("integration_base_commit") or batch.get("base_commit")
    current_candidate = _current_developer_candidate(repo, root, batch)
    fresh = package["base_commit"] == current_base and (
        current_candidate is None or package["candidate_commit"] == current_candidate
    )
    mismatch = _memory_pointer_mismatch(repo, package)
    fresh = fresh and mismatch is None
    return {
        **(
            {
                "memory_diagnostic": "frozen memory source changed, unavailable or revoked",
                "memory_mismatch": mismatch,
            }
            if mismatch is not None
            else {}
        ),
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
        # A conflict-resolver (issue #534) produces the candidate of its own resolver batch.
        if (
            item.get("role") in {"developer", "conflict-resolver"}
            and item.get("purpose", "work") == "work"
        ):
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


def _retry_pinned_candidate(repo: Path, root: Path, batch: JsonObject) -> str | None:
    """The candidate of the unaccepted developer report a pending developer retry continues.

    A ``retry`` decision on a developer work report leaves its candidate unaccepted, yet the next
    developer starts from that history, not from an older accepted candidate or the base. Any other
    retried stage (code-review, QA, publish, verification) has no such candidate: ``None``."""
    if batch.get("next_action") != "developer-retry":
        return None
    previous = _last_decided_entry(batch)
    if (
        previous is None
        or previous.get("role") != "developer"
        or previous["decision"].get("decision") != "retry"
    ):
        return None
    if _load_dispatch(root, previous["dispatch_id"]).get("purpose", "work") != "work":
        return None
    report = _pending_report(root, batch, previous)
    return _candidate_commit(repo, report["commit_sha"])


def _retry_handoff(
    root: Path, batch: JsonObject, package: JsonObject | None
) -> JsonObject | None:
    """The compact handoff a pending developer retry starts from, or ``None`` for any other dispatch.

    Shaped like a checkpoint, not a session: the last developer work report (accepted or returned),
    the retry decision with its review findings, the commit plan that developer worked against and
    the Context Package ID. No chat history and no logs of failed attempts reach the retry."""
    if batch.get("next_action") != "developer-retry":
        return None
    entries = batch.get("dispatches", [])
    retried = _last_decided_entry(batch)
    if retried is None or retried["decision"].get("decision") != "retry":
        return None
    developer_report: JsonObject | None = None
    commit_plan: JsonValue = None
    for item in reversed(entries):
        if item.get("role") != "developer" or not isinstance(item.get("report"), str):
            continue
        developer_brief = _load_dispatch(root, item["dispatch_id"])
        if developer_brief.get("purpose", "work") == "work":
            developer_report = _pending_report(root, batch, item)
            commit_plan = developer_brief.get("commit_plan")
            break
    review = _pending_report(root, batch, retried).get("review")
    findings = [
        {"axis": axis, **finding}
        for axis in ("standards", "spec")
        if isinstance(review, dict) and isinstance(review.get(axis), dict)
        for finding in review[axis].get("findings", [])
    ]
    routing = retried["decision"].get("routing") or {}
    return {
        "context_package_id": package["context_package_id"]
        if package
        else CHECKPOINT_NO_CONTEXT_PACKAGE,
        "commit_plan": commit_plan,
        "developer_report": developer_report,
        "retry_decision": {
            "dispatch_id": retried["dispatch_id"],
            "role": retried.get("role"),
            "route": routing.get("route"),
            "reason_category": routing.get("reason_category"),
            "rationale": routing.get("rationale"),
            "note": retried["decision"].get("note"),
            "findings": findings,
        },
    }


def _current_developer_candidate(
    repo: Path, root: Path, batch: JsonObject
) -> str | None:
    """The candidate the next developer-side dispatch continues: the retry-pinned candidate, else
    the latest accepted developer candidate, else a superseding batch's ``start_commit`` (the
    abandoned batch's last accepted candidate, issue #506), else ``None``."""
    pinned = _retry_pinned_candidate(repo, root, batch)
    if pinned is not None:
        return pinned
    try:
        return _latest_developer_candidate(repo, root, batch)
    except CoordinatorError:
        return _superseding_start_commit(batch)


def _superseding_start_commit(batch: JsonObject) -> str | None:
    """A superseding batch's ``start_commit``: the abandoned batch's last accepted candidate
    (issue #506); ``None`` for any other batch or when only the architect was accepted."""
    link = batch.get("supersedes")
    start = link.get("start_commit") if isinstance(link, dict) else None
    return start if isinstance(start, str) else None


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
    """Whether the batch has an accepted architect: its own, or the one a superseding batch
    carried by reference from the abandoned batch with the same definition of done (issue #506)."""
    link = batch.get("supersedes")
    if isinstance(link, dict) and isinstance(link.get("architect"), dict):
        return True
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


def _dispatch_entry(batch: JsonObject, dispatch_id: str) -> JsonObject | None:
    """The batch entry of ``dispatch_id``, or ``None`` when the batch does not list it."""
    return next(
        (
            item
            for item in batch.get("dispatches", [])
            if item.get("dispatch_id") == dispatch_id
        ),
        None,
    )


def _last_decided_entry(batch: JsonObject) -> JsonObject | None:
    """The newest batch entry the coordinator decided on, or ``None`` before any decision."""
    return next(
        (
            item
            for item in reversed(batch.get("dispatches", []))
            if isinstance(item.get("decision"), dict)
        ),
        None,
    )


def _require_route(value: object, *, recorded: bool = False) -> str:
    """Return ``value`` when it is one of ``RECOVERY_ROUTES``; refuse anything else.

    ``recorded`` marks a route read back from a batch record rather than one about to be written.
    """
    if isinstance(value, str) and value in RECOVERY_ROUTES:
        return value
    allowed = ", ".join(RECOVERY_ROUTES)
    if recorded:
        raise CoordinatorError(
            f"batch routing record carries an unknown recovery route {value!r}",
            remedy=f"a recorded route is one of: {allowed}; restore routing.route to the value "
            "the coordinator recorded for that decision -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    raise CoordinatorError(
        f"the coordinator computed an unknown recovery route {value!r}",
        remedy=f"a route must be one of: {allowed} -- " + INTERNAL_INVARIANT_REMEDY,
    )


def _validate_operational_batch_fields(batch: JsonObject) -> None:
    """Shape and integrity of the batch-level records issue #250 added. Every field is optional, so a
    batch written before them stays valid; one that carries them must carry them well-formed."""
    decided = [
        entry.get("decision")
        for entry in batch.get("dispatches", [])
        if isinstance(entry, dict)
    ]
    for decision in [*decided, *batch.get("coordinator_decisions", [])]:
        routing = decision.get("routing") if isinstance(decision, dict) else None
        if isinstance(routing, dict) and "route" in routing:
            _require_route(routing["route"], recorded=True)
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
    for entry in batch.get("carried_items", []):
        if not isinstance(entry, dict) or set(entry) != CARRIED_ITEM_RECORD_FIELDS:
            raise CoordinatorError(
                "batch carried_items record schema mismatch",
                remedy="the batch carried_items record is malformed -- "
                + INTERNAL_INVARIANT_REMEDY,
            )
        body = {key: value for key, value in entry.items() if key != "record_sha256"}
        if (
            entry["record_sha256"]
            != hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
        ):
            raise CoordinatorError(
                "batch carried_items record failed immutable integrity check",
                remedy="a carried_items record was modified after its hash was recorded -- "
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
    _validate_auto_records(batch)


def _auto_record_error(field: str, problem: str) -> CoordinatorError:
    return CoordinatorError(
        f"batch {field} record {problem}",
        remedy=f"the batch {field} record is malformed or was modified after its hash was "
        "recorded -- " + INTERNAL_INVARIANT_REMEDY,
    )


def _check_sealed(field: str, entry: object, fields: frozenset[str]) -> JsonObject:
    """A hashed batch record of ``fields`` whose ``record_sha256`` still matches its body."""
    if not isinstance(entry, dict) or set(entry) != fields:
        raise _auto_record_error(field, "schema mismatch")
    body = {key: value for key, value in entry.items() if key != "record_sha256"}
    if (
        entry["record_sha256"]
        != hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
    ):
        raise _auto_record_error(field, "failed immutable integrity check")
    return entry


def _auto_decision_bound(batch: JsonObject, entry: JsonObject) -> bool:
    """Whether the approval an ``auto_decisions`` record names is the one the batch carries."""
    kind = entry["kind"]
    evidence = entry["evidence"]
    if kind == "batch-approve":
        approval = batch.get("coordinator_approval")
        return (
            isinstance(approval, dict)
            and approval.get("approved_by") == (entry["approved_by"])
        )
    if kind in {"dispatch", "decision"}:
        dispatch = next(
            (
                item
                for item in batch.get("dispatches", [])
                if isinstance(item, dict)
                and item.get("dispatch_id") == entry["dispatch_id"]
            ),
            None,
        )
        if dispatch is None:
            return False
        if kind == "dispatch":
            return bool(dispatch.get("brief_sha256") == evidence["brief_sha256"])
        decision = dispatch.get("decision")
        return (
            isinstance(decision, dict)
            and decision.get("approved_by") == entry["approved_by"]
            and dispatch.get("report_sha256") == evidence["report_sha256"]
        )
    recorded = "carry-over" if kind == "carry-over" else "continue"
    return any(
        isinstance(item, dict)
        and item.get("dispatch_id") == entry["dispatch_id"]
        and item.get("decision") == recorded
        and item.get("approved_by") == entry["approved_by"]
        for item in batch.get("coordinator_decisions", [])
    )


def _validate_auto_records(batch: JsonObject) -> None:
    """Shape, integrity and batch-internal binding of the ``approval_policy: auto`` records
    (issue #643). Every field is optional, so a batch written before them stays valid."""
    records = batch.get("auto_decisions", [])
    if not isinstance(records, list):
        raise _auto_record_error("auto_decisions", "schema mismatch")
    for position, item in enumerate(records, start=1):
        entry = _check_sealed("auto_decisions", item, AUTO_DECISION_FIELDS)
        if (
            entry["sequence"] != position
            or entry["kind"] not in AUTO_DECISION_KINDS
            or entry["approved_by"] != AUTO_APPROVER
            or not _non_empty(entry["approved_at"])
            or not _non_empty(entry["rationale"])
            or not isinstance(entry["evidence"], dict)
            or set(entry["evidence"]) != AUTO_EVIDENCE_FIELDS[entry["kind"]]
        ):
            raise _auto_record_error("auto_decisions", "has an invalid entry")
        if not _auto_decision_bound(batch, entry):
            raise _auto_record_error(
                "auto_decisions",
                f"{entry['sequence']} does not match the {entry['kind']} approval it records",
            )
    if "auto_stop" in batch:
        stop = _check_sealed("auto_stop", batch["auto_stop"], AUTO_STOP_FIELDS)
        if (
            stop["reason"] not in AUTO_STOP_REASONS.get(stop["category"], ())
            or not isinstance(stop["evidence"], dict)
            or not _non_empty(stop["detected_at"])
            or not _non_empty(stop["detected_by"])
        ):
            raise _auto_record_error("auto_stop", "names no listed stop")
    if "auto_report" in batch:
        report = _check_sealed("auto_report", batch["auto_report"], AUTO_REPORT_FIELDS)
        if report["batch_id"] != batch.get("batch_id") or report["stop"] != batch.get(
            "auto_stop"
        ):
            raise _auto_record_error(
                "auto_report", "does not match its batch or its recorded stop"
            )


def _validate_batch_integrity(root: Path, batch: JsonObject) -> None:
    _validate_operational_batch_fields(batch)
    plan = _read_object(
        _records_root(root)
        / "plans"
        / f"{_safe_id(batch.get('batch_id'), 'batch')}.json",
        "immutable batch plan",
    )
    if batch.get("goal") != plan.get("goal"):
        raise CoordinatorError(
            "batch goal does not match its immutable plan",
            remedy=INTERNAL_INVARIANT_REMEDY,
        )
    # The link of a superseding batch (issue #506); a batch planned without one has none on both.
    if batch.get("supersedes") != plan.get("supersedes"):
        raise CoordinatorError(
            "batch supersedes link does not match its immutable plan",
            remedy="the batch supersedes link diverged from its immutable plan -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    for field in ("approval_policy", "communication_policy", "allowed_paths"):
        if (field in batch) != (field in plan):
            raise CoordinatorError(
                "batch record is incomplete",
                remedy="restore the batch record so it has every required field, or run 'ledger clean'",
            )
    # A batch planned before explicit scopes existed has no allowed_paths on either record and was
    # bounded by its zone; both stay valid. A scope present on both records must be identical.
    if batch.get("allowed_paths") != plan.get("allowed_paths"):
        raise CoordinatorError(
            "batch record does not match its immutable plan",
            remedy="the batch record diverged from its immutable plan -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    for fields in (PLAN_FIELDS, PRE_SCOPE_PLAN_FIELDS, LEGACY_PLAN_FIELDS):
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


def _batches_for_ticket_branch(
    root: Path, ticket: str, branch: str
) -> list[JsonObject]:
    """Every integrity-checked batch recorded for this ticket and issue branch.

    A coordinator can retain abandoned planning attempts for the same ticket and issue branch;
    they are audit evidence, so this lists them all and leaves the choice to the caller.
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
    return matches


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
    matches = _batches_for_ticket_branch(root, ticket, branch)
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
    current base or current developer candidate."""
    package_id = dispatch.get("context_package_id")
    if not isinstance(package_id, str):
        return False
    package = _load_context_package(root, package_id)
    current_candidate = _current_developer_candidate(repo, root, batch)
    return package["base_commit"] != _effective_base(batch) or (
        current_candidate is not None
        and package["candidate_commit"] != current_candidate
    )


def _validate_transition_binding(dispatch: JsonObject, batch: JsonObject) -> None:
    """The brief's transition, digest, approval, idempotency key and policy agree with one another
    and with the brief's own fields; the brief hash already proves none of them was edited alone."""
    transition = dispatch["transition"]
    if not isinstance(transition, dict) or set(transition) - {
        *operational_guards.OPTIONAL_TRANSITION_FIELDS,
        operational_guards.ACCESS_TRANSITION_FIELD,
    } != set(operational_guards.TRANSITION_FIELDS):
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
    # A non-empty carried-items section is part of what was approved (issue #499); an empty or
    # absent one binds nothing, so every earlier transition keeps its digest.
    carried = dispatch.get("carried_items")
    expected_carried = (
        operational_guards.carried_items_digest(carried) if carried else None
    )
    if transition.get("carried_items_sha256") != expected_carried:
        raise CoordinatorError(
            "dispatch transition does not match its carried items",
            remedy="the dispatch transition diverged from its carried items -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    # The rebase target a human approved for a rebase-fix-forward developer-retry (issue #504).
    if transition.get("rebase_target_sha") != dispatch.get("rebase_target_commit"):
        raise CoordinatorError(
            "dispatch transition does not match its rebase target",
            remedy="the dispatch transition diverged from its rebase_target_commit -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    # The delta-or-full choice of a code-review after a fix-forward (issue #625).
    scope = dispatch.get("delta_review_scope")
    expected_scope = (
        operational_guards.delta_review_digest(scope)
        if isinstance(scope, dict)
        else None
    )
    if transition.get("delta_review_sha256") != expected_scope:
        raise CoordinatorError(
            "dispatch transition does not match its delta-review scope",
            remedy="the dispatch transition diverged from its delta_review_scope -- "
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
    if not isinstance(policy, dict) or set(policy) - {"infrastructure_retry"} != {
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
    from harness.orchestration.infrastructure_retry import pinned

    pinned(dispatch)
    liveness = dispatch.get("liveness")
    if liveness is not None and (
        not isinstance(liveness, dict)
        or set(liveness) != {"heartbeat_every_seconds", "stale_after_seconds"}
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in liveness.values()
        )
        or liveness["heartbeat_every_seconds"] > liveness["stale_after_seconds"]
    ):
        raise CoordinatorError(
            "dispatch liveness policy is malformed",
            remedy="the dispatch liveness policy is malformed -- "
            + INTERNAL_INVARIANT_REMEDY,
        )


def _validate_carried_section(dispatch: JsonObject) -> None:
    """A brief's carried-items section: one list of items per known source kind, only on a work
    brief of a role that kind reaches, each item id carried once (issues #499, #501)."""
    section = dispatch["carried_items"]
    items = (
        [item for kind in section.values() if isinstance(kind, list) for item in kind]
        if isinstance(section, dict)
        else []
    )
    ids = [item.get("item_id") for item in items if isinstance(item, dict)]
    if (
        not isinstance(section, dict)
        or not set(section) <= set(CARRIED_ITEM_SOURCES)
        or any(not isinstance(kind, list) or not kind for kind in section.values())
        or any(
            not isinstance(item, dict) or set(item) != CARRIED_ITEM_FIELDS
            for item in items
        )
        or len(set(ids)) != len(items)
        or (section and dispatch.get("purpose") != "work")
        or any(
            dispatch.get("role") not in CARRIED_ITEM_BRIEF_ROLES[kind]
            for kind in section
        )
    ):
        raise CoordinatorError(
            "dispatch carried_items must map known source kinds to their items, on a work brief "
            "of a role each kind reaches only",
            remedy="the dispatch record's carried_items is malformed -- "
            + INTERNAL_INVARIANT_REMEDY,
        )


def _validate_delta_review_scope(dispatch: JsonObject) -> None:
    """A brief's ``delta_review_scope`` (issue #625): ``null``, or on a code-review work brief
    without ``delta_review_of`` an object whose ``mode`` is ``delta`` exactly when it records no
    escalation, each escalation naming a known reason and its evidence."""
    scope = dispatch.get("delta_review_scope")
    if scope is None:
        return
    escalations = scope.get("escalations") if isinstance(scope, dict) else None
    if (
        not isinstance(scope, dict)
        or (dispatch.get("role"), dispatch.get("purpose")) != ("code-review", "work")
        or dispatch.get("delta_review_of") is not None
        or scope.get("mode") not in DELTA_REVIEW_MODES
        or not isinstance(escalations, list)
        or (scope["mode"] == "delta") != (not escalations)
        or any(
            not isinstance(escalation, dict)
            or set(escalation) != {"reason", "evidence"}
            or escalation["reason"] not in DELTA_REVIEW_ESCALATIONS
            or not isinstance(escalation["evidence"], list)
            or not escalation["evidence"]
            for escalation in escalations
        )
    ):
        raise CoordinatorError(
            "dispatch delta_review_scope must be null or, on a code-review work brief without "
            "delta_review_of, a delta or full scope whose escalations name known reasons",
            remedy="the dispatch record's delta_review_scope is malformed -- "
            + INTERNAL_INVARIANT_REMEDY,
        )


def _validate_resolver_section(dispatch: JsonObject) -> None:
    """A conflict-resolver brief carries its complete ``resolver`` section (issue #534); every
    other brief carries none."""
    section = dispatch.get("resolver")
    if dispatch.get("role") != "conflict-resolver":
        if "resolver" in dispatch:
            raise CoordinatorError(
                "only a conflict-resolver brief may carry a resolver section",
                remedy="the dispatch record's resolver section is malformed -- "
                + INTERNAL_INVARIANT_REMEDY,
            )
        return
    needed = (
        "ticket",
        "sides",
        "candidate_sha",
        "target_sha",
        "scope",
        "prohibitions",
        "commit_plan",
        "checks",
        "budget",
        "report_staging_path",
    )
    if not isinstance(section, dict) or any(key not in section for key in needed):
        raise CoordinatorError(
            "a conflict-resolver brief must carry the complete resolver section",
            remedy="the dispatch record's resolver section is malformed -- "
            + INTERNAL_INVARIANT_REMEDY,
        )


def _validate_dispatch(
    repo: Path, config: JsonObject, root: Path, batch: JsonObject, dispatch: JsonObject
) -> None:
    _validate_harness_runtime_snapshot(repo, batch)
    _reject_sensitive(dispatch, "dispatch record")
    try:
        runtime_access.validate_binding(dispatch)
    except runtime_access.AccessError as exc:
        raise CoordinatorError(exc.message, remedy=exc.remedy) from exc
    # Briefs are immutable. A record created before worker attestation was introduced keeps its
    # historical shape and is treated as an explicit legacy opt-out instead of being rewritten.
    pre_summary_fields = DISPATCH_FIELDS - {"context_package_summary"}
    legacy_fields = DISPATCH_FIELDS - {
        "context_package_summary",
        "worker_attestation_required",
        "snapshot_commit",
        "communication_policy",
        "commit_plan",
        "liveness",
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
    # The code-review brief's commit-plan divergence (issue #478) came later than all of them.
    accepted |= {fields - {"commit_plan_divergence"} for fields in set(accepted)}
    # The carried-items section (issue #499) came after that.
    accepted |= {fields - {"carried_items"} for fields in set(accepted)}
    # The approved rebase target (issue #504) came after that.
    accepted |= {fields - {"rebase_target_commit"} for fields in set(accepted)}
    # Runtime access is added as an approval-bound group; historical briefs keep inherit.
    accepted |= {fields - {"runtime_access"} for fields in set(accepted)}
    # The delta-review scope of a code-review after a fix-forward (issue #625) came after that.
    accepted |= {fields - {"delta_review_scope"} for fields in set(accepted)}
    # The conflict-resolver brief's resolver section (issue #534) is the one field added on top.
    accepted |= {fields | {"resolver"} for fields in set(accepted)}
    if frozenset(dispatch) not in accepted:
        raise CoordinatorError(
            "dispatch record schema mismatch",
            remedy="the dispatch record schema is malformed -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    divergence = dispatch.get("commit_plan_divergence")
    if divergence is not None and (
        dispatch.get("role") != "code-review" or not isinstance(divergence, dict)
    ):
        raise CoordinatorError(
            "dispatch commit_plan_divergence must be null or, on a code-review brief, an object",
            remedy="the dispatch record's commit_plan_divergence is malformed -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    if "carried_items" in dispatch:
        _validate_carried_section(dispatch)
    _validate_delta_review_scope(dispatch)
    _validate_resolver_section(dispatch)
    target = dispatch.get("rebase_target_commit")
    if target is not None and (
        not isinstance(target, str)
        or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", target) is None
        or (dispatch.get("role"), dispatch.get("purpose")) != ("developer", "work")
        or not isinstance(dispatch.get("transition"), dict)
        or dispatch["transition"].get("next_action") != "developer-retry"
    ):
        raise CoordinatorError(
            "dispatch rebase_target_commit must be null or, on a developer-retry work brief, "
            "a full commit SHA",
            remedy="the dispatch record's rebase_target_commit is malformed -- "
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
    accepted_commands = accepted_verification_commands(
        batch, dispatch["role"], dispatch["purpose"]
    )
    if dispatch["verification_commands"] not in accepted_commands:
        raise CoordinatorError(
            "dispatch record verification_commands do not match its batch and role",
            remedy="the dispatch record's verification_commands diverged from its batch/role -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    if config.get("assignment_plans"):
        try:
            _, assignment = validate_brief_policy(
                dispatch,
                _project(repo),
                config,
                _roles_dir(repo),
            )
        except ContractError as exc:
            raise CoordinatorError(exc.message, remedy=exc.remedy) from exc
        ceiling = assignment["write_ceiling"]
    else:
        _validate_branch(repo, dispatch["branch"])
        # In zero-config mode the brief itself is the only record of the session-supplied runtime,
        # so it is replayed here; brief_sha256 above already protects it from being edited.
        role, ceiling, profile_id, model, effort, transport, resolved_runtime = (
            _resolve_assignment(
                repo,
                config,
                dispatch["role"],
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
    # The brief's write scope is the batch's explicit scope; a batch planned before scopes existed
    # was bounded by the role's own write ceiling, which is what its brief recorded.
    expected_paths = (
        batch.get("allowed_paths", ceiling) if dispatch["access"] == "write" else []
    )
    if dispatch["write_paths"] != expected_paths:
        raise CoordinatorError(
            "dispatch record write paths do not match the batch scope",
            remedy="the dispatch record write paths do not match the batch scope -- "
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
            and (_initial_developer_work(dispatch) or _initial_architect_work(dispatch))
            and dispatch.get("risk_assessment_id") is None
            and dispatch.get("review_base") is None
            and not dispatch.get("review_scope")
        ):
            # A writer's startup SHA is progress, not a completed/accepted candidate; the
            # architect that precedes the writer in a replacement batch starts from it too.
            # Report acceptance still precedes risk assessment and every downstream gate.
            if not _git_is_ancestor(repo, batch["base_commit"], candidate):
                raise CoordinatorError(
                    "initial developer snapshot must contain the batch base commit",
                    remedy="pin existing issue-branch progress that descends from the batch base",
                )
            return
        if (
            risk is None
            or dispatch.get("risk_assessment_id") != risk["risk_assessment_id"]
        ):
            raise CoordinatorError(
                "dispatch candidate is not linked to its immutable risk assessment",
                remedy=(
                    f"if candidate {candidate} has no risk assessment, register one with 'risk "
                    f"assess --batch {batch['batch_id']} --candidate-commit {candidate} "
                    "--changed-file <path>...' while the batch awaits approval; then propose and "
                    "create a new dispatch (cancel an unsent brief first)"
                ),
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
