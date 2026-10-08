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
    _candidate_commit,
    _changed_files_between,
    _commit_evidence,
    _commit_parent,
    _commits_between,
)
from harness.orchestration.core.utils import JsonObject
from harness.orchestration.ledger.ledger_ops import _load_dispatch, _load_risk
from harness.orchestration.workflow import carried_items, rebase
from harness.orchestration.workflow import commit_plan as plan_rules
from harness.orchestration.workflow.history import _pending_report, _validate_risk
from harness.orchestration.workflow.risk import _matching_triggers

# The routes of the decision that sends a reviewed candidate back to a developer-retry with items.
FIX_FORWARD_ROUTES = ("fix-forward", "rebase-fix-forward")
# A prior review whose report stands as evidence: accepted, or retried to a developer.
PRIOR_REVIEW_RETRY_ROUTES = ("developer-retry", "fix-forward", "rebase-fix-forward")
# The carried items a delta-review accounts for on top of its own brief's: the review findings and
# the developer's incomplete items the accepted developer-retry carried, closed or not. A retry
# accepted through override-warning counts as accepted.
CLOSURE_KINDS = (carried_items.REVIEW_FINDING, carried_items.INCOMPLETE_ITEM)


def _accepted(entry: JsonObject) -> bool:
    return entry["decision"].get("decision") in {"accept", "override-warning"}


def _developer_work(root: Path, entry: JsonObject) -> JsonObject | None:
    """The brief of a developer work entry, or ``None`` for any other entry."""
    if entry.get("role") != "developer":
        return None
    brief = _load_dispatch(root, entry["dispatch_id"])
    return brief if brief.get("purpose", "work") == "work" else None


def _fix_forward_chain(
    repo: Path, root: Path, batch: JsonObject, candidate: str
) -> tuple[list[tuple[JsonObject, JsonObject]], JsonObject, JsonObject] | None:
    """The fix-forward developer-retry chain that produced ``candidate`` and the review before it.

    The chain is the newest accepted developer work entry, a developer-retry whose report's
    ``commit_sha`` is ``candidate``, with every directly preceding retried developer-retry attempt,
    oldest first, as ``(entry, brief)`` pairs. The newest decided entry before the chain must have
    routed it as ``fix-forward`` or ``rebase-fix-forward``, and the newest code-review before the
    chain must have completed its report on the chain's first ``snapshot_commit`` and been accepted
    or retried to a developer. Returns the chain, that review's entry and its brief, or ``None``
    when the candidate did not come from such a chain.
    """
    entries = [
        item
        for item in batch.get("dispatches", [])
        if isinstance(item.get("decision"), dict)
    ]
    found = next(
        (
            (index, brief)
            for index in range(len(entries) - 1, -1, -1)
            if _accepted(entries[index])
            and (brief := _developer_work(root, entries[index])) is not None
        ),
        None,
    )
    if found is None:
        return None
    position, brief = found
    accepted = entries[position]
    if (
        not plan_rules.is_developer_retry(brief)
        or _candidate_commit(repo, _pending_report(root, batch, accepted)["commit_sha"])
        != candidate
    ):
        return None
    chain = [(accepted, brief)]
    start = position
    while start > 0:
        entry = entries[start - 1]
        attempt = _developer_work(root, entry)
        if (
            attempt is None
            or entry["decision"].get("decision") != "retry"
            or not plan_rules.is_developer_retry(attempt)
        ):
            break
        chain.insert(0, (entry, attempt))
        start -= 1
    if start == 0:
        return None
    routing = entries[start - 1]["decision"].get("routing")
    if not isinstance(routing, dict) or routing.get("route") not in FIX_FORWARD_ROUTES:
        return None
    review = next(
        (
            item
            for item in reversed(entries[:start])
            if item.get("role") == "code-review"
        ),
        None,
    )
    if review is None:
        return None
    review_brief = _load_dispatch(root, review["dispatch_id"])
    review_routing = review["decision"].get("routing")
    retried_to_developer = (
        review["decision"].get("decision") == "retry"
        and isinstance(review_routing, dict)
        and review_routing.get("route") in PRIOR_REVIEW_RETRY_ROUTES
    )
    if (
        _pending_report(root, batch, review).get("outcome") != "completed"
        or not (_accepted(review) or retried_to_developer)
        or review_brief.get("candidate_commit") != chain[0][1].get("snapshot_commit")
    ):
        return None
    return chain, review, review_brief


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


