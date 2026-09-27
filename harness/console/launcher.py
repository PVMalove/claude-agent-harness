"""Relaunch `harness console` under a one-off `uv run --with textual==<pin>` environment
(ADR 0025) without ever touching the target project's own dependencies: `dependencies` in
`pyproject.toml` stays `[]`, and `--no-project` keeps `uv run` from installing anything from a
`pyproject.toml`/`uv.lock` it happens to run inside.

This module and its `run_console` entry point stay importable with no `textual` installed: the
relaunch itself only ever constructs a subprocess argv and runs it via an injected
`CommandRunner`. `harness.console.app` (the actual `textual` UI) is imported lazily, only once
`run_console` has confirmed the process is already inside the relaunched subprocess.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Sequence

from .pin import TEXTUAL_PIN
from .runner import CommandRunner, default_runner

# Set on the relaunched `uv run` subprocess's own environment so it knows not to relaunch again.
RELAUNCH_ENV = "HARNESS_CONSOLE_RELAUNCHED"

BIN_HARNESS_PATH = Path(__file__).resolve().parent.parent / "bin" / "harness"


def find_uv() -> str | None:
    """Resolve `uv` the same way harness.health.checks.environment.check_uv does: `shutil.which`,
    never a hardcoded path."""
    return shutil.which("uv")


def build_relaunch_argv(uv: str, repo: Path, extra_argv: Sequence[str] = ()) -> list[str]:
    """The exact relaunch command: `--no-project` so `uv run` never installs the surrounding
    project's own dependencies or touches its lock file, `--with textual==<pin>` so the one-off
    environment carries only the pinned TUI dependency."""
    return [
        uv,
        "run",
        "--no-project",
        "--with",
        f"textual=={TEXTUAL_PIN}",
        "python",
        str(BIN_HARNESS_PATH),
        "console",
        str(repo),
        *extra_argv,
    ]


def run_console(
    repo: Path,
    extra_argv: Sequence[str] = (),
    *,
    runner: CommandRunner = default_runner,
) -> int:
    """Entry point `cmd_console` calls. Two paths so far:

    1. Already relaunched (`RELAUNCH_ENV` set by our own subprocess call below): import and run
       the real textual App in-process - this is the only path that ever imports `textual`.
    2. `uv` found: relaunch via `runner` and return its exit code.

    The no-`uv`/relaunch-failure fallback (print the reason, then the stdlib `harness health`
    report) is added in the next commit.
    """
    if os.environ.get(RELAUNCH_ENV) == "1":
        from . import app as console_app

        return console_app.run(repo)

    uv = find_uv()
    if uv is None:
        return 1

    argv = build_relaunch_argv(uv, repo, extra_argv)
    env = dict(os.environ)
    env[RELAUNCH_ENV] = "1"
    result = runner(argv, env=env)
    return result.returncode
