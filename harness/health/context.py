"""Shared, once-per-run context passed to every health check function."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .model import JsonObject


@dataclass(frozen=True)
class HealthContext:
    """Built once per `harness health` run so individual checks do not each re-read
    .harness/harness.lock."""

    repo: Path
    lock: JsonObject | None
    online: bool
