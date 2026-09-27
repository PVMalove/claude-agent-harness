"""The command-runner seam the console uses for anything that spawns a process outside its own
read-only data collection (harness.console.data): the `uv run` relaunch in launcher.py and the
Diagnostics screen's "apply fixes" action in app.py. A Pilot test injects a recording double here
instead of touching the real environment or shell."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Mapping, Protocol, Sequence


class CommandRunner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> "subprocess.CompletedProcess[str]": ...


def default_runner(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(argv),
        env=dict(env) if env is not None else None,
        cwd=cwd,
        text=True,
    )
