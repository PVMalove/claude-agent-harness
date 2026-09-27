"""Orchestration section: every in-scope coordinator command from
harness.console.coordinator_catalog.COORDINATOR_COMMANDS, grouped by `group` (batch, decide,
packet, dispatch, risk, context-package). Every command first opens `CoordinatorCommandFormScreen`,
which asks for its arguments, states why an irreversible class needs confirmation, and validates
required fields (and choice-restricted fields) before it lets the command run. Cancelling it runs
nothing - the console never reimplements the coordinator's own validation or invariants, it only
runs the same `python .harness/orchestration/coordinator.py --repo {repo} <group> <sub> ...` CLI
`harness/console/screens/harness.py` already uses for the `ledger-*` entries."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Header, Input, ListItem, ListView, Static, TextArea

from ..coordinator_catalog import (
    CONFIRMATION_REASONS,
    COORDINATOR_COMMANDS,
    CoordinatorCommand,
    process_argv,
)
from ..runner import CommandRunner, capturing_runner
from .harness import _render_result


class CoordinatorCommandFormScreen(ModalScreen["dict[str, str] | None"]):
    """Dismisses with the entered field values (keyed by field `dest`) to run the command, or
    None when cancelled. A repeatable field gets a multi-line `TextArea`, one value per line."""

    BINDINGS = [Binding("escape", "cancel", "Отмена")]

    def __init__(self, entry: CoordinatorCommand, repo: Path) -> None:
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
            for field in entry.fields:
                label = field.flag if not field.choices else f"{field.flag} ({'/'.join(field.choices)})"
                yield Static(label, classes="field-label")
                if field.repeatable:
                    yield TextArea(id=f"input-{field.dest}")
                else:
                    yield Input(value=field.default or "", id=f"input-{field.dest}")
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
        values: dict[str, str] = {}
        missing: list[str] = []
        invalid: list[str] = []
        for field in self.entry.fields:
            if field.repeatable:
                raw = self.query_one(f"#input-{field.dest}", TextArea).text
                lines = [line.strip() for line in raw.splitlines() if line.strip()]
                if field.required and not lines:
                    missing.append(field.flag)
                values[field.dest] = "\n".join(lines)
            else:
                raw = self.query_one(f"#input-{field.dest}", Input).value.strip()
                if field.required and not raw:
                    missing.append(field.flag)
                elif raw and field.choices is not None and raw not in field.choices:
                    invalid.append(field.flag)
                values[field.dest] = raw
        if missing:
            self._error(f"Заполните: {', '.join(missing)}")
        elif invalid:
            self._error(f"Недопустимое значение: {', '.join(invalid)}")
        else:
            self.dismiss(values)

    def _error(self, message: str) -> None:
        self.query_one("#form-error", Static).update(message)


class OrchestrationScreen(Screen[None]):
    """Orchestration section: coordinator commands grouped as `[group] title`."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Назад")]

    def __init__(
        self,
        repo: Path,
        *,
        command_runner: CommandRunner = capturing_runner,
        catalog: Sequence[CoordinatorCommand] = COORDINATOR_COMMANDS,
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
                    Static(
                        f"[{entry.group}] {entry.title}\n  $ {entry.cli_line(self.repo)}",
                        markup=False,
                    ),
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

        def on_form_closed(values: "dict[str, str] | None") -> None:
            if values is not None:
                self._run(entry, values)

        self.app.push_screen(CoordinatorCommandFormScreen(entry, self.repo), on_form_closed)

    def _run(self, entry: CoordinatorCommand, values: Mapping[str, str]) -> None:
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
