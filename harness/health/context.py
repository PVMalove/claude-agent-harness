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
    # stdout's encoding as the caller saw it, before any in-process reconfiguration (the canonical
    # CLI forces UTF-8 on startup, which would otherwise hide a non-UTF-8 console from
    # environment.check_output_encoding). None means "read sys.stdout at check time".
    output_encoding: str | None = None
    # Why .harness/harness.lock exists but could not be used (unreadable, not JSON, not an object);
    # `lock` is None then too, so lock-dependent checks skip while files.check_lock reports `fail`.
    lock_error: str | None = None
