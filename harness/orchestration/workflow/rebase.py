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

from pathlib import Path

from harness.orchestration.core.git_utils import _fetch_ref_tip, _git_is_ancestor
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.core.workspace import _integration_ref


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
