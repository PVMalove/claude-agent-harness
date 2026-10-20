"""Risk assessment of a candidate commit.

The code-review role's manifest owns the trigger vocabulary; this module matches a candidate's
diff and commit messages against it and records the resulting assessment.  A trigger decides
whether review is required and how wide its scope is -- it never decides the review's verdict.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from harness.errors import INTERNAL_INVARIANT_REMEDY
from harness.orchestration.core import utils
from harness.orchestration.core.constants import ACCEPTED_DECISIONS
from harness.orchestration.core.config import (
    _reject_sensitive,
    _role,
)
from harness.orchestration.core.git_utils import (
    _candidate_commit,
    _changed_files_between,
    _commit_changed_files,
    _commit_evidence,
    _git_is_ancestor,
)
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _canonical,
    _non_empty,
    _repo,
    _safe_id,
    _strings,
)
from harness.orchestration.ledger.ledger_ops import (
    _ledger_lock,
    _load_batch,
    _load_dispatch,
    _load_risk,
    _replace_record,
    _state_root,
    _write_record,
)
from harness.orchestration.ledger.lifecycle import (
    BatchRecord,
    LifecycleLedger,
    RiskAssessmentRecord,
)
from harness.orchestration.ledger.recovery_chain import active_dispatches
from harness.orchestration.workflow import rebase
from harness.orchestration.workflow.carried_items import (
    open_coordinator_findings,
    open_incomplete_items,
)
from harness.orchestration.workflow.history import (
    _latest_developer_candidate,
    _retry_pinned_candidate,
    _validate_batch_integrity,
)


def _risk_triggers(repo: Path) -> list[str]:
    role = _role(repo, "code-review")
    triggers = role.get("risk_triggers")
    if not isinstance(triggers, list) or not all(_non_empty(item) for item in triggers):
        raise CoordinatorError(
            "code-review role has no valid risk triggers",
            remedy="add the code-review role's required risk triggers to its role manifest",
        )
    return list(triggers)


def _validate_trigger_names(
    triggers: object, label: str, known: list[str]
) -> list[str]:
    values = _strings(triggers, label, allow_empty=True)
    unknown = [trigger for trigger in values if trigger not in known]
    if unknown:
        raise CoordinatorError(
            f"{label} contains unknown risk triggers: {unknown}",
            remedy=f"remove the unknown trigger(s) from {label}, or add them to the project's known risk triggers",
        )
    return list(dict.fromkeys(values))


def _matching_triggers(text: str, known: list[str]) -> list[str]:
    normalized = text.casefold()
    return [
        trigger
        for trigger in known
        if trigger in normalized
        or any(
            re.search(pattern, normalized, re.IGNORECASE)
            for pattern in _trigger_patterns(trigger)
        )
    ]


def _trigger_patterns(trigger: str) -> tuple[str, ...]:
    words = [
        word
        for word in re.split(r"[^a-z0-9]+", trigger.casefold())
        if word and word not in {"change", "changes"}
    ]
    patterns = [rf"\b{re.escape(word)}\b" for word in words]
    for word in words:
        if word.endswith("s") and not word.endswith(("is", "us", "ss")):
            patterns.append(rf"\b{re.escape(word[:-1])}s?\b")
    joined = set(words)
    if "api" in joined:
        patterns.extend((r"\bopenapi\b", r"endpoint", r"public[ _-]+contract"))
    if "migration" in joined:
        patterns.append(r"\bmigrate\b")
    if "message" in joined or "routing" in joined:
        patterns.extend((r"\bmessaging\b", r"\broute\b"))
    if "transaction" in joined:
        patterns.append(r"\batomic\b")
    if "authorization" in joined:
        patterns.extend((r"authori[sz]", r"security", r"permission", r"credential"))
    if "concurrency" in joined:
        patterns.extend((r"concurr", r"parallel", r"lock", r"retry"))
    if "retry" in joined:
        patterns.extend((r"\bdlq\b", r"dead[- ]letter"))
    return tuple(patterns)


def _candidate_changed_files(repo: Path, base: str | None, candidate: str) -> list[str]:
    """The files ``candidate`` changes against ``base`` (``rebase.batch_base``), as a risk
    assessment measures them."""
    return (
        _changed_files_between(repo, base, candidate)
        if base
        else _commit_changed_files(repo, candidate)
    )


# Risk-reassessment policy (issue #781).  A batch is in exactly one of three states, and the pure
# functions below are the only place that decides how an event moves it; recovery, reports and
# assess apply the result and call the guard before their first ledger write.
#   idle        no reassessment is owed;
#   bound(c, T) the candidate ``c`` must be risk-assessed again, covering the triggers ``T``;
#   pending     a reassessment is owed, but the writer candidate it concerns is not known yet
#               (``risk_reassessment_pending_next``).  A legacy record that has the requirement and
#               neither a candidate nor the marker is read as pending.
REASSESSMENT_FIELDS = (
    "risk_reassessment_required",
    "risk_reassessment_candidate",
    "risk_reassessment_triggers",
    "risk_reassessment_pending_next",
)
IDLE, BOUND, PENDING = "idle", "bound", "pending"
# What happened: a rewind past the risk-invalidating gates, a rewind to verification (the registered
# candidate stays), a rewind to a writer gate, a completed writer report, or a risk assessment.
REWIND_CLEAR = "rewind-clear"
REWIND_VERIFICATION = "rewind-verification"
REWIND_WRITER = "rewind-writer"
WRITER_REPORT = "writer-report"
ASSESSMENT = "assessment"
# Event -> state before -> states that may follow.  The requirement leaves ``bound`` or ``pending``
# only by an assessment of the bound candidate or by a rewind past the risk-invalidating gates.
_ALLOWED_AFTER: dict[str, dict[str, frozenset[str]]] = {
    REWIND_CLEAR: {
        IDLE: frozenset({IDLE}),
        BOUND: frozenset({IDLE}),
        PENDING: frozenset({IDLE}),
    },
    REWIND_VERIFICATION: {
        IDLE: frozenset({IDLE, BOUND}),
        BOUND: frozenset({BOUND}),
        PENDING: frozenset({BOUND}),
    },
    REWIND_WRITER: {
        IDLE: frozenset({IDLE, PENDING}),
        BOUND: frozenset({PENDING}),
        PENDING: frozenset({PENDING}),
    },
    WRITER_REPORT: {
        IDLE: frozenset({IDLE, BOUND}),
        BOUND: frozenset({BOUND}),
        PENDING: frozenset({BOUND}),
    },
    ASSESSMENT: {
        IDLE: frozenset({IDLE}),
        BOUND: frozenset({IDLE, BOUND}),
        PENDING: frozenset({PENDING}),
    },
}


@dataclass(frozen=True)
class Reassessment:
    kind: str
    candidate: str | None = None
    triggers: tuple[str, ...] = ()


def reassessment_fields(batch: JsonObject) -> JsonObject:
    """The four batch fields that carry the state, as stored (absent keys stay absent)."""
    return {name: batch[name] for name in REASSESSMENT_FIELDS if name in batch}


def reassessment_state(batch: JsonObject) -> Reassessment:
    if not batch.get("risk_reassessment_required"):
        return Reassessment(IDLE)
    candidate = batch.get("risk_reassessment_candidate")
    if not isinstance(candidate, str) or not candidate:
        return Reassessment(PENDING)
    triggers = batch.get("risk_reassessment_triggers", [])
    return Reassessment(
        BOUND, candidate, tuple(triggers) if isinstance(triggers, list) else ()
    )


def _stored_kind(fields: JsonObject) -> str | None:
    """The state a set of stored fields spells out, or ``None`` when it is not a legal record."""
    candidate = fields.get("risk_reassessment_candidate")
    triggers = fields.get("risk_reassessment_triggers")
    marker = fields.get("risk_reassessment_pending_next")
    if not fields.get("risk_reassessment_required"):
        legal = fields.get("risk_reassessment_required") in (None, False)
        return IDLE if legal and not (candidate or triggers or marker) else None
    if fields.get("risk_reassessment_required") is not True:
        return None
    if marker is True and candidate is None and triggers is None:
        return PENDING
    if (
        marker is None
        and isinstance(candidate, str)
        and candidate
        and isinstance(triggers, list)
        and all(isinstance(item, str) for item in triggers)
    ):
        return BOUND
    return None


def apply_reassessment(batch: JsonObject, state: Reassessment) -> None:
    """Write ``state`` into ``batch``, removing the keys the state does not use."""
    batch["risk_reassessment_required"] = state.kind != IDLE
    if state.kind == BOUND:
        batch["risk_reassessment_candidate"] = state.candidate
        batch["risk_reassessment_triggers"] = list(state.triggers)
    else:
        batch.pop("risk_reassessment_candidate", None)
        batch.pop("risk_reassessment_triggers", None)
    if state.kind == PENDING:
        batch["risk_reassessment_pending_next"] = True
    else:
        batch.pop("risk_reassessment_pending_next", None)


def next_after_rewind(
    state: Reassessment,
    *,
    event: str,
    superseded: bool,
    verification_candidate: str | None = None,
) -> Reassessment:
    """The state after a rewind.  ``event`` is ``REWIND_CLEAR``, ``REWIND_VERIFICATION`` or
    ``REWIND_WRITER``; ``superseded`` says the rewind retires risk assessments.  A rewind to or
    before verification never discharges an owed reassessment, so a second rewind cannot turn
    ``pending`` into ``idle``."""
    if event == REWIND_CLEAR:
        return Reassessment(IDLE)
    if state.kind == IDLE and not superseded:
        return state
    if event == REWIND_VERIFICATION:
        if not verification_candidate:
            raise CoordinatorError(
                "a rewind to verification needs the registered candidate",
                remedy=INTERNAL_INVARIANT_REMEDY,
            )
        return Reassessment(BOUND, verification_candidate)
    return Reassessment(PENDING)


def next_after_writer_report(
    state: Reassessment, candidate: str, triggers: tuple[str, ...]
) -> Reassessment:
    """The state after a completed writer report.  A writer report never discharges the
    requirement: it binds its candidate (and the triggers it escalates) to it."""
    if triggers:
        return Reassessment(BOUND, candidate, triggers)
    if state.kind == BOUND and state.candidate == candidate:
        return state
    if state.kind == BOUND:
        return Reassessment(BOUND, candidate, state.triggers)
    if state.kind == PENDING:
        return Reassessment(BOUND, candidate)
    return state


def next_after_assessment(
    state: Reassessment, candidate: str, matched: list[str]
) -> Reassessment:
    """The state after assessing ``candidate``: only the bound candidate, with every owed trigger
    matched, discharges the requirement."""
    if (
        state.kind == BOUND
        and state.candidate == candidate
        and set(state.triggers) <= set(matched)
    ):
        return Reassessment(IDLE)
    return state


def require_legal_reassessment(
    before: JsonObject, batch: JsonObject, event: str
) -> None:
    """Refuse an illegal state or transition, before the command writes anything.

    ``before`` is ``reassessment_fields`` taken before the command changed ``batch``.  Fields
    carried over unchanged pass: a legacy record is read as ``pending`` and repaired by migration,
    not here."""
    after = reassessment_fields(batch)
    if after == before:
        return
    kind = _stored_kind(after)
    if kind is None:
        raise CoordinatorError(
            f"illegal risk reassessment state after {event}: {after}",
            remedy="a risk reassessment is idle, bound to a candidate, or pending the next writer "
            "report; " + INTERNAL_INVARIANT_REMEDY,
        )
    previous = reassessment_state(before).kind
    if kind not in _ALLOWED_AFTER[event][previous]:
        raise CoordinatorError(
            f"illegal risk reassessment transition {previous} -> {kind} on {event}",
            remedy=INTERNAL_INVARIANT_REMEDY,
        )


def _accepted_review_covers(
    root: Path,
    batch: JsonObject,
    candidate: str,
    matched: list[str],
    changed_files: list[str],
) -> bool:
    """Whether an active, accepted code review of ``candidate`` was made against an assessment of
    the same diff whose triggers include every trigger ``matched`` now.  Assessing such a candidate
    again adds no review obligation, so the handoff stays QA instead of looping back to review."""
    for item in active_dispatches(batch):
        if (
            item.get("role") != "code-review"
            or item.get("state") != "reported"
            or item.get("decision", {}).get("decision") not in ACCEPTED_DECISIONS
        ):
            continue
        brief = _load_dispatch(root, item["dispatch_id"])
        risk_id = brief.get("risk_assessment_id")
        if brief.get("candidate_commit") != candidate or not isinstance(risk_id, str):
            continue
        reviewed = _load_risk(root, risk_id)
        if (
            reviewed.get("candidate_commit") == candidate
            and reviewed.get("changed_files") == changed_files
            and set(matched) <= set(reviewed.get("matched_triggers", []))
        ):
            return True
    return False


def assess_risk(args: argparse.Namespace) -> JsonObject:
    repo = _repo(args)
    root = _state_root(args, repo)
    known = _risk_triggers(repo)
    candidate = _candidate_commit(repo, args.candidate_commit)
    changed_files = [
        item.replace("\\", "/") for item in _strings(args.changed_file, "changed_files")
    ]
    developer_triggers = _validate_trigger_names(
        args.developer_trigger or [], "developer_triggers", known
    )
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        batch = _load_batch(root, args.batch)
        _validate_batch_integrity(root, batch)
        # `report complete` assesses a recorded report's candidate only while its batch still
        # awaits that assessment, so a repeated or concurrent completion never records it twice.
        expected = getattr(args, "_expected_next_action", None)
        if expected is not None and batch.get("next_action") != expected:
            raise CoordinatorError(
                "the batch no longer awaits the risk assessment of this report "
                f"(next_action is {batch.get('next_action')!r}); it is already recorded or the "
                "batch has moved on",
                remedy="run report complete again; it skips a step that is already recorded",
            )
        if batch.get("state") != "awaiting-approval":
            raise CoordinatorError(
                "risk assessment requires a batch awaiting coordinator approval",
                remedy="move the batch to awaiting coordinator approval before registering a risk assessment",
            )
        base = rebase.batch_base(repo, batch)
        if args.base_commit:
            requested_base = _candidate_commit(repo, args.base_commit)
            if requested_base != base:
                raise CoordinatorError(
                    "risk assessment base must match the batch-captured base commit",
                    remedy=f"omit --base-commit, or pass the batch base commit {base}",
                )
        if base and not _git_is_ancestor(repo, base, candidate):
            raise CoordinatorError(
                "risk assessment base must be an ancestor of the candidate commit",
                remedy="pass a risk assessment base that is an ancestor of candidate_commit",
            )
        if _candidate_changed_files(repo, base, candidate) != changed_files:
            raise CoordinatorError(
                "changed_files must exactly match the candidate diff",
                remedy="regenerate changed_files from the actual diff for candidate_commit",
            )
        # While a developer retry is pending, only the candidate it continues may be assessed:
        # the assessment links that candidate to an explicitly pinned retry brief and is never a
        # route to review or QA.
        pinned = _retry_pinned_candidate(repo, root, batch)
        if pinned is not None:
            if candidate != pinned:
                raise CoordinatorError(
                    "candidate commit does not match the developer report the pending retry continues",
                    remedy=f"pass --candidate-commit {pinned}, the candidate of the retried developer report",
                )
        elif _latest_developer_candidate(repo, root, batch) != candidate:
            raise CoordinatorError(
                "candidate commit does not match the accepted developer report",
                remedy="pass the candidate_commit from the accepted developer report",
            )
        inherited_triggers = {
            trigger
            for escalation in batch.get("risk_escalations", [])
            if escalation.get("candidate_commit") == candidate
            for trigger in escalation.get("triggers", [])
        }
        for trigger in sorted(inherited_triggers):
            if trigger not in developer_triggers:
                developer_triggers.append(trigger)
        evidence = (
            " ".join(batch["definition_of_done"] + changed_files)
            + "\n"
            + _commit_evidence(repo, base, candidate)
        )
        matched = _matching_triggers(evidence, known)
        for trigger in developer_triggers:
            if trigger not in matched:
                matched.append(trigger)
        risk = {
            "risk_assessment_id": f"risk-{uuid.uuid4()}",
            "batch_id": batch["batch_id"],
            "candidate_commit": candidate,
            "base_commit": base,
            "changed_files": changed_files,
            "matched_triggers": matched,
            "developer_triggers": developer_triggers,
            # A resolver batch holds only a conflict resolution, which adds no behaviour: the
            # repeat review is waived, and QA of the new pair still has to pass (issue #534).
            "review_required": bool(matched) and batch.get("kind") != "resolver",
            "review_scope": list(changed_files),
            "created_at": utils._now(),
        }
        _reject_sensitive(risk, "risk assessment")
        _safe_id(risk["risk_assessment_id"], "risk assessment")
        # Decided and checked before the first ledger write: only the bound candidate, with every
        # owed trigger matched, discharges the requirement.
        before = reassessment_state(batch)
        after = next_after_assessment(before, candidate, matched)
        proposed = dict(batch)
        if after != before:
            apply_reassessment(proposed, after)
        require_legal_reassessment(reassessment_fields(batch), proposed, ASSESSMENT)
        _write_record(ledger, RiskAssessmentRecord.from_dict(risk))
        batch.setdefault("risk_assessments", []).append(
            {
                "risk_assessment_id": risk["risk_assessment_id"],
                "candidate_commit": candidate,
                "matched_triggers": matched,
                "review_required": risk["review_required"],
                "record_sha256": hashlib.sha256(
                    _canonical(risk).encode("utf-8")
                ).hexdigest(),
            }
        )
        if after != before:
            apply_reassessment(batch, after)
        # Assessment is evidence, not a launch instruction.  It makes the one allowed next
        # handoff visible to the coordinator; a later, separately approved dispatch creates the
        # immutable brief.  A pending developer retry stays the next handoff.  An open coordinator
        # finding (issue #499) or an incomplete item handed to code-review (issue #501) is a review
        # obligation of its own: the candidate goes to code-review even when no trigger matched,
        # and the risk record is left as assessed.
        if pinned is None:
            review = (
                (
                    risk["review_required"]
                    and not _accepted_review_covers(
                        root, batch, candidate, matched, changed_files
                    )
                )
                or bool(open_coordinator_findings(root, batch))
                or bool(open_incomplete_items(root, batch, "code-review"))
            )
            batch["next_action"] = "code-review" if review else "qa"
        _safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, BatchRecord.from_dict(batch))
    return risk
