"""What the Harness and Orchestration sections share: a command menu that shows each command's CLI
equivalent and runs the chosen one in a worker thread (`CommandMenuScreen`), and the modal form
that asks for its arguments and, for an irreversible class, states why it needs confirmation
(`CommandForm`). Cancelling a form runs nothing."""

from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess
from typing import Sequence

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Header, ListItem, ListView, Static

from ..catalog import CONFIRMATION_REASONS, Reversibility
from ..runner import CommandRunner

_OUTPUT_TAIL_LINES = 200


def render_result(cli_line: str, result: "CompletedProcess[str]") -> str:
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    lines = output.rstrip().splitlines()[-_OUTPUT_TAIL_LINES:]
    return "\n".join([f"$ {cli_line}", f"код выхода: {result.returncode}", *lines])


class CommandForm(ModalScreen["dict[str, str] | None"]):
    """Dismisses with the entered values to run the command, or None when cancelled. A subclass
    composes its fields between `form_header` and `form_buttons` and implements `_submit`."""

    BINDINGS = [Binding("escape", "cancel", "Отмена")]

    def form_header(
        self, title: str, cli_line: str, reversibility: Reversibility
    ) -> ComposeResult:
        yield Static(title)
        yield Static(f"$ {cli_line}", id="form-cli", markup=False)
        if reversibility is not Reversibility.REVERSIBLE:
            yield Static(CONFIRMATION_REASONS[reversibility], id="confirmation-reason")

    def form_buttons(self, reversibility: Reversibility) -> ComposeResult:
        yield Static("", id="form-error")
        with Horizontal():
            yield Button(
                "Выполнить",
                id="run",
                variant="primary" if reversibility is Reversibility.REVERSIBLE else "error",
            )
            yield Button("Отмена", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "run":
            self._submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        raise NotImplementedError

    def _error(self, message: str) -> None:
        self.query_one("#form-error", Static).update(message)


class CommandMenuScreen(Screen[None]):
    """A `#command-menu` of `(key, label)` items above a `#command-output` pane."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Назад")]

    def __init__(self, repo: Path, *, command_runner: CommandRunner) -> None:
        super().__init__()
        self.repo = repo
        self._command_runner = command_runner

    def menu_items(self) -> Sequence[tuple[str, str]]:
        raise NotImplementedError

    def compose(self) -> ComposeResult:
        yield Header()
        yield ListView(
            *(
                ListItem(Static(label, markup=False), name=key)
                for key, label in self.menu_items()
            ),
            id="command-menu",
        )
        yield VerticalScroll(Static("", id="command-output", markup=False))
        yield Footer()

    def run_process(self, cli_line: str, argv: list[str]) -> None:
        """Run `argv` (the process behind `cli_line`) in the repository without blocking the TUI."""
        self._show(f"$ {cli_line}\nвыполняется…")

        def work() -> None:
            try:
                result = self._command_runner(argv, cwd=self.repo)
            except OSError as exc:
                text = f"$ {cli_line}\nне удалось запустить: {exc}"
            else:
                text = render_result(cli_line, result)
            self.app.call_from_thread(self._show, text)

        self.run_worker(work, thread=True, exclusive=True, group="command")

    def _show(self, text: str) -> None:
        self.query_one("#command-output", Static).update(text)
