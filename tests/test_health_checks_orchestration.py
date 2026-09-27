"""Group 'orchestration' health checks (#347): read-only reporting on the lifecycle ledger,
worktree directories, and disposable `.sandboxes` data, gated on the `backend-orchestration`
capability exactly like `files.check_orchestration_config` already is."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pytest

from harness.health.checks import orchestration as checks
from harness.health.context import HealthContext
from harness.health.model import CheckResult
from harness.orchestration.core.constants import STATE_REL
from harness.orchestration.ledger.lifecycle import JsonObject, JsonValue, LifecycleLedger

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


def _ledger(repo: Path) -> LifecycleLedger:
    ledger = LifecycleLedger(repo / STATE_REL)
    ledger.ensure()
    return ledger


def _write_batch(
    ledger: LifecycleLedger,
    batch_id: str,
    state: str,
    dispatches: list[JsonObject] | None = None,
) -> None:
    generation = ledger.records_root()
    dispatch_entries: list[JsonValue] = list(dispatches or [])
    record: JsonObject = {
        "batch_id": batch_id,
        "state": state,
        "coordinator_approval": None,
        "dispatches": dispatch_entries,
    }
    ledger.write_immutable(
        generation / "plans" / f"{batch_id}.json", {"batch_id": batch_id}
    )
    ledger.write_immutable(generation / "batches" / f"{batch_id}.json", record)


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


# --- orchestration.ledger_summary (DoD 2, 3) --------------------------------------------------


def test_ledger_summary_ok_with_no_orchestration_data_yet(tmp_path: Path) -> None:
    result = checks.check_ledger_summary(_context(tmp_path, lock=_ORCHESTRATION_LOCK))

    assert result.status == "ok"


def test_ledger_summary_fails_on_an_unreadable_ledger_pointer(tmp_path: Path) -> None:
    pointer = tmp_path / ".harness" / "orchestration" / "state" / "ledger.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text("not json", encoding="utf-8")

    result = checks.check_ledger_summary(_context(tmp_path, lock=_ORCHESTRATION_LOCK))

    assert result.status == "fail"
    assert "недоступен" in result.message


def test_ledger_summary_warns_on_legacy_state_requiring_migration(
    tmp_path: Path,
) -> None:
    legacy_batches = tmp_path / ".harness" / "orchestration" / "state" / "batches"
    legacy_batches.mkdir(parents=True)
    (legacy_batches / "batch-legacy.json").write_text(
        json.dumps(
            {"batch_id": "batch-legacy", "state": "completed", "dispatches": []}
        ),
        encoding="utf-8",
    )

    result = checks.check_ledger_summary(_context(tmp_path, lock=_ORCHESTRATION_LOCK))

    assert result.status == "warn"
    assert result.fix is not None
    assert result.fix.command == "coordinator.py ledger migrate"


def test_ledger_summary_warns_on_a_stale_schema_version(tmp_path: Path) -> None:
    state_root = tmp_path / ".harness" / "orchestration" / "state"
    state_root.mkdir(parents=True)
    (state_root / "ledger.json").write_text(
        json.dumps(
            {
                "version": 1,
                "generation": "generation-old",
                "selected_at": "2020-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    result = checks.check_ledger_summary(_context(tmp_path, lock=_ORCHESTRATION_LOCK))

    assert result.status == "warn"
    assert result.fix is not None
    assert result.fix.command == "coordinator.py ledger migrate"


def test_ledger_summary_reports_generation_schema_and_batch_counts(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _write_batch(ledger, "batch-a", "completed")
    _write_batch(ledger, "batch-b", "abandoned")
    _write_batch(ledger, "batch-c", "active")

    result = checks.check_ledger_summary(_context(tmp_path, lock=_ORCHESTRATION_LOCK))

    # failed/abandoned batches are informational only: the check still reports "ok".
    assert result.status == "ok"
    assert "completed=1" in result.message
    assert "abandoned=1" in result.message
    assert "active=1" in result.message
