"""Access of the operations the coordinator executes itself: QA, Git and publish.

A worker's plan is proven by the native runtime that launches it. These operations run in the
coordinator process, so the plan is selected here and checked against that process before the
operation changes anything.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .core.constants import ACCESS_OPERATIONS
from .core.utils import JsonObject
from .runtime_access import AccessError, resolve_plan, validate_binding


def select_plan(
    repo: Path,
    config: Mapping[str, object],
    operation: str,
    *,
    brief: Mapping[str, object] | None = None,
    worktree: Path | None = None,
) -> JsonObject:
    """The plan the operation runs under.

    An operation with a dispatch uses the plan pinned in its approved brief, never the live
    config: later edits cannot widen it. A brief without a plan is historical and keeps
    ``legacy-inherit``. An operation without a dispatch resolves the live config's defaults and its
    own override, without any role override.
    """
    if operation not in ACCESS_OPERATIONS:
        raise AccessError(
            f"unknown coordinator operation {operation!r}",
            remedy=f"select one of: {', '.join(ACCESS_OPERATIONS)}",
        )
    if brief is None:
        return resolve_plan(
            repo,
            worktree or repo,
            config,
            None,
            "write" if operation == "git" else "read-only",
            operation=operation,
        )
    if "runtime_access" not in brief:
        return resolve_plan(repo, repo, {}, None, "read-only")
    validate_binding(brief)
    plan = brief["runtime_access"]
    assert isinstance(plan, dict)
    return plan
