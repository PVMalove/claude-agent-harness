"""The ``rebase-fix-forward`` route (issue #504): a developer-retry decided while the integration
base moved ahead rebases the candidate onto a human-approved target and fixes on top of it in the
same dispatch.

The retry decision proposes the target: the fetched ``origin/<integration_ref>`` tip, recorded in
the routing record. It reaches the developer-retry brief only through a dispatch a human approved
explicitly, bound into the transition as ``rebase_target_sha``. No new transition type exists: the
next action stays ``developer-retry``.

This module holds the Git side of the route; the pure ``commit_map`` rules live in
``commit_plan``. The decision, dispatch and report modules call into it, never the other way round.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

from harness.orchestration.core.git_utils import (
    _candidate_commit,
    _commits_between,
    _fetch_ref_tip,
    _git_is_ancestor,
    _merge_base,
    _patch_id,
)
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.core.workspace import _integration_ref
from harness.orchestration.ledger.ledger_ops import _load_dispatch
from harness.orchestration.workflow import carried_items
from harness.orchestration.workflow import commit_plan as plan_rules
from harness.orchestration.workflow.history import _pending_report


def propose_target(repo: Path, batch: JsonObject, snapshot: str) -> str | None:
    """The integration tip a developer-retry continuing ``snapshot`` must rebase onto, or ``None``.

    A target is proposed when ``origin/<integration_ref>`` moved ahead of the pinned integration
    base (the base is a strict ancestor of the tip) and ``snapshot`` does not contain that tip yet.
    While a stale-base block is open, the tip must also contain that block's target. A batch
    without a pinned integration base (a legacy batch) gets no proposal and no fetch.
    """
    base = batch.get("integration_base_commit")
    if not isinstance(base, str) or not base:
        return None
    ref = _integration_ref(repo, batch)
    try:
        tip = _fetch_ref_tip(repo, ref)
    except CoordinatorError as exc:
        raise CoordinatorError(
            f"a retry that routes to a developer-retry must know whether origin/{ref} moved "
            f"ahead of the integration base {base}: {exc.message}",
            remedy=f"restore access to origin/{ref}, then decide the retry again",
        ) from exc
    if (
        tip == base
        or not _git_is_ancestor(repo, base, tip)
        or _git_is_ancestor(repo, tip, snapshot)
    ):
        return None
    stale = batch.get("rebase_target_commit")
    if (
        batch.get("base_rebase_required")
        and isinstance(stale, str)
        and not _git_is_ancestor(repo, stale, tip)
    ):
        return None
    return tip


def approved_target(
    repo: Path, root: Path, batch: JsonObject, commit: str
) -> str | None:
    """The newest rebase target a developer brief of the batch carried that ``commit`` contains and
    that is strictly newer than the pinned integration base, or ``None``.

    Before the rebase-fix-forward report is accepted, a later retry of it continues a candidate
    already on that target; after the accept pins the target, no target is newer than the base.
    """
    base = batch.get("integration_base_commit")
    if not isinstance(base, str) or not base:
        return None
    for item in reversed(batch.get("dispatches", [])):
        if item.get("role") != "developer":
            continue
        target = plan_rules.rebase_target(_load_dispatch(root, item["dispatch_id"]))
        if (
            target is not None
            and target != base
            and _git_is_ancestor(repo, base, target)
            and _git_is_ancestor(repo, target, commit)
        ):
            return target
    return None


def report_base(
    repo: Path, root: Path, batch: JsonObject, dispatch: JsonObject
) -> str | None:
    """The commit a report's ``changed_files`` are measured from: the pinned integration base,
    or, for a developer brief whose snapshot already sits on a not yet accepted rebase target (a
    retry or tooling-retry of a rebase-fix-forward report), that target, so upstream files the
    rebase brought in are never attributed to the ticket."""
    base = batch.get("integration_base_commit") or batch.get("base_commit")
    snapshot = dispatch.get("snapshot_commit")
    if dispatch.get("role") == "developer" and isinstance(snapshot, str):
        return approved_target(repo, root, batch, snapshot) or base
    return base if isinstance(base, str) else None


def pre_chain_copies(
    repo: Path, root: Path, batch: JsonObject, dispatch: JsonObject, report: JsonObject
) -> set[str]:
    """The rebased copies, in the retry chain of ``dispatch`` (``carried_items.closure_attempts``),
    of commits that existed before the chain (issue #627): a closure never names them, as it does
    not name their originals.

    A copy is traced through the ``rebased_from`` of every rebase-fix-forward ``commit_map`` of the
    chain (``report`` is the one of ``dispatch`` itself) back to its original; the copy is a
    pre-chain copy when that original is not above the chain's first ``snapshot_commit``. A
    stale-base rebase maps no ``rebased_from``, so it has no pre-chain copies: that route counts
    closure commits from the batch target, as for #503. A recorded report of an earlier attempt
    must pass its integrity check.
    """
    attempts = carried_items.closure_attempts(root, batch, dispatch)
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


def previous_commits(repo: Path, dispatch: JsonObject) -> list[str]:
    """The previous-candidate commits a rebase-fix-forward brief rebases: those after the old base
    (``git merge-base`` of ``snapshot_commit`` and the target) up to ``snapshot_commit``."""
    target = plan_rules.rebase_target(dispatch)
    snapshot = dispatch.get("snapshot_commit")
    if target is None or not isinstance(snapshot, str):
        return []
    return _commits_between(repo, _merge_base(repo, snapshot, target), snapshot)


def rebase_check(
    repo: Path, report: JsonObject, dispatch: JsonObject
) -> JsonObject | None:
    """Compare every rebased copy with its original by ``git patch-id --stable``; ``None`` for a
    report whose brief carries no rebase target.

    A mismatch (a conflict resolved with changes) neither refuses the report nor changes whether
    it is clean: it is evidence for the decision and a later delta-review. A pair without a
    patch-id on either side counts as a mismatch."""
    target = plan_rules.rebase_target(dispatch)
    snapshot = dispatch.get("snapshot_commit")
    if (
        target is None
        or not isinstance(snapshot, str)
        or dispatch.get("role") != "developer"
    ):
        return None
    rebased, dropped = plan_rules.rebased_commits(
        report, partial(_candidate_commit, repo)
    )
    pairs: list[JsonObject] = []
    for original, copy in rebased:
        original_id = _patch_id(repo, original)
        pairs.append(
            {
                "rebased_from": original,
                "commit_sha": copy,
                "patch_id_match": original_id is not None
                and original_id == _patch_id(repo, copy),
            }
        )
    return {
        "rebase_target_commit": target,
        "previous_base_commit": _merge_base(repo, snapshot, target),
        "rebased": pairs,
        "dropped": [
            {"rebased_from": original, "dropped": reason}
            for original, reason in dropped
        ],
        "patch_id_mismatches": [
            {"rebased_from": pair["rebased_from"], "commit_sha": pair["commit_sha"]}
            for pair in pairs
            if not pair["patch_id_match"]
        ],
    }
