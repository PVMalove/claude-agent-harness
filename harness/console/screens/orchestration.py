"""Orchestration section: every operator-facing coordinator command from
harness.console.coordinator_catalog.COORDINATOR_COMMANDS, grouped by `group` (batch, decide,
packet, dispatch, qa, risk, context-package, ledger). Every command first opens
`CoordinatorCommandFormScreen`, which asks for its arguments, states why an irreversible class
needs confirmation, and validates required fields (and choice-restricted fields) before it lets
the command run. Cancelling it runs nothing - the console never reimplements the coordinator's own
validation or invariants, it only runs the same `python .harness/orchestration/coordinator.py
--repo {repo} <subcommand path> ...` CLI `harness/console/screens/harness.py` already uses for the
`ledger-*` entries."""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Checkbox, Input, ListView, Static, TextArea

from ..coordinator_catalog import COORDINATOR_COMMANDS, CoordinatorCommand, process_argv
from ..runner import CommandRunner, capturing_runner
from .commands import CommandForm, CommandMenuScreen


class CoordinatorCommandFormScreen(CommandForm):
    """A repeatable field gets a multi-line `TextArea`, one value per line, and a value-less flag
    such as `--open` a `Checkbox`."""

    def __init__(self, entry: CoordinatorCommand, repo: Path) -> None:
        super().__init__()
        self.entry = entry
        self.repo = repo

    def compose(self) -> ComposeResult:
        entry = self.entry
        with Vertical(id="command-form"):
            yield from self.form_header(entry.title, entry.cli_line(self.repo), entry.reversibility)
            for field in entry.fields:
                if not field.takes_value:
                    yield Checkbox(field.flag, id=f"input-{field.dest}")
                    continue
                label = field.flag if not field.choices else f"{field.flag} ({'/'.join(field.choices)})"
                yield Static(label, classes="field-label")
                if field.repeatable:
                    yield TextArea(id=f"input-{field.dest}")
                else:
                    yield Input(value=field.default or "", id=f"input-{field.dest}")
            yield from self.form_buttons(entry.reversibility)

    def _submit(self) -> None:
        values: dict[str, str] = {}
        missing: list[str] = []
        invalid: list[str] = []
        for field in self.entry.fields:
            if not field.takes_value:
                checked = self.query_one(f"#input-{field.dest}", Checkbox).value
                values[field.dest] = "1" if checked else ""
            elif field.repeatable:
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


class OrchestrationScreen(CommandMenuScreen):
    """Orchestration section: coordinator commands grouped as `[group] title`."""

    def __init__(
        self,
        repo: Path,
        *,
        command_runner: CommandRunner = capturing_runner,
        catalog: Sequence[CoordinatorCommand] = COORDINATOR_COMMANDS,
    ) -> None:
        super().__init__(repo, command_runner=command_runner)
        self._entries = {entry.key: entry for entry in catalog}

    def menu_items(self) -> Sequence[tuple[str, str]]:
        return [
            (entry.key, f"[{entry.group}] {entry.title}\n  $ {entry.cli_line(self.repo)}")
            for entry in self._entries.values()
        ]

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.item.name is None:
            return
        entry = self._entries[event.item.name]

        def on_form_closed(values: "dict[str, str] | None") -> None:
            if values is not None:
                self._run(entry, values)

        self.app.push_screen(CoordinatorCommandFormScreen(entry, self.repo), on_form_closed)

    def _run(self, entry: CoordinatorCommand, values: Mapping[str, str]) -> None:
        self.run_process(
            entry.cli_line(self.repo, values), process_argv(entry.cli_argv(self.repo, values))
        )
