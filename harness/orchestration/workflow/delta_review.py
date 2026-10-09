"""Delta-review after a fix-forward (issue #625).

After a fix-forward or rebase-fix-forward developer-retry on a candidate a code-review already
judged, the coordinator scopes the next code-review itself: to the commits the retry added and the
closure of the items it carried, with the prior review's report as evidence for the rest of the
candidate. A rebased copy whose ``git patch-id`` matches its original counts as already reviewed. It
escalates to a full review on its own when the new commits match a risk trigger the prior review
did not see, change a file outside the carried items or add nothing to review, or when a rebased
copy's patch-id differs from its original or the rebase dropped a previous-candidate commit.

The choice is made while ``dispatch create --role code-review`` builds the brief, never by a new
decision or transition type: the brief records it as ``delta_review_scope``, bound into the
transition as ``delta_review_sha256``. An explicit ``--delta-review-of`` keeps the test-only
delta-review and computes nothing here. Only the dispatch module calls into this one.
"""

from __future__ import annotations

from pathlib import Path

from harness.orchestration.core.constants import (
    CARRIED_ITEM_SOURCES,
    DELTA_REVIEW_ESCALATIONS,
)
from harness.orchestration.core.git_utils import (
    _changed_files_between,
    _commit_evidence,
)
from harness.orchestration.core.utils import JsonObject
from harness.orchestration.ledger.ledger_ops import _load_dispatch, _load_risk
from harness.orchestration.workflow import carried_items
from harness.orchestration.workflow.fix_forward import FixForwardHistory
from harness.orchestration.workflow.history import _validate_risk
from harness.orchestration.workflow.risk import _matching_triggers

# The carried items a delta-review accounts for on top of its own brief's: the review findings and
# the developer's incomplete items the accepted developer-retry carried, closed or not. A retry
# accepted through override-warning counts as accepted.
CLOSURE_KINDS = (carried_items.REVIEW_FINDING, carried_items.INCOMPLETE_ITEM)


def _carried_files(root: Path, brief: JsonObject) -> set[str]:
    """The files of the items a developer-retry brief carried.

    A coordinator finding or an incomplete item names its own ``files``. A review finding names
    none (the review report's finding has no files): it concerns the diff its review judged, so its
    files are that review's ``review_scope``.
    """
    files: set[str] = set()
    for items in (brief.get("carried_items") or {}).values():
        for item in items:
            source = item["source"]
            if source["kind"] == carried_items.REVIEW_FINDING:
                review = _load_dispatch(root, source["dispatch_id"])
                files.update(review.get("review_scope") or [])
            else:
                files.update(item["files"])
    return files


def _escalations(
    repo: Path,
    root: Path,
    batch: JsonObject,
    briefs: tuple[JsonObject, JsonObject],
    risk: JsonObject,
    known: list[str],
    delta: tuple[str, str],
) -> dict[str, list[object]]:
    """The escalations of a non-empty delta ``(base, candidate)``, by reason.

    ``briefs`` are the prior review's brief and the accepted developer-retry's brief. A
    ``new-risk-trigger`` is a trigger the prior review did not see: one of the candidate's own
    assessment ``risk`` (developer triggers included) or one the delta's commits and files match
    among ``known``, less the triggers of the assessment the prior review was dispatched under. A
    trigger the prior review already saw does not escalate on its own: a delta-review still judges
    the new commits on both axes. A ``file-outside-carried-items`` is a file the delta changes that
    no item of the developer-retry brief carried.
    """
    review_brief, brief = briefs
    base, candidate = delta
    files = _changed_files_between(repo, base, candidate)
    prior = _load_risk(root, review_brief["risk_assessment_id"])
    _validate_risk(root, batch, prior)
    matched = _matching_triggers(
        _commit_evidence(repo, base, candidate) + "\n" + " ".join(files), known
    )
    seen = set(prior["matched_triggers"])
    found: dict[str, list[object]] = {}
    triggers = [
        trigger
        for trigger in dict.fromkeys([*risk["matched_triggers"], *matched])
        if trigger not in seen
    ]
    if triggers:
        found["new-risk-trigger"] = list(triggers)
    outside = sorted(set(files) - _carried_files(root, brief))
    if outside:
        found["file-outside-carried-items"] = list(outside)
    return found


def _escalation_list(found: dict[str, list[object]]) -> list[JsonObject]:
    """The escalations found, in the order of the closed set of reasons."""
    return [
        {"reason": reason, "evidence": found[reason]}
        for reason in DELTA_REVIEW_ESCALATIONS
        if reason in found
    ]


def scope_section(
    repo: Path,
    root: Path,
    batch: JsonObject,
    candidate: str,
    risk: JsonObject,
    known: list[str],
) -> JsonObject | None:
    """The ``delta_review_scope`` of a code-review brief for ``candidate``, or ``None``.

    ``None`` keeps today's full review: the candidate did not come from a fix-forward developer-retry
    on an already reviewed candidate. Otherwise the section names the prior review, the accepted
    developer-retry, the commits it added on top of the reviewed candidate (after a rebase, the
    commits after the last rebase target less the leading reviewed copies) and its
    ``carried_item_closure``. ``mode`` is ``delta`` unless an escalation was found, which makes it
    ``full``: the brief is then an ordinary full review and the section is audit evidence only.
    ``risk`` is the candidate's assessment and ``known`` the code-review role's risk triggers.
    """
    delta = FixForwardHistory(repo, root, batch).review_delta(candidate)
    if delta is None:
        return None
    scope = delta.scope
    base = scope["delta_base"]
    found = (
        _escalations(
            repo,
            root,
            batch,
            (delta.review_brief, delta.developer_brief),
            risk,
            known,
            (base, candidate),
        )
        if base and scope["delta_commits"]
        else {}
    )
    found.update(delta.escalations)
    return {
        **scope,
        "mode": "full" if found else "delta",
        "escalations": _escalation_list(found),
    }


def with_closure_items(
    section: JsonObject, root: Path, scope: JsonObject | None
) -> JsonObject:
    """The carried items of a code-review brief under ``scope``.

    A delta-review also carries the review findings and the developer's incomplete items the
    accepted developer-retry carried, each once, so the review accounts for their closure in
    ``review.carried_items``; any other brief carries ``section`` unchanged.
    """
    if scope is None or scope["mode"] != "delta":
        return section
    retried = (
        _load_dispatch(root, scope["developer_dispatch_id"]).get("carried_items") or {}
    )
    present = set(carried_items.section_item_ids(section))
    merged = {kind: list(items) for kind, items in section.items()}
    for kind in CLOSURE_KINDS:
        for item in retried.get(kind, []):
            if item["item_id"] in present or (
                kind == carried_items.INCOMPLETE_ITEM
                and item["source"].get("target_role") != "developer"
            ):
                continue
            merged.setdefault(kind, []).append(item)
    return {kind: merged[kind] for kind in CARRIED_ITEM_SOURCES if merged.get(kind)}
