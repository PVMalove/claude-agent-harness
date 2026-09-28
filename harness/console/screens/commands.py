"""Общие компоненты для разделов Harness и Orchestration: меню команд, отображающее эквивалент CLI
и запускающее выбранную команду в рабочем потоке (`CommandMenuScreen`), а также модальная форма
для ввода аргументов, поясняющая причину подтверждения для необратимых действий (`CommandForm`).
Отмена формы ничего не выполняет.
"""

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
from .. import brand

_OUTPUT_TAIL_LINES = 200


def render_result(cli_line: str, result: "CompletedProcess[str]") -> str:
    """Формирует текстовый результат выполнения команды: строку вызова, код выхода и последние строки вывода."""
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    lines = output.rstrip().splitlines()[-_OUTPUT_TAIL_LINES:]
    return "\n".join([f"$ {cli_line}", f"код выхода: {result.returncode}", *lines])


class CommandForm(ModalScreen["dict[str, str] | None"]):
    """Модальная форма параметров команды. Закрывается со словарём введённых значений или с None при отмене."""

    BINDINGS = [Binding("escape", "cancel", "Отмена")]

    def form_header(
        self, title: str, cli_line: str, reversibility: Reversibility
    ) -> ComposeResult:
        """Генерирует заголовок формы, эквивалент команды CLI и причину подтверждения при наличии."""
        yield Static(title)
        yield Static(f"$ {cli_line}", id="form-cli", markup=False)
        if reversibility is not Reversibility.REVERSIBLE:
            yield Static(CONFIRMATION_REASONS[reversibility], id="confirmation-reason")

    def form_buttons(self, reversibility: Reversibility) -> ComposeResult:
        """Генерирует кнопки запуска и отмены, а также блок вывода ошибок валидации формы."""
        yield Static("", id="form-error")
        with Horizontal():
            yield Button(
                "Выполнить",
                id="run",
                variant="primary"
                if reversibility is Reversibility.REVERSIBLE
                else "error",
            )
            yield Button("Отмена", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Обрабатывает нажатие кнопок формы («Выполнить» или «Отмена»)."""
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "run":
            self._submit()

    def action_cancel(self) -> None:
        """Обрабатывает действие отмены формы по клавише Escape."""
        self.dismiss(None)

    def _submit(self) -> None:
        """Считывает и проверяет введённые поля формы перед закрытием с результатом."""
        raise NotImplementedError

    def _error(self, message: str) -> None:
        """Отображает сообщение об ошибке валидации на форме."""
        self.query_one("#form-error", Static).update(message)


class CommandMenuScreen(Screen[None]):
    """Базовый экран меню команд со списком `#command-menu` и областью вывода `#command-output`."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Назад")]
    # The command menu owns the keyboard on arrival even when a section adds an overview above it.
    AUTO_FOCUS = "#command-menu"
    DEFAULT_CSS = """
    CommandMenuScreen .overview {
        height: auto; margin: 0 1; padding: 0 1;
        border: round $primary 50%; border-title-color: $primary;
    }
    CommandMenuScreen #command-menu {
        height: 1fr; min-height: 6; margin: 0 1; border-title-color: $primary;
    }
    CommandMenuScreen #command-output-scroll {
        height: auto; max-height: 40%; margin: 0 1; padding: 0 1;
        border: round $panel-lighten-2; border-title-color: $primary;
    }
    """

    def __init__(self, repo: Path, *, command_runner: CommandRunner) -> None:
        """Инициализирует экран меню команд для указанного репозитория и исполнителя команд."""
        super().__init__()
        self.repo = repo
        self._command_runner = command_runner

    def menu_items(self) -> Sequence[tuple[str, str]]:
        """Возвращает последовательность кортежей (ключ, метка) для элементов меню команд."""
        raise NotImplementedError

    def overview(self) -> ComposeResult:
        """Виджеты над меню команд: текущее состояние и статистика раздела (по умолчанию нет)."""
        yield from ()

    def compose(self) -> ComposeResult:
        """Формирует структуру виджетов экрана меню команд."""
        yield Header(icon=brand.MENU_ICON)
        yield from self.overview()
        menu = ListView(
            *(
                ListItem(Static(label, markup=False), name=key)
                for key, label in self.menu_items()
            ),
            id="command-menu",
        )
        menu.border_title = "Команды"
        yield menu
        output = VerticalScroll(
            Static("", id="command-output", markup=False), id="command-output-scroll"
        )
        output.border_title = "Вывод"
        yield output
        yield Footer()

    def run_process(self, cli_line: str, argv: list[str]) -> None:
        """Запускает процесс команды в фоновом потоке без блокировки интерфейса TUI."""
        self._show(f"$ {cli_line}\nвыполняется…")

        def work() -> None:
            """Фоновая задача выполнения процесса и передачи результатов в интерфейс."""
            try:
                result = self._command_runner(argv, cwd=self.repo)
            except OSError as exc:
                text = f"$ {cli_line}\nне удалось запустить: {exc}"
            else:
                text = render_result(cli_line, result)
            self.app.call_from_thread(self._show, text)

        self.run_worker(work, thread=True, exclusive=True, group="command")

    def _show(self, text: str) -> None:
        """Обновляет содержимое текстовой панели вывода команды."""
        self.query_one("#command-output", Static).update(text)
