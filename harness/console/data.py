"""Stdlib-only fact collection for the console's Dashboard and Diagnostics screens. Every function
here is importable and runnable with no `textual` installed - screens/*.py only ever render what
these functions return, never touch health-check logic or the packaging catalog directly.

`harness.health.registry.run` is called in-process through `run_health`, with the same
`snapshot_diff` and CLI invocation `harness/bin/harness.py`'s `cmd_health` passes, so the console's
report is the CLI's report (skill-snapshot drift included), not a reduced copy. Drift status and active batch count are read
through the packager's and coordinator's own `--json` CLIs by subprocess, rather than duplicating
their capability-resolution/ledger logic inside the console (both stay optional facts: a project
with no `.harness/harness.lock`, or no backend-orchestration, renders "не подключено").
"""

from __future__ import annotations

import functools
import json
import runpy
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, cast

from ..health import registry as health_registry
from ..health.context import shell_join
from ..health.model import JsonObject, Report

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = _PACKAGE_ROOT / "VERSION"
BIN_HARNESS_PATH = _PACKAGE_ROOT / "bin" / "harness.py"


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


@functools.cache
def _packager() -> dict[str, object]:
    """The packager CLI's module namespace, loaded once: it owns `snapshot_diff`, which re-derives
    the expected skill snapshot from CAPABILITIES.json and cannot live in the health package."""
    return runpy.run_path(str(BIN_HARNESS_PATH))


def run_health(repo: Path, *, online: bool = False, fix: bool = False) -> Report:
    """The same health run `harness health [--online] [--fix]` makes."""
    packager = _packager()
    return health_registry.run(
        repo,
        online=online,
        fix=fix,
        snapshot_diff=cast(Callable[[Path], JsonObject], packager["snapshot_diff"]),
        harness_cli=cast(tuple[str, ...], packager["HARNESS_CLI"]),
    )


def collect_dashboard(repo: Path, *, online: bool = False) -> DashboardData:
    """`online=True` is the dashboard's "online checks" action; by default the summary stays
    offline so opening the console never waits on the network."""
    report = run_health(repo, online=online)
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
    action - the same run `harness health --online` makes, no new check logic."""
    return run_health(repo, online=online)


def apply_local_fixes(repo: Path, *, online: bool = False) -> Report:
    """The in-process half of `harness health --fix` (#399): applies every check's `FIXERS`
    entry (creating missing `.harness` directories, regenerating the skill registry) and re-runs
    every check, the same run `harness health --fix` makes. A check's `fix.command` is never run: it
    is a remedy the developer runs by hand."""
    return run_health(repo, online=online, fix=True)


def health_cli_line(repo: Path, *flags: str) -> str:
    """The CLI equivalent a console health action shows, e.g. `harness health <repo> --online`."""
    return shell_join(["harness", "health", str(repo), *flags])
