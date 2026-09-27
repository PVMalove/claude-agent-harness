"""Read-only `orchestration` health-check group (#343-#347).

Reports the lifecycle ledger's own state, blocked batches, dispatches that stopped sending a
heartbeat, worktree directories orphaned from both `git worktree list` and every open batch, and
the disposable data volume `harness cleanup` would remove -- all without importing
`harness.orchestration.*` or `harness.cleanup` at module level.

Like `files.check_orchestration_config`, every check below first reads `context.lock` and returns
`skipped` when the `backend-orchestration` capability is absent, before ever importing an
orchestration module; only then would the import happen, local to the function body. This keeps a
shipped, standalone `harness/health/` (see tests/test_health_standalone_package.py, which copies
only `health` and `repo_map`) working with no `harness/orchestration/` and no `harness/cleanup.py`
on disk -- the capability manifest installs both together with `backend-orchestration`, so a
deferred import only ever fires once they are actually present.

This first commit only scaffolds the group and its capability gate; each check's real read-only
logic (and its own local import) lands in a later commit of the same series.
"""

from __future__ import annotations

from ..context import HealthContext
from ..model import CheckResult

BACKEND_ORCHESTRATION_CAPABILITY = "backend-orchestration"
_NO_ORCHESTRATION_CAPABILITY_MESSAGE = "backend-orchestration capability не выбрана"


def _skipped(check_id: str) -> CheckResult:
    return CheckResult(
        id=check_id,
        group="orchestration",
        status="skipped",
        message=_NO_ORCHESTRATION_CAPABILITY_MESSAGE,
    )


def check_ledger_summary(context: HealthContext) -> CheckResult:
    return _skipped("orchestration.ledger_summary")


def check_blocked_batches(context: HealthContext) -> CheckResult:
    return _skipped("orchestration.blocked_batches")


def check_stale_dispatches(context: HealthContext) -> CheckResult:
    return _skipped("orchestration.stale_dispatches")


def check_orphaned_worktrees(context: HealthContext) -> CheckResult:
    return _skipped("orchestration.orphaned_worktrees")


def check_disposable_data(context: HealthContext) -> CheckResult:
    return _skipped("orchestration.disposable_data")
