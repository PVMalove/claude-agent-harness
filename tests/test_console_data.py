"""harness.console.data: stdlib-only fact collection for the console. No textual import - this
must stay importable and correct with no TUI dependency installed at all."""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

from harness.console import data as console_data


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def test_harness_version_reads_the_package_version_file() -> None:
    assert console_data.harness_version() == console_data.VERSION_FILE.read_text(
        encoding="utf-8"
    ).strip()
    assert console_data.harness_version()


def test_drift_state_reports_missing_lock(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    assert console_data.drift_state(tmp_path) == "missing"


def test_active_batches_is_none_when_orchestration_is_not_connected(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    assert console_data.active_batches(tmp_path) is None


def _write_fake_coordinator(repo: Path, batches: list[dict[str, object]]) -> None:
    coordinator_dir = repo / ".harness" / "orchestration"
    coordinator_dir.mkdir(parents=True, exist_ok=True)
    script = coordinator_dir / "coordinator.py"
    payload = json.dumps({"batches": batches})
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"print({payload!r})\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def test_active_batches_counts_open_batches_from_the_coordinator_cli(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_fake_coordinator(
        tmp_path,
        [{"batch_id": "batch-a", "state": "active"}, {"batch_id": "batch-b", "state": "active"}],
    )
    assert console_data.active_batches(tmp_path) == 2


def test_collect_dashboard_returns_offline_counters_and_optional_facts(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    dashboard = console_data.collect_dashboard(tmp_path)

    assert dashboard.ok + dashboard.warn + dashboard.fail + dashboard.skipped > 0
    assert dashboard.harness_version
    assert dashboard.drift_state == "missing"
    assert dashboard.active_batches is None


def test_collect_diagnostics_returns_the_full_offline_report(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    report = console_data.collect_diagnostics(tmp_path)
    assert report.online is False
    assert report.checks
