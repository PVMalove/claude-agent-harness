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
from pathlib import Path

from harness.errors import INTERNAL_INVARIANT_REMEDY
from harness.orchestration.core import config as core_config, utils
from harness.orchestration.ledger.lifecycle import BatchRecord, LifecycleLedger, RiskAssessmentRecord
from harness.orchestration.core.utils import (
    CoordinatorError, JsonObject, _canonical, _non_empty, _repo, _safe_id, _strings,
)
from harness.orchestration.core.git_utils import (
    _candidate_commit, _changed_files_between, _commit_changed_files, _commit_evidence, _git_is_ancestor,
)
from harness.orchestration.core.config import (
    _reject_sensitive, _role,
)
from harness.orchestration.ledger.ledger_ops import (
    _ledger_lock, _load_batch, _replace_record, _state_root, _write_record,
)
from harness.orchestration.workflow.history import (
    _latest_developer_candidate, _validate_batch_integrity,
)
def _risk_triggers(repo: Path) -> list[str]:
    role = _role(repo, "code-review")
    triggers = role.get("risk_triggers")
    if not isinstance(triggers, list) or not all(_non_empty(item) for item in triggers):
        raise CoordinatorError("code-review role has no valid risk triggers", remedy="add the code-review role's required risk triggers to its role manifest")
    return list(triggers)


def _validate_trigger_names(triggers: object, label: str, known: list[str]) -> list[str]:
    values = _strings(triggers, label, allow_empty=True)
    unknown = [trigger for trigger in values if trigger not in known]
    if unknown:
        raise CoordinatorError(f"{label} contains unknown risk triggers: {unknown}", remedy=f"remove the unknown trigger(s) from {label}, or add them to the project's known risk triggers")
    return list(dict.fromkeys(values))


def _matching_triggers(text: str, known: list[str]) -> list[str]:
    normalized = text.casefold()
    return [
        trigger
        for trigger in known
        if trigger in normalized
        or any(re.search(pattern, normalized, re.IGNORECASE) for pattern in _trigger_patterns(trigger))
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


def assess_risk(args: argparse.Namespace) -> JsonObject:
    repo = _repo(args)
    root = _state_root(args, repo)
    known = _risk_triggers(repo)
    candidate = _candidate_commit(repo, args.candidate_commit)
    changed_files = [item.replace("\\", "/") for item in _strings(args.changed_file, "changed_files")]
    developer_triggers = _validate_trigger_names(args.developer_trigger or [], "developer_triggers", known)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        batch = _load_batch(root, args.batch)
        _validate_batch_integrity(root, batch)
        if batch.get("state") != "awaiting-approval":
            raise CoordinatorError("risk assessment requires a batch awaiting coordinator approval", remedy="move the batch to awaiting coordinator approval before registering a risk assessment")
        base = batch.get("base_commit")
        if args.base_commit:
            requested_base = _candidate_commit(repo, args.base_commit)
            if requested_base != base:
                raise CoordinatorError("risk assessment base must match the batch-captured base commit", remedy="the risk assessment base does not match the batch-captured base commit -- " + INTERNAL_INVARIANT_REMEDY)
        if base and not _git_is_ancestor(repo, base, candidate):
            raise CoordinatorError("risk assessment base must be an ancestor of the candidate commit", remedy="pass a risk assessment base that is an ancestor of candidate_commit")
        actual_files = _changed_files_between(repo, base, candidate) if base else _commit_changed_files(repo, candidate)
        if actual_files != changed_files:
            raise CoordinatorError("changed_files must exactly match the candidate diff", remedy="regenerate changed_files from the actual diff for candidate_commit")
        if _latest_developer_candidate(repo, root, batch) != candidate:
            raise CoordinatorError("candidate commit does not match the accepted developer report", remedy="pass the candidate_commit from the accepted developer report")
        inherited_triggers = {
            trigger
            for escalation in batch.get("risk_escalations", [])
            if escalation.get("candidate_commit") == candidate
            for trigger in escalation.get("triggers", [])
        }
        for trigger in sorted(inherited_triggers):
            if trigger not in developer_triggers:
                developer_triggers.append(trigger)
        evidence = " ".join(batch["definition_of_done"] + changed_files) + "\n" + _commit_evidence(repo, base, candidate)
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
            "review_required": bool(matched),
            "review_scope": list(changed_files),
            "created_at": utils._now(),
        }
        _reject_sensitive(risk, "risk assessment")
        _safe_id(risk["risk_assessment_id"], "risk assessment")
        _write_record(ledger, RiskAssessmentRecord.from_dict(risk))
        batch.setdefault("risk_assessments", []).append(
            {
                "risk_assessment_id": risk["risk_assessment_id"],
                "candidate_commit": candidate,
                "matched_triggers": matched,
                "review_required": risk["review_required"],
                "record_sha256": hashlib.sha256(_canonical(risk).encode("utf-8")).hexdigest(),
            }
        )
        pending_candidate = batch.get("risk_reassessment_candidate")
        pending_triggers = set(batch.get("risk_reassessment_triggers", []))
        if (
            batch.get("risk_reassessment_required")
            and pending_candidate == candidate
            and pending_triggers <= set(matched)
        ):
            batch["risk_reassessment_required"] = False
            batch.pop("risk_reassessment_candidate", None)
            batch.pop("risk_reassessment_triggers", None)
        # Assessment is evidence, not a launch instruction.  It makes the one allowed next
        # handoff visible to the coordinator; a later, separately approved dispatch creates the
        # immutable brief.
        batch["next_action"] = "code-review" if risk["review_required"] else "qa"
        _safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, BatchRecord.from_dict(batch))
    return risk
