"""The harness console's textual App. Imported only from harness.console.launcher.run_console,
and only once that has already confirmed the process is running inside the relaunched `uv run
--with textual==<pin>` subprocess - never at CLI module load time, so every non-interactive path
(CI, agents, every other `harness` subcommand) stays free of a `textual` dependency."""

from __future__ import annotations

from pathlib import Path

from textual.app import App

from .runner import CommandRunner, capturing_runner
from .screens.dashboard import DashboardScreen


class HarnessConsoleApp(App[None]):
    """Accepts an injected `command_runner`, threaded through to the Diagnostics screen's
    "apply fixes" action and the Harness screen's commands, so a Pilot test never spawns a real
    process."""

    TITLE = "harness console"

    def __init__(self, repo: Path, *, command_runner: CommandRunner = capturing_runner) -> None:
        super().__init__()
        self.repo = repo
        self.command_runner = command_runner

    def on_mount(self) -> None:
        self.push_screen(DashboardScreen(self.repo, command_runner=self.command_runner))


def run(repo: Path, *, command_runner: CommandRunner = capturing_runner) -> int:
    """harness.console.launcher.run_console's entry point once relaunched."""
    HarnessConsoleApp(repo, command_runner=command_runner).run()
    return 0
