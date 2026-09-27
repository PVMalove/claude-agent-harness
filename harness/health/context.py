"""Shared, once-per-run context passed to every health check function."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .model import JsonObject


@dataclass(frozen=True)
class HealthContext:
    """Built once per `harness health` run so individual checks do not each re-read
    .harness/harness.lock.

    `snapshot_diff` is optional and defaults to None: it is the one detection function that cannot
    move into this stdlib-only package (it re-derives expected package content from
    CAPABILITIES.json and the harness/ source tree, neither of which ships to an installed
    project). Only the canonical `harness health` CLI (harness/bin/harness's cmd_health) supplies
    its own already-loaded snapshot_diff here; a shipped, standalone harness/health/ leaves it None
    and files.check_skill_snapshot reports 'skipped' instead of failing to import it.
    """

    repo: Path
    lock: JsonObject | None
    online: bool
    snapshot_diff: Callable[[Path], JsonObject] | None = None
