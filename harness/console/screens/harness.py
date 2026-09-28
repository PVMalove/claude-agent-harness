"""Раздел Harness: команды харнесса и обслуживания леджера из `harness.console.catalog.HARNESS_COMMANDS`,
доступные для запуска в текущем репозитории, каждая с отображением эквивалента CLI. Обратимые команды
без параметров запускаются немедленно; любые другие команды открывают модальный экран `CommandFormScreen`,
запрашивающий параметры, поясняющий причины подтверждения для необратимых действий и требующий ввода
контрольного слова (например, RESET). Отмена формы ничего не выполняет.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping, Sequence

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, ListView, Static

from .. import data as console_data
from .. import reports as console_reports
from .. import stats as console_stats
from ..catalog import HARNESS_COMMANDS, CatalogEntry, process_argv
from ..runner import CommandRunner, capturing_runner
from .commands import CommandForm, CommandMenuScreen


class CommandFormScreen(CommandForm):
    """Модальная форма параметров для команд раздела Harness."""

    def __init__(self, entry: CatalogEntry, repo: Path) -> None:
        """Инициализирует форму параметров команды каталога для репозитория."""
        super().__init__()
        self.entry = entry
        self.repo = repo

    def compose(self) -> ComposeResult:
        """Формирует структуру виджетов формы ввода параметров команды."""
        entry = self.entry
        with Vertical(id="command-form"):
            yield from self.form_header(
                entry.title, entry.cli_line(self.repo), entry.reversibility
            )
            for name in entry.inputs:
                yield Input(placeholder=name, id=f"input-{name}")
            if entry.typed_confirmation is not None:
                yield Input(
                    placeholder=f"Введите {entry.typed_confirmation}",
                    id="typed-confirmation",
                )
            yield from self.form_buttons(entry.reversibility)

    def _submit(self) -> None:
        """Проверяет заполнение обязательных полей и ввод проверочного слова подтверждения."""
        values = {
            name: self.query_one(f"#input-{name}", Input).value.strip()
            for name in self.entry.inputs
        }
        missing = [name for name, value in values.items() if not value]
        word = self.entry.typed_confirmation
        if missing:
            self._error(f"Заполните: {', '.join(missing)}")
        elif (
            word is not None
            and self.query_one("#typed-confirmation", Input).value != word
        ):
            self._error(f"Для запуска введите {word}")
        else:
            self.dismiss(values)


class HarnessScreen(CommandMenuScreen):
    """Экран раздела Harness с меню доступных команд харнесса и обслуживания леджера."""

    def __init__(
        self,
        repo: Path,
        *,
        command_runner: CommandRunner = capturing_runner,
        catalog: Sequence[CatalogEntry] = HARNESS_COMMANDS,
        load_view: Callable[
            [Path], console_reports.LedgerView
        ] = console_reports.load_ledger_view,
    ) -> None:
        """Инициализирует экран раздела Harness со списком доступных команд."""
        super().__init__(repo, command_runner=command_runner)
        self._load_view = load_view
        # A command whose script is absent here (e.g. verify outside the harness repository)
        # would only ever fail with "can't open file", so it is not offered.
        self._entries = {entry.key: entry for entry in catalog if entry.available(repo)}

    def overview(self) -> ComposeResult:
        """Текущее состояние установки и сводка использования пайплайна."""
        state = console_stats.harness_state(self.repo)
        text = console_stats.harness_state_text(state, console_data.harness_version())
        text += "\n" + console_stats.usage_text(self._load_view(self.repo))
        panel = Static(text, id="harness-state", classes="overview", markup=False)
        panel.border_title = "Состояние"
        yield panel

    def menu_items(self) -> Sequence[tuple[str, str]]:
        """Возвращает список элементов меню команд харнесса с описанием и строкой CLI."""
        return [
            (entry.key, f"{entry.title}\n  $ {entry.cli_line(self.repo)}")
            for entry in self._entries.values()
        ]

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Обрабатывает выбор команды из списка, открывая форму параметров либо запуская команду сразу."""
        if event.item.name is None:
            return
        entry = self._entries[event.item.name]
        if not entry.needs_confirmation and not entry.inputs:
            self._run(entry, {})
            return

        def on_form_closed(values: "dict[str, str] | None") -> None:
            """Коллбэк закрытия модальной формы параметров с запуском команды при подтверждении."""
            if values is not None:
                self._run(entry, values)

        self.app.push_screen(CommandFormScreen(entry, self.repo), on_form_closed)

    def _run(self, entry: CatalogEntry, values: Mapping[str, str]) -> None:
        """Формирует аргументы и запускает процесс выбранной команды каталога."""
        argv = process_argv(
            entry.cli_argv(self.repo, values),
            self.repo,
            dev_environment=entry.dev_environment,
        )
        self.run_process(entry.cli_line(self.repo, values), argv)
