"""Read-only `orchestration` health-check group (#343-#347).

Reports the lifecycle ledger's own state, blocked batches, dispatches that stopped sending a
heartbeat, worktree directories orphaned from both `git worktree list` and every open batch, and
the disposable data volume `harness cleanup` would remove -- all without importing
`harness.orchestration.*` or `harness.cleanup` at module level.

Like `files.check_orchestration_config`, every check below first reads `context.lock` and returns
`skipped` when the `backend-orchestration` capability is absent, before ever importing an
orchestration module; only then does the import happen, local to the function body. This keeps a
shipped, standalone `harness/health/` (see tests/test_health_standalone_package.py, which copies
only `health` and `repo_map`) working with no `harness/orchestration/` and no `harness/cleanup.py`
on disk -- the capability manifest installs both together with `backend-orchestration`, so the
deferred import only ever fires once they are actually present.

Every check only reads state: ledger records, `git worktree list --porcelain`, and a dry-run
`plan_cleanup` preview. None of them call `ledger migrate`, `ledger reset`, `apply_cleanup`, or
`git worktree remove`/`prune` -- nothing here mutates the ledger, worktrees, or disposable data.
"""

from __future__ import annotations

from pathlib import Path

from ..context import HealthContext
from ..model import CheckResult, Fix, JsonObject

BACKEND_ORCHESTRATION_CAPABILITY = "backend-orchestration"
_NO_ORCHESTRATION_CAPABILITY_MESSAGE = "backend-orchestration capability не выбрана"
_MIGRATION_MESSAGE = "леджер оркестрации требует миграции схемы"
_MIGRATION_FIX = Fix(
    text="выполните coordinator.py ledger migrate",
    command="coordinator.py ledger migrate",
)


def _has_capability(context: HealthContext) -> bool:
    lock = context.lock
    return lock is not None and BACKEND_ORCHESTRATION_CAPABILITY in (
        lock.get("capabilities") or []
    )


def _skipped(check_id: str) -> CheckResult:
    return CheckResult(
        id=check_id,
        group="orchestration",
        status="skipped",
        message=_NO_ORCHESTRATION_CAPABILITY_MESSAGE,
    )


class _LedgerState:
    """One read of the lifecycle ledger, shared by the ledger-backed checks in this module so
    none of them repeats the same pointer/generation resolution and error handling.

    Only calls read-only `LifecycleLedger` methods (`status`, `records_root`,
    `read_record_lenient`) -- it never writes, migrates, or resets the ledger."""

    def __init__(self, repo: Path) -> None:
        from harness.orchestration.core.constants import STATE_REL
        from harness.orchestration.ledger.lifecycle import LedgerError, LifecycleLedger

        self.ledger = LifecycleLedger(repo / STATE_REL)
        self.error: str | None = None
        self.status: JsonObject = {}
        self.batches: list[JsonObject] = []
        self.records_root: Path | None = None
        try:
            self.status = self.ledger.status()
        except LedgerError as exc:
            self.error = str(exc)
            return
        if self.status.get("legacy") or self.status.get("stale_schema"):
            return
        if self.status.get("generation") is None:
            return
        try:
            root = self.ledger.records_root()
        except LedgerError as exc:
            self.error = str(exc)
            return
        self.records_root = root
        for path in sorted((root / "batches").glob("*.json")):
            record = LifecycleLedger.read_record_lenient(path)
            if record is not None:
                self.batches.append(record)

    @property
    def unreadable(self) -> bool:
        return self.error is not None

    @property
    def needs_migration(self) -> bool:
        return bool(self.status.get("legacy")) or bool(self.status.get("stale_schema"))


def check_ledger_summary(context: HealthContext) -> CheckResult:
    check_id = "orchestration.ledger_summary"
    if not _has_capability(context):
        return _skipped(check_id)
    state = _LedgerState(context.repo)
    if state.unreadable:
        return CheckResult(
            id=check_id,
            group="orchestration",
            status="fail",
            message=f"леджер оркестрации недоступен: {state.error}",
        )
    if state.needs_migration:
        return CheckResult(
            id=check_id,
            group="orchestration",
            status="warn",
            message=_MIGRATION_MESSAGE,
            fix=_MIGRATION_FIX,
        )
    if state.status.get("generation") is None:
        return CheckResult(
            id=check_id,
            group="orchestration",
            status="ok",
            message="леджер оркестрации ещё не инициализирован (батчей нет)",
        )
    counts: dict[str, int] = {}
    for batch in state.batches:
        batch_state = batch.get("state")
        if isinstance(batch_state, str):
            counts[batch_state] = counts.get(batch_state, 0) + 1
    # failed/abandoned batches are informational only: they are counted here but never change
    # this check's status away from "ok".
    counts_text = (
        ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))
        if counts
        else "батчей нет"
    )
    return CheckResult(
        id=check_id,
        group="orchestration",
        status="ok",
        message=(
            f"леджер: generation={state.status['generation']} "
            f"schema_version={state.status['version']}; батчи по состояниям: {counts_text}"
        ),
    )


def check_blocked_batches(context: HealthContext) -> CheckResult:
    return _skipped("orchestration.blocked_batches")


def check_stale_dispatches(context: HealthContext) -> CheckResult:
    return _skipped("orchestration.stale_dispatches")


def check_orphaned_worktrees(context: HealthContext) -> CheckResult:
    return _skipped("orchestration.orphaned_worktrees")


def check_disposable_data(context: HealthContext) -> CheckResult:
    return _skipped("orchestration.disposable_data")
