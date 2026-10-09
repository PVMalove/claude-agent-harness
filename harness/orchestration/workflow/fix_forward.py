"""Read-only Git history of Fix-forward candidates.

Report validation and delta-review share retry-chain and rebased-origin facts here. Their
admission rules stay distinct; this module never approves a dispatch or writes lifecycle state.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from pathlib import Path

from harness.orchestration.core.git_utils import (
    _candidate_commit,
    _changed_files_between,
    _commit_changed_files,
    _commit_parent,
    _commits_between,
    _git_is_ancestor,
)
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.ledger.ledger_ops import _load_dispatch
from harness.orchestration.workflow import carried_items, rebase
from harness.orchestration.workflow import commit_plan as plan_rules
from harness.orchestration.workflow.history import (
    _pending_report,
    _initial_developer_work,
)

FIX_FORWARD_ROUTES = ("fix-forward", "rebase-fix-forward")
PRIOR_REVIEW_RETRY_ROUTES = ("developer-retry", "fix-forward", "rebase-fix-forward")


def _retried_attempt(
    root: Path, entries: list[JsonObject], brief: JsonObject
) -> JsonObject | None:
    """The developer work brief whose retry created ``brief`` with the same carried items, or
    ``None``. The retried entry is the last decided one before the brief's own entry."""
    position = next(
        (
            index
            for index, item in enumerate(entries)
            if item.get("dispatch_id") == brief.get("dispatch_id")
        ),
        0,
    )
    retried = next(
        (
            item
            for item in reversed(entries[:position])
            if isinstance(item.get("decision"), dict)
        ),
        None,
    )
    if (
        retried is None
        or retried.get("role") != "developer"
        or retried["decision"].get("decision") != "retry"
    ):
        return None
    previous = _load_dispatch(root, retried["dispatch_id"])
    if previous.get("purpose", "work") != "work" or previous.get(
        "carried_items"
    ) != brief.get("carried_items"):
        return None
    return previous


def _closure_attempts(
    root: Path, batch: JsonObject, dispatch: JsonObject
) -> list[JsonObject]:
    """The developer briefs of the retry chain whose commits a carried_item_closure may name (#503),
    oldest first and ending with ``dispatch``; ``[]`` when no closure is owed.

    A retried developer report, or a developer tooling-retry, hands the same closed list to the
    next attempt, whose ``snapshot_commit`` is the retried attempt's HEAD. An item an earlier
    attempt closed stays closed by that attempt's commit, so the chain reaches back through every
    retried developer work brief that carried the same list.
    """
    if not carried_items.owes_closure(dispatch):
        return []
    entries = batch.get("dispatches", [])
    chain: list[JsonObject] = []
    brief: JsonObject | None = dispatch
    while brief is not None and isinstance(brief.get("snapshot_commit"), str):
        chain.insert(0, brief)
        brief = _retried_attempt(root, entries, brief)
    return chain


def _closure_snapshots(
    root: Path, batch: JsonObject, dispatch: JsonObject
) -> list[str]:
    """The ``snapshot_commit`` of every attempt of ``closure_attempts``, oldest first."""
    return [
        brief["snapshot_commit"] for brief in _closure_attempts(root, batch, dispatch)
    ]


def _closure_base(
    repo: Path, root: Path, batch: JsonObject, dispatch: JsonObject
) -> str | None:
    """The commit a developer-retry's carried_item_closure counts commits from (issue #503): the
    oldest snapshot of its retry chain that its own ``snapshot_commit`` still descends from, so a
    rebase inside the chain never lets a closure name upstream commits; ``None`` when none is owed.

    When a rebase-fix-forward attempt of the chain rebased it onto a not yet accepted target (a
    chain snapshot does not contain ``rebase.approved_target``), the chain's commits are those
    after that target, as for ``changed_files`` (``rebase.report_base``) (issue #504).
    """
    chain = _closure_snapshots(root, batch, dispatch)
    if not chain:
        return None
    target = rebase.approved_target(repo, root, batch, chain[-1])
    for base in chain[:-1]:
        if target is not None and not _git_is_ancestor(repo, target, base):
            return target
        if _git_is_ancestor(repo, base, chain[-1]):
            return base
    return chain[-1]


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


def _resolve_report_commit(repo: Path, commit_sha: str) -> str:
    try:
        return _candidate_commit(repo, commit_sha)
    except CoordinatorError as exc:
        raise CoordinatorError(
            f"completion report commit_sha {commit_sha} does not resolve to a commit",
            remedy="report a commit_sha that resolves to a real commit in this repository",
        ) from exc


