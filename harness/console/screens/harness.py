"""Harness section: every harness and ledger-maintenance command from
harness.console.catalog.HARNESS_COMMANDS, each listed with its CLI equivalent. A reversible command
without inputs runs immediately; any other command first opens `CommandFormScreen`, which asks for
its inputs, states why an irreversible class needs confirmation, and requires a typed word such as
RESET. Cancelling it runs nothing."""

from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess
from typing import Mapping, Sequence

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Header, Input, ListItem, ListView, Static

from ..catalog import CONFIRMATION_REASONS, HARNESS_COMMANDS, CatalogEntry, process_argv
from ..runner import CommandRunner, capturing_runner

_OUTPUT_TAIL_LINES = 200


class CommandFormScreen(ModalScreen["dict[str, str] | None"]):
    """Dismisses with the entered input values to run the command, or None when cancelled."""

    BINDINGS = [Binding("escape", "cancel", "Отмена")]

    def __init__(self, entry: CatalogEntry, repo: Path) -> None:
        super().__init__()
        self.entry = entry
        self.repo = repo

    def compose(self) -> ComposeResult:
        entry = self.entry
        with Vertical(id="command-form"):
            yield Static(entry.title)
            yield Static(f"$ {entry.cli_line(self.repo)}", id="form-cli", markup=False)
            if entry.needs_confirmation:
                yield Static(CONFIRMATION_REASONS[entry.reversibility], id="confirmation-reason")
            for name in entry.inputs:
                yield Input(placeholder=name, id=f"input-{name}")
            if entry.typed_confirmation is not None:
                yield Input(
                    placeholder=f"Введите {entry.typed_confirmation}", id="typed-confirmation"
                )
            yield Static("", id="form-error")
            with Horizontal():
                yield Button(
                    "Выполнить",
                    id="run",
                    variant="error" if entry.needs_confirmation else "primary",
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
        values = {
            name: self.query_one(f"#input-{name}", Input).value.strip()
            for name in self.entry.inputs
        }
        missing = [name for name, value in values.items() if not value]
        word = self.entry.typed_confirmation
        if missing:
            self._error(f"Заполните: {', '.join(missing)}")
        elif word is not None and self.query_one("#typed-confirmation", Input).value != word:
            self._error(f"Для запуска введите {word}")
        else:
            self.dismiss(values)

    def _error(self, message: str) -> None:
        self.query_one("#form-error", Static).update(message)


def _render_result(cli_line: str, result: "CompletedProcess[str]") -> str:
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    lines = output.rstrip().splitlines()[-_OUTPUT_TAIL_LINES:]
    return "\n".join([f"$ {cli_line}", f"код выхода: {result.returncode}", *lines])


class HarnessScreen(Screen[None]):
    BINDINGS = [Binding("escape", "app.pop_screen", "Назад")]

    def __init__(
        self,
        repo: Path,
        *,
        command_runner: CommandRunner = capturing_runner,
        catalog: Sequence[CatalogEntry] = HARNESS_COMMANDS,
    ) -> None:
        super().__init__()
        self.repo = repo
        self._command_runner = command_runner
        self._entries = {entry.key: entry for entry in catalog}

    def compose(self) -> ComposeResult:
        yield Header()
        yield ListView(
            *(
                ListItem(
                    Static(f"{entry.title}\n  $ {entry.cli_line(self.repo)}", markup=False),
                    name=entry.key,
                )
                for entry in self._entries.values()
            ),
            id="command-menu",
        )
        yield VerticalScroll(Static("", id="command-output", markup=False))
        yield Footer()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.item.name is None:
            return
        entry = self._entries[event.item.name]
        if not entry.needs_confirmation and not entry.inputs:
            self._run(entry, {})
            return

        def on_form_closed(values: "dict[str, str] | None") -> None:
            if values is not None:
                self._run(entry, values)

        self.app.push_screen(CommandFormScreen(entry, self.repo), on_form_closed)

    def _run(self, entry: CatalogEntry, values: Mapping[str, str]) -> None:
        cli_argv = entry.cli_argv(self.repo, values)
        cli_line = entry.cli_line(self.repo, values)
        self._show(f"$ {cli_line}\nвыполняется…")

        def work() -> None:
            try:
                result = self._command_runner(process_argv(cli_argv, self.repo), cwd=self.repo)
            except OSError as exc:
                text = f"$ {cli_line}\nне удалось запустить: {exc}"
            else:
                text = _render_result(cli_line, result)
            self.app.call_from_thread(self._show, text)

        self.run_worker(work, thread=True, exclusive=True, group="command")

    def _show(self, text: str) -> None:
        self.query_one("#command-output", Static).update(text)
