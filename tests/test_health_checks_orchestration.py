"""Group 'orchestration' health checks (#347): read-only reporting on the lifecycle ledger,
worktree directories, and disposable `.sandboxes` data, gated on the `backend-orchestration`
capability exactly like `files.check_orchestration_config` already is."""

from __future__ import annotations

import json
import os
import sys
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

import pytest

from harness.health.checks import orchestration as checks
from harness.health.context import HealthContext, shell_join
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


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


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


def _write_dispatch_status(
    ledger: LifecycleLedger, dispatch_id: str, status: JsonObject
) -> None:
    generation = ledger.records_root()
    ledger.write_immutable(
        generation / "dispatch-status" / f"{dispatch_id}.json", status
    )


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
    # Runnable as printed from any directory: interpreter and coordinator path are absolute.
    assert result.fix.command == shell_join(
        [
            sys.executable,
            str(tmp_path / ".harness" / "orchestration" / "coordinator.py"),
            "--repo",
            str(tmp_path),
            "ledger",
            "migrate",
        ]
    )


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
    # Runnable as printed from any directory: interpreter and coordinator path are absolute.
    assert result.fix.command == shell_join(
        [
            sys.executable,
            str(tmp_path / ".harness" / "orchestration" / "coordinator.py"),
            "--repo",
            str(tmp_path),
            "ledger",
            "migrate",
        ]
    )


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


# --- orchestration.blocked_batches (DoD 3) ------------------------------------------------------


