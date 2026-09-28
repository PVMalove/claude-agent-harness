"""Harness section: every harness and ledger-maintenance command from
harness.console.catalog.HARNESS_COMMANDS that can run in this repository, each listed with its CLI
equivalent. A reversible command without inputs runs immediately; any other command first opens
`CommandFormScreen`, which asks for its inputs, states why an irreversible class needs
confirmation, and requires a typed word such as RESET. Cancelling it runs nothing."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, ListView

from ..catalog import HARNESS_COMMANDS, CatalogEntry, process_argv
from ..runner import CommandRunner, capturing_runner
from .commands import CommandForm, CommandMenuScreen


class CommandFormScreen(CommandForm):
    def __init__(self, entry: CatalogEntry, repo: Path) -> None:
        super().__init__()
        self.entry = entry
        self.repo = repo

    def compose(self) -> ComposeResult:
        entry = self.entry
        with Vertical(id="command-form"):
            yield from self.form_header(entry.title, entry.cli_line(self.repo), entry.reversibility)
            for name in entry.inputs:
                yield Input(placeholder=name, id=f"input-{name}")
            if entry.typed_confirmation is not None:
                yield Input(
                    placeholder=f"Введите {entry.typed_confirmation}", id="typed-confirmation"
                )
            yield from self.form_buttons(entry.reversibility)

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


class HarnessScreen(CommandMenuScreen):
    def __init__(
        self,
        repo: Path,
        *,
        command_runner: CommandRunner = capturing_runner,
        catalog: Sequence[CatalogEntry] = HARNESS_COMMANDS,
    ) -> None:
        super().__init__(repo, command_runner=command_runner)
        # A command whose script is absent here (e.g. verify outside the harness repository)
        # would only ever fail with "can't open file", so it is not offered.
        self._entries = {entry.key: entry for entry in catalog if entry.available(repo)}

    def menu_items(self) -> Sequence[tuple[str, str]]:
        return [
            (entry.key, f"{entry.title}\n  $ {entry.cli_line(self.repo)}")
            for entry in self._entries.values()
        ]

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
        argv = process_argv(
            entry.cli_argv(self.repo, values), self.repo, dev_environment=entry.dev_environment
        )
        self.run_process(entry.cli_line(self.repo, values), argv)
