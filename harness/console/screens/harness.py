"""Раздел Harness: команды харнесса и обслуживания леджера из `harness.console.catalog.HARNESS_COMMANDS`,
доступные для запуска в текущем репозитории, каждая с отображением эквивалента CLI. Обратимые команды
без параметров запускаются немедленно; любые другие команды открывают модальный экран `CommandFormScreen`,
запрашивающий параметры, поясняющий причины подтверждения для необратимых действий и требующий ввода
контрольного слова (например, RESET). Отмена формы ничего не выполняет.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping, Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Input, ListView, Static

from .. import brand, i18n
from .. import data as console_data
from .. import reports as console_reports
from .. import stats as console_stats
from ..catalog import HARNESS_COMMANDS, CatalogEntry, process_argv
from ..logtext import colorize_overview
from ..runner import CommandRunner, capturing_runner
from ..shell import SHELL_CSS, ConsoleShell, WrappedValue
from .commands import (
    CommandForm,
    CommandMenuScreen,
    command_menu_css,
    describe_command,
)

_OTHER_GROUP = "Прочее"
_GROUPS = {
    **dict.fromkeys(
        (
            "init",
            "update",
            "update-force-managed",
            "update-force-seed",
            "adopt",
        ),
        "Установка и обновление",
    ),
    **dict.fromkeys(
        ("diff", "health", "health-fix", "health-online-fix", "verify"),
        "Проверки",
    ),
    **dict.fromkeys(("registry", "lock-project-skills", "list"), "Скиллы"),
    **dict.fromkeys(("repo-map", "parser-bundle"), "Repo Map"),
    **dict.fromkeys(
        (
            "cleanup",
            "cleanup-hard",
            "cleanup-apply",
            "cleanup-hard-apply",
            "uninstall",
            "uninstall-apply",
            "worktree-remove",
        ),
        "Очистка и удаление",
    ),
    **dict.fromkeys(("ledger-migrate", "ledger-clean", "ledger-reset"), "Ledger"),
}
_GROUP_ORDER = list(dict.fromkeys(_GROUPS.values())) + [_OTHER_GROUP]


def _group_rank(key: str) -> int:
    return _GROUP_ORDER.index(_GROUPS.get(key, _OTHER_GROUP))


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
            with VerticalScroll(id="command-form-body"):
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
            # Outside the scrolling body: Run and the error stay visible however long the form is.
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


class HarnessScreen(ConsoleShell, CommandMenuScreen):
    """Экран раздела Harness на общей оболочке консоли: меню доступных команд харнесса и
    обслуживания леджера."""

    CRUMB = "Harness"
    CRUMBS = "Главная › Harness"
    SECTION = "Harness"
    NARROW_FOOTER_KEYS = ("escape", "enter", "y")

    # The shell's rules plus the command-menu rules under this screen's own type name.
    CSS = (
        SHELL_CSS
        + command_menu_css("HarnessScreen")
        + brand.shell_css("HarnessScreen #harness-page { height: 1fr; padding: 0 2; }")
    )

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

    def compose_strip(self) -> ComposeResult:
        yield WrappedValue(str(self.repo))

    def compose_content(self) -> ComposeResult:
        with VerticalScroll(id="harness-page"):
            yield from self.compose_commands()

    def sync_language(self) -> None:
        ConsoleShell.sync_language(self)
        CommandMenuScreen.sync_language(self)

    def overview(self) -> ComposeResult:
        """Текущее состояние установки и сводка использования пайплайна."""
        state = console_stats.harness_state(self.repo)
        text = console_stats.harness_state_text(state, console_data.harness_version())
        text += "\n" + console_stats.usage_text(self._load_view(self.repo))
        panel = Static(
            colorize_overview(text),
            id="harness-state",
            classes="overview",
            markup=False,
        )
        panel.border_title = brand.section_title("Состояние")
        panel.i18n_source = text  # type: ignore[attr-defined]
        yield panel

    def menu_items(self) -> Sequence[tuple[str, str]]:
        """Команды харнесса по группам; необратимые отмечены ⚠."""
        ordered = sorted(
            self._entries.values(), key=lambda entry: _group_rank(entry.key)
        )
        return [
            (
                entry.key,
                ("⚠ " if entry.needs_confirmation else "  ")
                + i18n.tr(self._language, entry.title),
            )
            for entry in ordered
        ]

    def menu_group(self, key: str) -> str:
        return _GROUPS.get(key, _OTHER_GROUP)

    def menu_detail(self, key: str) -> Text:
        entry = self._entries[key]
        return describe_command(
            i18n.tr(self._language, entry.title),
            entry.reversibility,
            self.display_cli(entry.cli_line(self.repo)),
            entry.inputs,
            confirmation_word=entry.typed_confirmation,
            language=self._language,
        )

    def menu_cli_line(self, key: str) -> str:
        return self._entries[key].cli_line(self.repo)

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Обрабатывает выбор команды из списка, открывая форму параметров либо запуская команду сразу."""
        if event.item.name is None or event.list_view.id == self.IDS.menu:
            return
        self.open_command(event.item.name)

    def open_command(self, key: str) -> None:
        """Open a catalog command through its usual parameter and confirmation flow."""
        entry = self._entries.get(key)
        if entry is None:
            self.notify("Команда недоступна для выбранного проекта", severity="warning")
            return
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
