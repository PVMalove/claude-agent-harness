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
from harness.orchestration.workflow import commit_plan as plan_rules


def required_target(batch: JsonObject, dispatch: JsonObject) -> str | None:
    """The integration tip a developer dispatch must rebase onto: the human-approved target its
    rebase-fix-forward brief carries (issue #504), else the batch target while a stale-base block
    is open."""
    approved = plan_rules.rebase_target(dispatch)
    if approved is not None:
        return approved
    target = batch.get("rebase_target_commit")
    if (
        dispatch.get("role") == "developer"
        and batch.get("base_rebase_required")
        and isinstance(target, str)
    ):
        return target
    return None


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
