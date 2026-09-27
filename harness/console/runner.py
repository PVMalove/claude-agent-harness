"""The command-runner seam the console uses for anything that spawns a process outside its own
read-only data collection (harness.console.data): the `uv run` relaunch in launcher.py and the
catalog commands the Harness, Orchestration and Repo Map screens run. A Pilot test injects a
recording double here instead of touching the real environment or shell."""

from __future__ import annotations

import os
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


def capturing_runner(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> "subprocess.CompletedProcess[str]":
    """The runner for commands started from inside the TUI, which owns the terminal: output is
    captured for the screen to show, and stdin is closed so a command that would prompt (such as
    `harness init`) takes its non-interactive default instead of competing for the keyboard.
    `PYTHONUTF8` makes a Python child write the UTF-8 this decodes."""
    child_env = dict(env if env is not None else os.environ)
    child_env.setdefault("PYTHONUTF8", "1")
    return subprocess.run(
        list(argv),
        env=child_env,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
