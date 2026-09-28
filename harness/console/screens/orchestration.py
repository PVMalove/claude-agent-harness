"""Раздел Orchestration: команды координатора из `harness.console.coordinator_catalog.COORDINATOR_COMMANDS`,
сгруппированные по группам `group` (batch, decide, packet, dispatch, qa, risk, context-package, ledger).
Каждая команда открывает модальный экран `CoordinatorCommandFormScreen`, запрашивающий аргументы,
поясняющий причины подтверждения для терминальных действий и валидирующий обязательные поля
(и ограничения choices=) перед выполнением. Отмена формы ничего не выполняет.
"""

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
    """Модальная форма параметров для подкоманд координатора с поддержкой списков и флагов."""

    def __init__(self, entry: CoordinatorCommand, repo: Path) -> None:
        """Инициализирует форму параметров команды координатора для репозитория."""
        super().__init__()
        self.entry = entry
        self.repo = repo

    def compose(self) -> ComposeResult:
        """Формирует структуру полей ввода формы на основе метаданных команды координатора."""
        entry = self.entry
        with Vertical(id="command-form"):
            yield from self.form_header(
                entry.title, entry.cli_line(self.repo), entry.reversibility
            )
            for field in entry.fields:
                if not field.takes_value:
                    yield Checkbox(field.flag, id=f"input-{field.dest}")
                    continue
                label = (
                    field.flag
                    if not field.choices
                    else f"{field.flag} ({'/'.join(field.choices)})"
                )
                yield Static(label, classes="field-label")
                if field.repeatable:
                    yield TextArea(id=f"input-{field.dest}")
                else:
                    yield Input(value=field.default or "", id=f"input-{field.dest}")
            yield from self.form_buttons(entry.reversibility)

    def _submit(self) -> None:
        """Считывает значения полей формы, проверяет обязательные поля и допустимость значений."""
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
    """Экран раздела Orchestration со списком команд координатора, сгруппированных по категориям."""

    def __init__(
        self,
        repo: Path,
        *,
        command_runner: CommandRunner = capturing_runner,
        catalog: Sequence[CoordinatorCommand] = COORDINATOR_COMMANDS,
    ) -> None:
        """Инициализирует экран раздела Orchestration со списком доступных команд координатора."""
        super().__init__(repo, command_runner=command_runner)
        self._entries = {entry.key: entry for entry in catalog}

    def menu_items(self) -> Sequence[tuple[str, str]]:
        """Возвращает список элементов меню команд координатора с группой, названием и строкой CLI."""
        return [
            (
                entry.key,
                f"[{entry.group}] {entry.title}\n  $ {entry.cli_line(self.repo)}",
            )
            for entry in self._entries.values()
        ]

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Обрабатывает выбор команды координатора из списка и открывает форму её параметров."""
        if event.item.name is None:
            return
        entry = self._entries[event.item.name]

        def on_form_closed(values: "dict[str, str] | None") -> None:
            """Коллбэк закрытия модальной формы параметров с запуском команды при подтверждении."""
            if values is not None:
                self._run(entry, values)

        self.app.push_screen(
            CoordinatorCommandFormScreen(entry, self.repo), on_form_closed
        )

    def _run(self, entry: CoordinatorCommand, values: Mapping[str, str]) -> None:
        """Формирует аргументы и запускает команду координатора через runner."""
        self.run_process(
            entry.cli_line(self.repo, values),
            process_argv(entry.cli_argv(self.repo, values)),
        )
