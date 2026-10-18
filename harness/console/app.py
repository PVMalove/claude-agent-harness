"""The harness console's textual App. Imported only from harness.console.launcher.run_console,
and only once that has already confirmed the process is running inside the relaunched `uv run
--with textual==<pin>` subprocess - never at CLI module load time, so every non-interactive path
(CI, agents, every other `harness` subcommand) stays free of a `textual` dependency."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from threading import Lock
from typing import Iterable, Mapping, Sequence, cast

from textual.app import App, SystemCommand
from textual.binding import Binding
from textual.command import CommandPalette
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.scrollbar import ScrollBar
from textual.theme import Theme

from . import brand, clipboard, i18n, translate
from .chrome import HealthCounts
from .launcher import restart_console
from .runner import CommandRunner, capturing_runner
from .screens.dashboard import DashboardScreen
from .style import ThinScrollBarRender

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

    TITLE = " Harness console "
    BINDINGS = [
        Binding("f1", "open_help", "Справка"),
        Binding("f2", "toggle_language", "RU/EN"),
    ]
    # Thin rounded frames and warm accents shared by every screen. Only the screen itself is
    # filled: every widget is transparent and set apart by its frame; a selected item or a hovered
    # button is marked by colour, never by a fill. Modal dialogs keep the screen colour so the
    # screen behind them does not show through.
    CSS = """
    Screen { background: $background; }
    Screen * { background: transparent; }
    ModalScreen { background: $background 70%; }
    * {
        scrollbar-size: 1 1;
        scrollbar-background: $panel-lighten-2;
        scrollbar-background-hover: $panel-lighten-2;
        scrollbar-background-active: $panel-lighten-2;
        scrollbar-color: $primary;
        scrollbar-color-hover: $primary;
        scrollbar-color-active: $primary;
    }
    ModalScreen > * { background: $background; }
    .frame {
        margin: 0 1; padding: 0 1;
        border: round $panel-lighten-2; border-title-color: $primary;
    }
    .frame:focus, .frame:focus-within { border: round $primary; }
    .actions, .action-row {
        height: auto;
        overflow-x: auto;
    }
    .actions > Button, .action-row > Button { margin-right: 1; }
    .actions > Button:last-of-type, .action-row > Button:last-of-type { margin-right: 0; }
    Footer { overflow-x: hidden; }
    CommandPalette { background: $background 70%; }
    CommandPalette > Vertical#--container {
        visibility: visible; width: 80%; max-width: 100; height: auto; max-height: 80%;
        margin: 2 0 0 0; padding: 0 1; background: $background;
        border: round $primary; border-title-color: $primary;
    }
    CommandPalette #--input { border: none; border-bottom: solid $primary 40%; }
    CommandPalette #--input.--list-visible { border-bottom: solid $primary 40%; }
    CommandPalette SearchIcon { display: none; }
    CommandPalette CommandInput, CommandPalette CommandInput:focus {
        border: none; background: transparent; padding: 0 1; height: 1;
    }
    CommandPalette #--results { overlay: none; height: auto; }
    CommandPalette CommandList {
        height: auto; max-height: 16; border: none; padding: 0; background: transparent;
    }
    CommandPalette CommandList > .option-list--option { padding: 0 1; }
    CommandPalette CommandList > .option-list--option-highlighted,
    CommandPalette CommandList:focus > .option-list--option-highlighted {
        background: transparent; color: $primary; text-style: bold;
    }
    CommandPalette > .command-palette--highlight { text-style: bold; color: $primary; }
    FooterKey .footer-key--key, FooterKey .footer-key--description { background: transparent; }
    ListView { border: round $panel-lighten-2; border-title-color: $primary; }
    ListView:focus { border: round $primary; }
    DashboardScreen #section-menu, DashboardScreen #section-menu:focus {
        border: none; margin: 0; padding: 0;
    }
    ListView > ListItem { padding: 0 1; }
    ListView > ListItem.-highlight, ListView:focus > ListItem.-highlight,
    ListView > ListItem:hover {
        background: transparent; color: $primary; text-style: bold;
    }
    Tree > .tree--cursor, Tree:focus > .tree--cursor, Tree > .tree--highlight,
    Tree > .tree--highlight-line {
        background: transparent; color: $primary; text-style: bold;
    }
    Tab.-active, Tab:hover { background: transparent; color: $primary; text-style: bold; }
    Input, TextArea { border: round $panel-lighten-2; }
    Input:focus, TextArea:focus { border: round $primary; }
    Input { scrollbar-size-horizontal: 0; }
    RadioSet, RadioSet:focus { border: none; padding: 0; background: transparent; }
    Select > SelectCurrent, Select:focus > SelectCurrent, Select.-expanded > SelectCurrent {
        border: round $panel-lighten-2; background: transparent; padding: 0 1;
    }
    Select:focus > SelectCurrent { border: round $primary; }
    ReportsScreen #report-order Select > SelectCurrent {
        border: none; height: 1; padding: 0 1;
    }
    SelectOverlay { border: round $primary; background: $background; }
    Checkbox, Checkbox:focus, RadioButton, RadioButton:focus {
        border: none; padding: 0; background: transparent;
    }
    Checkbox:focus > .toggle--label, RadioButton:focus > .toggle--label {
        text-style: bold; background: transparent;
    }
    SelectionList, SelectionList:focus { border: none; padding: 0; background: transparent; }
    SelectionList > .selection-list--button-highlighted { background: transparent; }
    Input.-textual-compact, Input.-textual-compact:focus { border: none; height: 1; }
    Input.-textual-compact:focus { text-style: bold; }
    Button { border: round $primary 60%; color: $primary; min-width: 16; }
    Button:hover { background: transparent; border: round $primary; text-style: bold; }
    Button:focus { background: transparent; text-style: bold; border: round $primary; }
    Button.-textual-compact, Button.-textual-compact:hover, Button.-textual-compact:focus {
        border: none; min-width: 0; height: 1; padding: 0 1;
    }
    Button.-textual-compact:focus { text-style: bold reverse; }
    """

    def __init__(
        self, repo: Path, *, command_runner: CommandRunner = capturing_runner
    ) -> None:
        super().__init__()
        ScrollBar.renderer = ThinScrollBarRender
        self.repo = repo
        self.health_counts: HealthCounts | None = None
        self.language = i18n.default_language()
        self.restart_requested = False
        self.commands_running = 0
        self._command_lock = Lock()
        self._runner = command_runner
        self.command_runner = self._tracked_runner

    def on_mount(self) -> None:
        self.register_theme(HARNESS_THEME)
        self.theme = brand.THEME_NAME
        self.push_screen(DashboardScreen(self.repo, command_runner=self.command_runner))

    def _tracked_runner(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with self._command_lock:
            self.commands_running += 1
        try:
            return self._runner(argv, env=env, cwd=cwd)
        finally:
            with self._command_lock:
                self.commands_running -= 1

    def activate_project(self, repo: Path) -> None:
        if self.commands_running:
            self.notify("Дождитесь окончания команды", severity="warning")
            return
        self.repo = repo
        self.switch_screen(DashboardScreen(repo, command_runner=self.command_runner))

    def action_open_help(self) -> None:
        from .screens.help import HelpScreen

        if not isinstance(self.screen, HelpScreen):
            self.push_screen(HelpScreen())

    def action_command_palette(self) -> None:
        """Open the palette (the ☰ menu) in the console's own look and language."""
        if not self.use_command_palette or CommandPalette.is_open(
            cast(App[object], self)
        ):
            return
        palette = CommandPalette(
            id="--command-palette",
            placeholder=i18n.ui(self.language, "palette_placeholder"),
        )
        self.push_screen(palette)

        def title() -> None:
            try:
                palette.query_one("#--container").border_title = brand.frame_title(
                    i18n.ui(self.language, "palette")
                )
            except NoMatches:
                pass

        self.call_after_refresh(title)

    def get_system_commands(self, screen: Screen[object]) -> Iterable[SystemCommand]:
        for command in self._console_commands(screen):
            title, help_text = i18n.palette_command(
                self.language, command.title, command.help
            )
            yield command._replace(title=title, help=help_text)

    def _console_commands(self, screen: Screen[object]) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)
        yield SystemCommand(
            "Language / Язык",
            "Switch the menu language between Russian and English (F2)",
            self.action_toggle_language,
        )
        yield SystemCommand(
            "Restart console",
            "Reload console code and keep the selected project",
            self.action_restart_console,
        )

    @property
    def clipboard(self) -> str:
        """What Ctrl+V pastes: the system clipboard when there is one, else the app's own."""
        system = clipboard.read_system()
        return super().clipboard if system is None else system

    def copy_to_clipboard(self, text: str) -> None:
        super().copy_to_clipboard(text)
        clipboard.write_system(text)

    def action_toggle_language(self) -> None:
        """Switch the menu language between Russian and English."""
        self.language = i18n.next_language(self.language)
        # Every screen of the stack: the ones below are translated now, not only when resumed.
        for screen in self.screen_stack:
            translate.retranslate(screen, self.language)
        translate.translate_bindings(self, self.language)
        self.refresh_bindings()
        self.notify(i18n.ui(self.language, "language"))

    def action_restart_console(self) -> None:
        busy = self.commands_running or any(
            not worker.is_finished
            and isinstance(worker.node, Screen)
            and not isinstance(worker.node, CommandPalette)
            for worker in self.workers
        )
        if busy:
            self.notify("Дождитесь окончания текущей операции", severity="warning")
            return
        self.restart_requested = True
        self.exit()


def run(repo: Path, *, command_runner: CommandRunner = capturing_runner) -> int:
    """harness.console.launcher.run_console's entry point once relaunched."""
    app = HarnessConsoleApp(repo, command_runner=command_runner)
    app.run()
    if app.restart_requested:
        try:
            restart_console(app.repo)
        except OSError as exc:
            print(
                f"harness console: не удалось перезапустить консоль: {exc}",
                file=sys.stderr,
            )
            return 1
    return 0