def test_blocked_batches_warns_and_names_only_the_blocked_batch(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _write_batch(ledger, "batch-blocked", "blocked")
    _write_batch(ledger, "batch-active", "active")

    result = checks.check_blocked_batches(_context(tmp_path, lock=_ORCHESTRATION_LOCK))

    assert result.status == "warn"
    assert "batch-blocked" in result.message
    assert "batch-active" not in result.message


def test_blocked_batches_ok_when_none_are_blocked(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _write_batch(ledger, "batch-active", "active")

    result = checks.check_blocked_batches(_context(tmp_path, lock=_ORCHESTRATION_LOCK))

    assert result.status == "ok"


def test_blocked_batches_fails_on_an_unreadable_ledger(tmp_path: Path) -> None:
    pointer = tmp_path / ".harness" / "orchestration" / "state" / "ledger.json"
    pointer.parent.mkdir(parents=True)
    pointer.write_text("not json", encoding="utf-8")

    result = checks.check_blocked_batches(_context(tmp_path, lock=_ORCHESTRATION_LOCK))

    assert result.status == "fail"


# --- orchestration.stale_dispatches (DoD 3, architect risk #3) ---------------------------------


def test_stale_dispatches_warns_past_the_threshold(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _write_batch(
        ledger,
        "batch-live",
        "active",
        dispatches=[{"dispatch_id": "dispatch-stale", "state": "working"}],
    )
    _write_dispatch_status(
        ledger,
        "dispatch-stale",
        {
            "dispatch_id": "dispatch-stale",
            "state": "working",
            "heartbeat_at": "2020-01-01T00:00:00+00:00",
        },
    )

    result = checks.check_stale_dispatches(_context(tmp_path, lock=_ORCHESTRATION_LOCK))

    assert result.status == "warn"
    assert "dispatch-stale" in result.message


def test_stale_dispatches_ok_when_heartbeat_is_recent(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    recent = datetime.now(UTC).isoformat()
    _write_batch(
        ledger,
        "batch-live",
        "active",
        dispatches=[{"dispatch_id": "dispatch-fresh", "state": "working"}],
    )
    _write_dispatch_status(
        ledger,
        "dispatch-fresh",
        {"dispatch_id": "dispatch-fresh", "state": "working", "heartbeat_at": recent},
    )

    result = checks.check_stale_dispatches(_context(tmp_path, lock=_ORCHESTRATION_LOCK))

    assert result.status == "ok"


def test_stale_dispatches_ignores_dispatches_of_terminal_batches(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _write_batch(
        ledger,
        "batch-done",
        "completed",
        dispatches=[{"dispatch_id": "dispatch-old", "state": "reported"}],
    )
    _write_dispatch_status(
        ledger,
        "dispatch-old",
        {
            "dispatch_id": "dispatch-old",
            # Even a "working" status record on a terminal batch must not be flagged: the batch
            # itself, not the dispatch's own last recorded state, decides whether it is in scope.
            "state": "working",
            "heartbeat_at": "2020-01-01T00:00:00+00:00",
        },
    )

    result = checks.check_stale_dispatches(_context(tmp_path, lock=_ORCHESTRATION_LOCK))

    assert result.status == "ok"


def test_stale_dispatches_uses_the_project_configured_threshold_over_the_default(
    tmp_path: Path,
) -> None:
    """Architect risk #3: the threshold must actually come from
    .harness/orchestration.json when set, not silently fall back to the built-in default."""
    ledger = _ledger(tmp_path)
    ten_minutes_ago = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
    _write_batch(
        ledger,
        "batch-live",
        "active",
        dispatches=[{"dispatch_id": "dispatch-recent", "state": "working"}],
    )
    _write_dispatch_status(
        ledger,
        "dispatch-recent",
        {
            "dispatch_id": "dispatch-recent",
            "state": "working",
            "heartbeat_at": ten_minutes_ago,
        },
    )

    # The built-in default (3600s) tolerates a 600s-old heartbeat.
    default_result = checks.check_stale_dispatches(
        _context(tmp_path, lock=_ORCHESTRATION_LOCK)
    )
    assert default_result.status == "ok"

    # A project override lowers the threshold below 600s, so the same heartbeat now warns.
    config_path = tmp_path / ".harness" / "orchestration.json"
    config_path.write_text(
        json.dumps({"attention_policy": {"stale_dispatch_seconds": 60}}),
        encoding="utf-8",
    )

    overridden_result = checks.check_stale_dispatches(
        _context(tmp_path, lock=_ORCHESTRATION_LOCK)
    )
    assert overridden_result.status == "warn"


# --- orchestration.orphaned_worktrees (DoD 4) --------------------------------------------------


def test_owner_reports_access_missing_on_a_stat_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(_self: Path) -> int:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "stat", _raise)

    assert checks._owner(tmp_path) == "неизвестен (доступ отсутствует)"


def test_owner_falls_back_to_uid_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.platform", "win32")

    assert checks._owner(tmp_path) == str(tmp_path.stat().st_uid)


def test_orphaned_worktrees_warns_with_owner_and_deletes_nothing(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    worktrees_dir = tmp_path / ".harness" / ".sandboxes" / "worktrees"
    orphan = worktrees_dir / "orphan-1"
    orphan.mkdir(parents=True)
    marker = orphan / "marker.txt"
    marker.write_text("keep me", encoding="utf-8")

    result = checks.check_orphaned_worktrees(
        _context(tmp_path, lock=_ORCHESTRATION_LOCK)
    )

    assert result.status == "warn"
    assert "orphan-1" in result.message
    assert "owner=" in result.message
    assert result.fix is not None
    assert result.fix.command == shell_join(["harness", "cleanup", str(tmp_path), "--mode", "hard"])
    assert orphan.is_dir()
    assert marker.read_text(encoding="utf-8") == "keep me"


def test_orphaned_worktrees_ok_when_registered_by_git_worktree_list(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=test",
            "commit",
            "-q",
            "-m",
            "init",
        ],
        cwd=tmp_path,
        check=True,
    )
    worktree_path = tmp_path / ".harness" / ".sandboxes" / "worktrees" / "linked"
    worktree_path.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature/issue-1-x", str(worktree_path)],
        cwd=tmp_path,
        check=True,
    )

    result = checks.check_orphaned_worktrees(
        _context(tmp_path, lock=_ORCHESTRATION_LOCK)
    )

    assert result.status == "ok"
    assert worktree_path.is_dir()  # still present; nothing was pruned


def test_orphaned_worktrees_ok_when_referenced_by_an_active_batch(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    ledger = _ledger(tmp_path)
    worktree_path = tmp_path / ".harness" / ".sandboxes" / "worktrees" / "active-1"
    worktree_path.mkdir(parents=True)
    generation = ledger.records_root()
    record: JsonObject = {
        "batch_id": "batch-active",
        "state": "active",
        "coordinator_approval": None,
        "dispatches": [],
        "worktree": str(worktree_path),
    }
    ledger.write_immutable(
        generation / "plans" / "batch-active.json", {"batch_id": "batch-active"}
    )
    ledger.write_immutable(generation / "batches" / "batch-active.json", record)

    result = checks.check_orphaned_worktrees(
        _context(tmp_path, lock=_ORCHESTRATION_LOCK)
    )

    assert result.status == "ok"


def test_orphaned_worktrees_skipped_when_git_worktree_list_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Story 21: "not checked" is never reported as "passed" - a failed `git worktree list`
    means skipped."""
    import harness.cleanup

    monkeypatch.setattr(harness.cleanup, "_registered_worktrees", lambda repo: None)

    result = checks.check_orphaned_worktrees(_context(tmp_path, lock=_ORCHESTRATION_LOCK))

    assert result.status == "skipped"


# --- orchestration.disposable_data (DoD 5) ------------------------------------------------------


def test_disposable_data_ok_when_nothing_to_clean(tmp_path: Path) -> None:
    _init_repo(tmp_path)

    result = checks.check_disposable_data(_context(tmp_path, lock=_ORCHESTRATION_LOCK))

    assert result.status == "ok"


def test_disposable_data_reports_a_nonzero_size_as_information_with_a_cleanup_hint(
    tmp_path: Path,
) -> None:
    """Epic #341 severity rules: disposable data is informational - never a `warn`."""
    _init_repo(tmp_path)
    scratch = tmp_path / ".harness" / ".sandboxes" / "scratch"
    scratch.mkdir(parents=True)
    stale_file = scratch / "old.txt"
    stale_file.write_text("x" * 100, encoding="utf-8")
    old_time = time.time() - 48 * 3600
    os.utime(stale_file, (old_time, old_time))

    result = checks.check_disposable_data(_context(tmp_path, lock=_ORCHESTRATION_LOCK))

    assert result.status == "ok"
    assert "100 B" in result.message
    # An informational `ok` carries no "Как исправить" remedy; the preview hint is in the message.
    assert result.fix is None
    assert shell_join(["harness", "cleanup", str(tmp_path), "--mode", "hard"]) in result.message
    assert stale_file.exists()  # preview only; nothing was removed
