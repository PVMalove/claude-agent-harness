"""The flat, explicit registry of health checks that `harness health` runs.

No plugin auto-discovery: checks are wired in by explicit import, one entry per check function, so
the registry stays auditable. Future check groups (#343-#347) add entries here without touching how
already-registered checks run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from harness.health.context import HealthContext
from harness.health.model import CheckResult, JsonObject, Report

CheckFn = Callable[[HealthContext], CheckResult]

REGISTRY: list[CheckFn] = []

_LOCK_REL = Path(".harness/harness.lock")


def _load_lock(repo: Path) -> JsonObject | None:
    """Parse .harness/harness.lock once per run; a missing file means no lock, not a problem."""
    path = repo / _LOCK_REL
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def run(repo: Path, *, online: bool = False) -> Report:
    """Build one HealthContext and run every registered check, without early exit."""
    context = HealthContext(repo=repo, lock=_load_lock(repo), online=online)
    report = Report(schema_version=1, repo=str(repo), online=online)
    for check_fn in REGISTRY:
        report.checks.append(check_fn(context))
    return report