def _reviewed_origin(
    commit: str, pairs: dict[str, tuple[str, bool]], reviewed: set[str]
) -> str | None:
    """The commit of the prior review's range ``commit`` is a rebased copy of, or ``None``.

    ``pairs`` maps each rebased copy of the retry chain to its ``rebased_from`` original and whether
    their patch-ids match, so a copy of a copy resolves through every rebase of the chain; one
    mismatched step, a cycle (a pair whose copy is its own original included), or an original the
    prior review never judged makes it a new commit.
    """
    visited: set[str] = set()
    while commit in pairs:
        if commit in visited:
            return None
        visited.add(commit)
        commit, matched = pairs[commit]
        if not matched:
            return None
    return commit if commit in reviewed else None


def _rebased_delta(
    repo: Path,
    root: Path,
    batch: JsonObject,
    chain: list[tuple[JsonObject, JsonObject]],
    review_brief: JsonObject,
    delta: tuple[str, str],
) -> tuple[str | None, list[JsonObject], dict[str, list[object]]]:
    """The delta of a retry chain that rebased onto ``target``: ``delta`` is ``(target,
    candidate)``.

    The commits after the last rebase target are walked in order: each leading rebased copy that
    resolves to the prior review's range with matching patch-ids is already reviewed. The delta
    starts at the parent of the first other commit, so every later copy is reviewed again, which is
    safe. Returns that base (``None`` when every commit is a reviewed copy), the reviewed copies and
    the escalations the chain's reports recorded: every patch-id mismatch and every dropped
    previous-candidate commit. A dropped commit leaves the candidate without a change the prior
    review judged, which no delta of the remaining commits shows.
    """
    target, candidate = delta
    pairs: dict[str, tuple[str, bool]] = {}
    recorded: dict[str, list[object]] = {"patch-id-mismatch": [], "dropped-commit": []}
    for entry, attempt in chain:
        check = rebase.rebase_check(repo, _pending_report(root, batch, entry), attempt)
        if check is None:
            continue
        for pair in check["rebased"]:
            pairs[pair["commit_sha"]] = (pair["rebased_from"], pair["patch_id_match"])
        recorded["patch-id-mismatch"].extend(check["patch_id_mismatches"])
        recorded["dropped-commit"].extend(check["dropped"])
    found = {reason: evidence for reason, evidence in recorded.items() if evidence}
    reviewed = set(
        _commits_between(
            repo, review_brief["review_base"], review_brief["candidate_commit"]
        )
    )
    copies: list[JsonObject] = []
    for commit in _commits_between(repo, target, candidate):
        origin = _reviewed_origin(commit, pairs, reviewed)
        if origin is None:
            return _commit_parent(repo, commit), copies, found
        copies.append({"commit_sha": commit, "rebased_from": origin})
    return None, copies, found


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
    prior = _fix_forward_chain(repo, root, batch, candidate)
    if prior is None:
        return None
    chain, review, review_brief = prior
    accepted, brief = chain[-1]
    report = _pending_report(root, batch, accepted)
    targets = [
        target for _, attempt in chain if (target := plan_rules.rebase_target(attempt))
    ]
    origin = targets[-1] if targets else review_brief["candidate_commit"]
    base, copies, recorded = (
        _rebased_delta(repo, root, batch, chain, review_brief, (origin, candidate))
        if targets
        else (origin, [], {})
    )
    delta_commits = _commits_between(repo, base, candidate) if base else []
    found: dict[str, list[object]] = (
        _escalations(
            repo, root, batch, (review_brief, brief), risk, known, (base, candidate)
        )
        if base and delta_commits
        else {"no-new-commits": [f"{origin}..{candidate}"]}
    )
    found.update(recorded)
    return {
        "mode": "full" if found else "delta",
        "route": "rebase-fix-forward" if targets else "fix-forward",
        "prior_review": {
            "dispatch_id": review["dispatch_id"],
            "report_sha256": review["report_sha256"],
            "candidate_commit": review_brief["candidate_commit"],
            "review_base": review_brief.get("review_base"),
            "risk_assessment_id": review_brief.get("risk_assessment_id"),
        },
        "developer_dispatch_id": accepted["dispatch_id"],
        "delta_base": base if delta_commits else None,
        "delta_commits": delta_commits,
        "reviewed_copies": copies,
        "closure": report.get(carried_items.CLOSURE_FIELD, []),
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
