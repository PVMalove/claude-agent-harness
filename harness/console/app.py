"""The harness console's textual App. Imported only from harness.console.launcher.run_console,
and only once that has already confirmed the process is running inside the relaunched `uv run
--with textual==<pin>` subprocess - never at CLI module load time, so every non-interactive path
(CI, agents, every other `harness` subcommand) stays free of a `textual` dependency."""

from __future__ import annotations

from pathlib import Path

from textual.app import App

from .runner import CommandRunner, default_runner
from .screens.dashboard import DashboardScreen


class HarnessConsoleApp(App[None]):
    """Accepts an injected `command_runner` so Pilot tests never spawn a real process (see
    harness/console/screens/diagnostics.py's "apply fixes" action, added in a later commit)."""

    TITLE = "harness console"

    def __init__(self, repo: Path, *, command_runner: CommandRunner = default_runner) -> None:
        super().__init__()
        self.repo = repo
        self.command_runner = command_runner

    def on_mount(self) -> None:
        self.push_screen(DashboardScreen(self.repo))


def run(repo: Path, *, command_runner: CommandRunner = default_runner) -> int:
    """harness.console.launcher.run_console's entry point once relaunched."""
    HarnessConsoleApp(repo, command_runner=command_runner).run()
    return 0
