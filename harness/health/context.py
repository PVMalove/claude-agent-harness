"""Shared, once-per-run context passed to every health check function."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .model import JsonObject

_BROKEN_LOCK_MESSAGE = "не проверено: .harness/harness.lock повреждён (см. files.lock)"


def shell_join(argv: list[str]) -> str:
    """One copy-pasteable command line for this platform's shell."""
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


@dataclass(frozen=True)
class HealthContext:
    """Built once per `harness health` run so individual checks do not each re-read
    .harness/harness.lock.

    `snapshot_diff` is optional and defaults to None: it is the one detection function that cannot
    move into this stdlib-only package (it re-derives expected package content from
    CAPABILITIES.json and the harness/ source tree, neither of which ships to an installed
    project). Only the canonical `harness health` CLI (harness/bin/harness.py's cmd_health) supplies
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
    # How to invoke the harness CLI in a printed remedy. The canonical CLI passes its own interpreter
    # and script path so the command runs as printed; a shipped, standalone package keeps the
    # documented `harness` alias.
    harness_cli: tuple[str, ...] = ("harness",)

    def no_lock_message(self) -> str:
        """Why a lock-dependent check is skipped: no lock, or a lock files.check_lock reports broken."""
        return _BROKEN_LOCK_MESSAGE if self.lock_error else "нет .harness/harness.lock"

    def no_orchestration_message(self) -> str:
        """Why a backend-orchestration check is skipped - never "not selected" for a broken lock."""
        if self.lock_error:
            return _BROKEN_LOCK_MESSAGE
        return "backend-orchestration capability не выбрана"

    def harness_command(self, *args: str) -> str:
        """A remedy command for the harness CLI, with this repository's path filled in."""
        return shell_join([*self.harness_cli, *args])

    def coordinator_command(self, *args: str) -> str:
        """A remedy command for this repository's coordinator, runnable from any directory."""
        script = self.repo / ".harness" / "orchestration" / "coordinator.py"
        return shell_join([sys.executable, str(script), "--repo", str(self.repo), *args])
