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
import sys
from pathlib import Path
from typing import Callable, Sequence

from .pin import TEXTUAL_PIN
from .runner import CommandRunner, default_runner

# Set on the relaunched `uv run` subprocess's own environment so it knows not to relaunch again.
RELAUNCH_ENV = "HARNESS_CONSOLE_RELAUNCHED"

BIN_HARNESS_PATH = Path(__file__).resolve().parent.parent / "bin" / "harness.py"


def find_uv() -> str | None:
    """Resolve `uv` the same way harness.health.checks.environment.check_uv does: `shutil.which`,
    never a hardcoded path."""
    return shutil.which("uv")


def build_relaunch_argv(uv: str, repo: Path, extra_argv: Sequence[str] = ()) -> list[str]:
    """The exact relaunch command: `--no-project` so `uv run` never installs the surrounding
    project's own dependencies or touches its lock file, `--with textual==<pin>` so the one-off
    environment carries only the pinned TUI dependency, and `--python <this interpreter>` so the
    one-off environment reuses the interpreter that already passed the harness's Python >= 3.12
    check instead of whichever one uv would discover first."""
    return [
        uv,
        "run",
        "--no-project",
        "--python",
        sys.executable,
        "--with",
        f"textual=={TEXTUAL_PIN}",
        "python",
        str(BIN_HARNESS_PATH),
        "console",
        str(repo),
        *extra_argv,
    ]


def _default_app_runner(repo: Path) -> int:
    from . import app as console_app

    return console_app.run(repo)


def run_console(
    repo: Path,
    extra_argv: Sequence[str] = (),
    *,
    runner: CommandRunner = default_runner,
    app_runner: Callable[[Path], int] = _default_app_runner,
) -> int:
    """Entry point `cmd_console` calls. Three paths:

    1. Already relaunched (`RELAUNCH_ENV` set by our own subprocess call below): run the (real,
       by default) textual App in-process via `app_runner` - the only path that ever imports
       `textual`, and the only parameter a test overrides to avoid that import.
    2. `uv` not on PATH: print why and fall back to the stdlib `harness health` report.
    3. `uv` found: relaunch via `runner`; a non-zero exit (offline, resolution failure, ...) falls
       back the same way as (2). Diagnostics never depends on textual being installed.
    """
    if os.environ.get(RELAUNCH_ENV) == "1":
        return app_runner(repo)

    uv = find_uv()
    if uv is None:
        _print_fallback(
            repo,
            "harness console: uv не найден в PATH — TUI недоступен, показан отчёт harness health",
        )
        return 1

    argv = build_relaunch_argv(uv, repo, extra_argv)
    env = dict(os.environ)
    env[RELAUNCH_ENV] = "1"
    result = runner(argv, env=env)
    if result.returncode != 0:
        _print_fallback(
            repo,
            "harness console: не удалось запустить textual через uv "
            f"(uv run завершился с кодом {result.returncode}) — показан отчёт harness health",
        )
    return result.returncode


def _print_fallback(repo: Path, reason: str) -> None:
    """Print the reason, then the same text report `harness health` prints - via its two public
    entry points (harness.health.registry.run / harness.health.render.render_text), never a
    reimplementation of check logic."""
    print(reason)
    from ..health import registry as health_registry
    from ..health import render as health_render

    report = health_registry.run(repo)
    print(health_render.render_text(report), end="")
