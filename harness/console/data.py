"""Stdlib-only fact collection for the console's Dashboard and Diagnostics screens. Every function
here is importable and runnable with no `textual` installed - screens/*.py only ever render what
these functions return, never touch health-check logic or the packaging catalog directly.

`harness.health.registry.run` (offline health checks) is called in-process, the same public entry
point `harness/bin/harness`'s `cmd_health` uses. Drift status and active batch count are read
through the packager's and coordinator's own `--json` CLIs by subprocess, rather than duplicating
their capability-resolution/ledger logic inside the console (both stay optional facts: a project
with no `.harness/harness.lock`, or no backend-orchestration, renders "не подключено").
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ..health import registry as health_registry
from ..health.model import Report

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = _PACKAGE_ROOT / "VERSION"
BIN_HARNESS_PATH = _PACKAGE_ROOT / "bin" / "harness"


@dataclass(frozen=True)
class DashboardData:
    """Everything the Dashboard screen shows; see DoD: "offline health ok/warn/fail counters,
    active batches, Repo Map tier, harness version and drift"."""

    ok: int
    warn: int
    fail: int
    skipped: int
    repo_map_tier: str
    harness_version: str
    drift_state: str
    active_batches: int | None  # None: backend-orchestration is not connected in this project


def harness_version() -> str:
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def _repo_map_tier(report: Report) -> str:
    for check in report.checks:
        if check.id == "repo_map.tier":
            return check.message
    return "не установлен"


def drift_state(repo: Path, *, timeout: float = 30.0) -> str:
    """Shells out to `harness diff --json`, the packager's own public CLI contract, instead of
    reimplementing capability-resolution/snapshot-hashing inside the console."""
    try:
        result = subprocess.run(
            [sys.executable, str(BIN_HARNESS_PATH), "diff", str(repo), "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return "неизвестно"
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return "неизвестно"
    state = payload.get("state") if isinstance(payload, dict) else None
    return state if isinstance(state, str) else "неизвестно"


def active_batches(repo: Path, *, timeout: float = 30.0) -> int | None:
    """None means backend-orchestration is not connected in this project (no
    `.harness/orchestration/coordinator.py`) - the dashboard then renders "не подключено", the
    same fallback pattern `repo_map.tier` already uses for "not installed"."""
    coordinator = repo / ".harness" / "orchestration" / "coordinator.py"
    if not coordinator.is_file():
        return None
    try:
        result = subprocess.run(
            [sys.executable, str(coordinator), "--repo", str(repo), "batch", "list", "--open"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    batches = payload.get("batches") if isinstance(payload, dict) else None
    if not isinstance(batches, list):
        return None
    return len(batches)


def collect_dashboard(repo: Path) -> DashboardData:
    report = health_registry.run(repo)
    summary = report.summary()
    return DashboardData(
        ok=summary["ok"],
        warn=summary["warn"],
        fail=summary["fail"],
        skipped=summary["skipped"],
        repo_map_tier=_repo_map_tier(report),
        harness_version=harness_version(),
        drift_state=drift_state(repo),
        active_batches=active_batches(repo),
    )


def collect_diagnostics(repo: Path, *, online: bool = False) -> Report:
    """The full report the Diagnostics screen renders; `online=True` is its "online checks"
    action - the same `harness.health.registry.run(..., online=True)` call `cmd_health` would make,
    no new check logic."""
    return health_registry.run(repo, online=online)
