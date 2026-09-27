"""Group 'orchestration' health checks (#347): read-only reporting on the lifecycle ledger,
worktree directories, and disposable `.sandboxes` data, gated on the `backend-orchestration`
capability exactly like `files.check_orchestration_config` already is."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from harness.health.checks import orchestration as checks
from harness.health.context import HealthContext
from harness.health.model import CheckResult

_NO_CAPABILITY_LOCK: dict[str, object] = {"capabilities": []}
_ORCHESTRATION_LOCK: dict[str, object] = {"capabilities": ["backend-orchestration"]}

_CHECK_FUNCTIONS: list[Callable[[HealthContext], CheckResult]] = [
    checks.check_ledger_summary,
    checks.check_blocked_batches,
    checks.check_stale_dispatches,
    checks.check_orphaned_worktrees,
    checks.check_disposable_data,
]


def _context(repo: Path, lock: dict[str, object] | None = None) -> HealthContext:
    return HealthContext(repo=repo, lock=lock, online=False)


# --- capability gating (DoD 1) ----------------------------------------------------------------


@pytest.mark.parametrize("check_fn", _CHECK_FUNCTIONS)
def test_skips_without_backend_orchestration_capability(
    tmp_path: Path, check_fn: Callable[[HealthContext], CheckResult]
) -> None:
    result = check_fn(_context(tmp_path, lock=_NO_CAPABILITY_LOCK))

    assert result.status == "skipped"
    assert result.group == "orchestration"


@pytest.mark.parametrize("check_fn", _CHECK_FUNCTIONS)
def test_skips_without_a_lock_at_all(
    tmp_path: Path, check_fn: Callable[[HealthContext], CheckResult]
) -> None:
    result = check_fn(_context(tmp_path, lock=None))

    assert result.status == "skipped"
