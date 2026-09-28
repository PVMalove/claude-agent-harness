"""The harness console's textual App. Imported only from harness.console.launcher.run_console,
and only once that has already confirmed the process is running inside the relaunched `uv run
--with textual==<pin>` subprocess - never at CLI module load time, so every non-interactive path
(CI, agents, every other `harness` subcommand) stays free of a `textual` dependency."""

from __future__ import annotations

from pathlib import Path

from textual.app import App
from textual.binding import Binding
from textual.theme import Theme

from . import brand
from .runner import CommandRunner, capturing_runner
from .screens.dashboard import DashboardScreen

HARNESS_THEME = Theme(
    name=brand.THEME_NAME,
    primary=brand.PALETTE["primary"],
    secondary=brand.PALETTE["secondary"],
    accent=brand.PALETTE["accent"],
    warning=brand.PALETTE["warning"],
    error=brand.PALETTE["error"],
    success=brand.PALETTE["success"],
    foreground=brand.PALETTE["foreground"],
    background=brand.PALETTE["background"],
    surface=brand.PALETTE["surface"],
    panel=brand.PALETTE["panel"],
    dark=True,
    variables={
        "text-muted": brand.PALETTE["muted"],
        "footer-key-foreground": brand.PALETTE["primary"],
        "block-cursor-background": brand.PALETTE["primary"],
        "block-cursor-foreground": brand.PALETTE["background"],
    },
)


class HarnessConsoleApp(App[None]):
    """Accepts an injected `command_runner`, threaded through to the screens that run catalog
    commands (Harness, Orchestration, Repo Map), so a Pilot test never spawns a real process."""

    TITLE = "harness console"
    BINDINGS = [Binding("f1", "open_help", "Справка")]
    # Thin rounded frames and warm accents shared by every screen; the palette itself is the
    # registered theme, so widgets keep textual's own focus and hover states.
    CSS = """
    Screen { background: $background; }
    Header { background: $panel; color: $primary; text-style: bold; }
    Footer { background: $panel; }
    ListView { background: $background; border: round $panel-lighten-2; }
    ListView:focus { border: round $primary; }
    ListView > ListItem { padding: 0 1; }
    Input, TextArea { border: round $panel-lighten-2; }
    Input:focus, TextArea:focus { border: round $primary; }
    Button { border: round $primary 60%; background: $background; color: $primary; min-width: 16; }
    Button:hover { background: $primary 20%; }
    Button:focus { text-style: bold; border: round $primary; }
    """

    def __init__(
        self, repo: Path, *, command_runner: CommandRunner = capturing_runner
    ) -> None:
        super().__init__()
        self.repo = repo
        self.command_runner = command_runner

    def on_mount(self) -> None:
        self.register_theme(HARNESS_THEME)
        self.theme = brand.THEME_NAME
        self.push_screen(DashboardScreen(self.repo, command_runner=self.command_runner))

    def action_open_help(self) -> None:
        from .screens.help import HelpScreen

        if not isinstance(self.screen, HelpScreen):
            self.push_screen(HelpScreen())


def run(repo: Path, *, command_runner: CommandRunner = capturing_runner) -> int:
    """harness.console.launcher.run_console's entry point once relaunched."""
    HarnessConsoleApp(repo, command_runner=command_runner).run()
    return 0