def _pre_chain_copies(
    repo: Path, root: Path, batch: JsonObject, dispatch: JsonObject, report: JsonObject
) -> set[str]:
    """The rebased copies, in the retry chain of ``dispatch`` (``_closure_attempts``),
    of commits that existed before the chain (issue #627): a closure never names them, as it does
    not name their originals.

    A copy is traced through the ``rebased_from`` of every rebase-fix-forward ``commit_map`` of the
    chain (``report`` is the one of ``dispatch`` itself) back to its original; the copy is a
    pre-chain copy when that original is not above the chain's first ``snapshot_commit``. A
    stale-base rebase maps no ``rebased_from``, so it has no pre-chain copies: that route counts
    closure commits from the batch target, as for #503. A recorded report of an earlier attempt
    must pass its integrity check.
    """
    attempts = _closure_attempts(root, batch, dispatch)
    resolve = partial(_candidate_commit, repo)
    entries = {item.get("dispatch_id"): item for item in batch.get("dispatches", [])}
    original_of: dict[str, str] = {}
    for brief in attempts:
        if plan_rules.rebase_target(brief) is None:
            continue
        source = (
            report
            if brief is dispatch
            else _pending_report(root, batch, entries.get(brief["dispatch_id"], {}))
        )
        for original, copy in plan_rules.rebased_commits(source, resolve)[0]:
            original_of[copy] = original
    first = attempts[0]["snapshot_commit"] if attempts else ""

    def origin(copy: str) -> str:
        # Bounded: a malformed commit_map of a brief without commit_plan is not validated yet.
        for _ in original_of:
            if copy not in original_of:
                break
            copy = original_of[copy]
        return copy

    return {copy for copy in original_of if _git_is_ancestor(repo, origin(copy), first)}


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


@dataclass(frozen=True)
class ReviewDelta:
    """History evidence plus the briefs whose risk and scope policy the caller checks."""

    scope: JsonObject
    review_brief: JsonObject
    developer_brief: JsonObject
    escalations: dict[str, list[object]]


class FixForwardHistory:
    """Hide report bases, retry chains and rebased origins behind report and review operations."""

    def __init__(
        self, repo: Path, root: Path | None = None, batch: JsonObject | None = None
    ) -> None:
        self.repo = repo
        self.root = root
        self.batch = batch

    def validate_report(
        self,
        report: JsonObject,
        dispatch: JsonObject,
        role: JsonObject,
        base_commit: str | None = None,
    ) -> None:
        """Check the write report's files, commit plan and closure against its history."""
        repo, root = self.repo, self.root
        batch = self.batch or {}
        if root is not None and self.batch is not None:
            base_commit = rebase.report_base(repo, root, batch, dispatch)
        rebase_target = rebase.required_target(batch, dispatch)
        closure_base = (
            _closure_base(repo, root, batch, dispatch) if root is not None else None
        )
        changed_files = report["changed_files"]
        commit_sha = report["commit_sha"]
        if changed_files:
            resolved = _resolve_report_commit(repo, commit_sha)
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
        if role.get("name") == "developer" and (planned or closure):
            if not changed_files:
                commit_map = report.get("commit_map")
                if isinstance(commit_map, list) and commit_map:
                    raise CoordinatorError(
                        "early blocked developer report cannot claim commit_map entries without changed_files",
                        remedy="report an empty commit_map when stopped before creating commits",
                    )
            else:
                snapshot = dispatch.get("snapshot_commit")
                if not isinstance(snapshot, str):
                    raise CoordinatorError(
                        "developer dispatch lacks snapshot_commit",
                        remedy="create a new developer dispatch with an immutable snapshot",
                    )
                plan_base = (
                    base_commit or snapshot
                    if _initial_developer_work(dispatch)
                    else snapshot
                )
                if planned:
                    plan_rules.check_report(
                        report,
                        dispatch,
                        _commits_between(repo, rebase_target or plan_base, resolved),
                        partial(_candidate_commit, repo),
                        previous=rebase.previous_commits(repo, dispatch),
                    )
                if closure:
                    # An earlier attempt of the retry chain may have closed an item (issue #503).
                    pre_chain = (
                        _pre_chain_copies(repo, root, batch, dispatch, report)
                        if root is not None
                        else set()
                    )
                    carried_items.check_closure_commits(
                        report,
                        [
                            sha
                            for sha in _commits_between(
                                repo,
                                rebase_target or closure_base or snapshot,
                                resolved,
                            )
                            if sha not in pre_chain
                        ],
                        partial(_candidate_commit, repo),
                    )

    def review_delta(self, candidate: str) -> ReviewDelta | None:
        """Recover the prior review and the candidate's new commits without choosing risk policy."""
        if self.root is None or self.batch is None:
            raise ValueError("review history requires a ledger root and batch")
        repo, root, batch = self.repo, self.root, self.batch
        prior = _fix_forward_chain(repo, root, batch, candidate)
        if prior is None:
            return None
        chain, review, review_brief = prior
        accepted, brief = chain[-1]
        report = _pending_report(root, batch, accepted)
        targets = [
            target
            for _, attempt in chain
            if (target := plan_rules.rebase_target(attempt))
        ]
        origin = targets[-1] if targets else review_brief["candidate_commit"]
        base, copies, recorded = (
            _rebased_delta(repo, root, batch, chain, review_brief, (origin, candidate))
            if targets
            else (origin, [], {})
        )
        delta_commits = _commits_between(repo, base, candidate) if base else []
        if not delta_commits:
            recorded = {"no-new-commits": [f"{origin}..{candidate}"], **recorded}
        return ReviewDelta(
            {
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
            },
            review_brief,
            brief,
            recorded,
        )
