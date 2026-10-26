"""Общие компоненты для разделов Harness и Orchestration: меню команд, отображающее эквивалент CLI
и запускающее выбранную команду в рабочем потоке (`CommandMenuScreen`), а также модальная форма
для ввода аргументов, поясняющая причину подтверждения для необратимых действий (`CommandForm`).
Отмена формы ничего не выполняет.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from subprocess import CompletedProcess
from typing import Callable, Sequence, TypeVar

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
from textual.widgets import Button, Footer, ListItem, ListView, Static

from ..chrome import TopBar
from ..translate import Translated
from ..logtext import colorize_log, colorize_overview
from ..catalog import CONFIRMATION_REASONS, Reversibility
from ..runner import CommandRunner, HarnessRuns
from .. import brand, i18n

_OUTPUT_TAIL_LINES = 200


def command_menu_css(screen: str) -> str:
    """The rules of a command-menu section for the screen type `screen`. A type selector reaches
    only the first base class of a screen, so a shell screen that mixes in `CommandMenuScreen` loads
    these rules under its own type name, after the shell's."""
    return brand.shell_css(f"""
    {screen} .overview {{ height: auto; margin: 0 0 1 0; padding: 0 1; border: round $component-border; border-title-color: $primary; }}
    {screen} #command-body {{ height: 16; margin: 0 0 1 0; }}
    {screen} #command-menu {{ height: 1fr; margin: 0; border: round $component-border; border-title-color: $primary; }}
    {screen} #command-menu:focus {{ border: round $primary; }}
    {screen}.-wide #command-menu, {screen}.-spacious #command-menu {{ width: 48; }}
    {screen} #command-detail-scroll {{ height: 1fr; padding: 0 1; border: round $component-border; border-title-color: $primary; }}
    {screen} #command-output-scroll {{ height: 8; margin: 0; padding: 0 1; border: round $component-border; border-title-color: $primary; }}
    {screen} #command-menu > ListItem {{ height: 1; padding: 0 1; }}
    {screen} #command-menu Static {{ height: 1; text-wrap: nowrap; text-overflow: ellipsis; }}
    {screen} .group-title {{ color: $text-muted; text-style: bold; }}
    {screen}.-narrow #command-body {{ layout: vertical; height: 22; }}
    {screen}.-narrow #command-detail-scroll {{ height: 6; }}
    """)


_Result = TypeVar("_Result")


def call_from_worker(screen: Widget, callback: Callable[[], object]) -> None:
    """Передаёт результат фонового потока в UI, если экран (или виджет) и приложение ещё работают."""
    if not (screen.is_mounted and screen.app.is_running):
        return
    try:
        screen.app.call_from_thread(callback)
    except Exception:
        # The screen or the app can close between the check above and the call (RuntimeError from
        # a stopped loop, NoMatches from an unmounted widget): the result has no receiver then.
        pass


def harness_runs(widget: Widget) -> HarnessRuns:
    """The Harness session's admission belongs to the app, not to a replaceable screen."""
    state = getattr(widget.app, "_harness_runs", None)
    if not isinstance(state, HarnessRuns):
        state = HarnessRuns()
        setattr(widget.app, "_harness_runs", state)
    return state


def short_repo(cli_line: str, repo: Path) -> str:
    """`cli_line` with the project path replaced by `$REPO`, quoted or not (a Windows path is quoted)."""
    return cli_line.replace(shlex.quote(str(repo)), "$REPO").replace(str(repo), "$REPO")


def render_result(
    cli_line: str, result: "CompletedProcess[str]", *, full: bool = False
) -> str:
    """Формирует текстовый результат выполнения команды: строку вызова, код выхода и последние строки вывода."""
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    lines = output.rstrip().splitlines()
    if not full:
        lines = lines[-_OUTPUT_TAIL_LINES:]
    label = "exit" if full else "код выхода:"
    return "\n".join([f"$ {cli_line}", f"{label} {result.returncode}", *lines])


def describe_command(
    title: str,
    reversibility: Reversibility,
    cli_line: str,
    params: Sequence[str],
    *,
    confirmation_word: str | None = None,
    language: str = "ru",
) -> Text:
    """Панель деталей команды: название, обратимость, строка CLI и параметры."""
    warn = brand.STATUS_COLORS["warn"]
    muted = brand.PALETTE["muted"]

    def t(line: str) -> str:
        return i18n.tr_text(language, line)

    text = Text(title + "\n", style="bold")
    if reversibility is Reversibility.REVERSIBLE:
        text.append(t("Обратимо: да") + "\n", style=brand.STATUS_COLORS["ok"])
    else:
        text.append(f"⚠ {t(CONFIRMATION_REASONS[reversibility])}\n", style=warn)
        if confirmation_word:
            text.append(f"{t('Потребуется ввести')} {confirmation_word}\n", style=warn)
    text.append(f"\n$ {cli_line}\n", style=brand.PALETTE["foreground"])
    if params:
        text.append("\n" + t("Параметры:") + " " + ", ".join(params) + "\n")
    text.append("\n" + t("Enter — запустить · y — копировать CLI"), style=muted)
    return text


class CommandForm(Translated, ModalScreen["dict[str, str] | None"]):
    """Модальная форма параметров команды. Закрывается со словарём введённых значений или с None при отмене."""

    BINDINGS = [Binding("escape", "cancel", "Отмена")]
    DEFAULT_CSS = """
    CommandForm { align: center middle; }
    CommandForm #command-form {
        width: 80%; max-width: 100; height: auto; max-height: 80%;
        padding: 0 1; border: round $primary; border-title-color: $primary;
    }
    CommandForm #command-form-body { height: auto; max-height: 70vh; }
    CommandForm #command-form Static { height: auto; }
    CommandForm #form-error { color: $error; }
    CommandForm #command-form Horizontal { height: auto; }
    """

    def form_header(
        self,
        title: str,
        cli_line: str,
        reversibility: Reversibility,
        *,
        warning_cli: bool = False,
    ) -> ComposeResult:
        """Генерирует заголовок формы, эквивалент команды CLI и причину подтверждения при наличии."""
        language = getattr(self.app, "language", "ru")
        yield Static(Text(i18n.tr(language, title), style="bold"), markup=False)
        yield Static(
            Text(
                f"$ {cli_line}",
                style=brand.PALETTE["warning"]
                if warning_cli
                else f"bold {brand.PALETTE['secondary']}",
            ),
            id="form-cli",
            markup=False,
        )
        if reversibility is not Reversibility.REVERSIBLE:
            yield Static(
                Text(
                    i18n.tr_text(language, CONFIRMATION_REASONS[reversibility]),
                    style=brand.STATUS_COLORS["warn"],
                ),
                id="confirmation-reason",
                markup=False,
            )

    def form_buttons(self, reversibility: Reversibility) -> ComposeResult:
        """Генерирует кнопки запуска и отмены, а также блок вывода ошибок валидации формы."""
        yield Static("", id="form-error", markup=False)
        with Horizontal():
            yield Button(
                "Запустить",
                id="run",
                variant="primary"
                if reversibility is Reversibility.REVERSIBLE
                else "error",
            )
            yield Button("Отмена", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#command-form").border_title = brand.frame_title(
            "Запуск команды"
        )

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


class CommandMenuScreen(Translated, Screen[None]):
    """Базовый экран меню команд: список `#command-menu` с группами, панель деталей `#command-detail`
    выбранной команды и закреплённая область вывода `#command-output`."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Назад"),
        Binding("y", "copy_cli", "Копировать CLI"),
    ]
    # The command menu owns the keyboard on arrival even when a section adds an overview above it.
    AUTO_FOCUS = "#command-menu"
    CRUMB = "Команды"
    CANCEL_OWN_WORKERS_ONLY = False
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (100, "-wide")]
    DEFAULT_CSS = """
    CommandMenuScreen .overview {
        height: auto; margin: 0 1; padding: 0 1;
        border: round $primary 50%; border-title-color: $primary;
    }
    CommandMenuScreen #command-body { height: 1fr; min-height: 8; margin: 0 1; }
    CommandMenuScreen #command-menu { height: 1fr; margin: 0; border-title-color: $primary; }
    CommandMenuScreen.-wide #command-menu, CommandMenuScreen.-spacious #command-menu { width: 48; }
    CommandMenuScreen #command-detail-scroll {
        height: 1fr; padding: 0 1; border: round $panel-lighten-2; border-title-color: $primary;
    }
    CommandMenuScreen.-narrow #command-body { layout: vertical; }
    CommandMenuScreen.-narrow #command-detail-scroll { height: 6; }
    CommandMenuScreen #command-output-scroll {
        height: 8; margin: 0 1; padding: 0 1;
        border: round $panel-lighten-2; border-title-color: $primary;
    }
    CommandMenuScreen #command-menu > ListItem { height: 1; padding: 0 1; }
    CommandMenuScreen #command-menu Static {
        height: 1; text-wrap: nowrap; text-overflow: ellipsis;
    }
    CommandMenuScreen .group-title { color: $text-muted; text-style: bold; }
    """

    def __init__(self, repo: Path, *, command_runner: CommandRunner) -> None:
        """Инициализирует экран меню команд для указанного репозитория и исполнителя команд."""
        super().__init__()
        self.repo = repo
        self._command_runner = command_runner
        self._language = "ru"
        self._headings: dict[str, str] = {}

    def menu_items(self) -> Sequence[tuple[str, str]]:
        """Возвращает последовательность кортежей (ключ, метка) для элементов меню команд."""
        raise NotImplementedError

    def menu_group(self, key: str) -> str:
        """Заголовок группы команды; пустая строка — без группировки."""
        return ""

    def menu_detail(self, key: str) -> Text:
        """Описание команды для панели деталей."""
        raise NotImplementedError

    def menu_cli_line(self, key: str) -> str:
        """Полная строка CLI команды для копирования."""
        raise NotImplementedError

    def display_cli(self, cli_line: str) -> str:
        """Строка CLI для показа: путь проекта заменён на `$REPO`."""
        return short_repo(cli_line, self.repo)

    def overview(self) -> ComposeResult:
        """Виджеты над меню команд: текущее состояние и статистика раздела (по умолчанию нет)."""
        yield from ()

    @staticmethod
    def _label_text(label: str) -> Text:
        text = Text(label)
        if label.startswith("⚠"):
            text.stylize(brand.STATUS_COLORS["warn"], 0, 1)
        return text

    def _menu_children(self) -> list[ListItem]:
        children: list[ListItem] = []
        group = ""
        self._headings = {}
        for key, label in self.menu_items():
            heading = self.menu_group(key)
            if heading and heading != group:
                group = heading
                heading_id = f"group-{len(self._headings)}"
                self._headings[heading_id] = heading
                children.append(
                    ListItem(
                        Static(
                            i18n.group_title(self._language, heading).upper(),
                            classes="group-title",
                            markup=False,
                        ),
                        id=heading_id,
                        disabled=True,
                    )
                )
            children.append(ListItem(Static(self._label_text(label)), name=key))
        return children

    def compose(self) -> ComposeResult:
        """Формирует структуру виджетов экрана меню команд."""
        yield TopBar(f"Главная › {self.CRUMB}")
        yield from self.compose_commands()
        yield Footer()

    def compose_commands(self) -> ComposeResult:
        """Содержимое экрана без рамки: обзор, меню команд с деталями и область вывода. Его же
        размещает общая оболочка консоли."""
        yield from self.overview()
        self._language = getattr(self.app, "language", "ru")
        children = self._menu_children()
        first = next((i for i, item in enumerate(children) if item.name), 0)
        with Horizontal(id="command-body"):
            menu = ListView(*children, id="command-menu", initial_index=first)
            menu.border_title = brand.section_title(i18n.ui(self._language, "commands"))
            yield menu
            with VerticalScroll(id="command-detail-scroll") as detail:
                detail.border_title = brand.section_title("Описание")
                yield Static("", id="command-detail")
        output = VerticalScroll(
            Static("", id="command-output", markup=False), id="command-output-scroll"
        )
        output.border_title = brand.section_title("Вывод")
        yield output

    def sync_language(self) -> None:
        """Re-label the group headings after the console language changed."""
        language = getattr(self.app, "language", "ru")
        self._repaint_overview(language)
        if language == self._language:
            return
        self._language = language
        menu = self.query_one("#command-menu", ListView)
        menu.border_title = brand.section_title(i18n.ui(language, "commands"))
        for heading_id, heading in self._headings.items():
            self.query_one(f"#{heading_id} Static", Static).update(
                i18n.group_title(language, heading).upper()
            )
        for key, label in self.menu_items():
            for entry in menu.query(ListItem):
                if entry.name == key:
                    entry.query_one(Static).update(self._label_text(label))
        item = menu.highlighted_child
        if item is not None and item.name:
            self.query_one("#command-detail", Static).update(
                self.menu_detail(item.name)
            )

    def _repaint_overview(self, language: str) -> None:
        """The state panels above the menu are built from Russian text: show it translated."""
        for panel in self.query(".overview"):
            source = getattr(panel, "i18n_source", None)
            if isinstance(panel, Static) and source:
                panel.update(colorize_overview(i18n.tr_text(language, source)))

    def on_screen_resume(self) -> None:
        self.sync_language()

    @on(ListView.Highlighted, "#command-menu")
    def _show_detail(self, event: ListView.Highlighted) -> None:
        if event.item is not None and event.item.name:
            self.query_one("#command-detail", Static).update(
                self.menu_detail(event.item.name)
            )

    def action_copy_cli(self) -> None:
        """Копирует полную строку CLI выбранной команды в буфер обмена."""
        item = self.query_one("#command-menu", ListView).highlighted_child
        if item is None or not item.name:
            return
        self.app.copy_to_clipboard(self.menu_cli_line(item.name))
        self.notify("Команда скопирована")

    def on_unmount(self) -> None:
        """Отменяет фоновые воркеры при размонтировании экрана."""
        if self.CANCEL_OWN_WORKERS_ONLY:
            self.workers.cancel_node(self)
        else:
            self.workers.cancel_all()

    def run_process(self, cli_line: str, argv: list[str]) -> None:
        """Запускает процесс команды в фоновом потоке без блокировки интерфейса TUI."""
        self._show(f"$ {cli_line}\nвыполняется…")

        def work() -> None:
            """Фоновая задача выполнения процесса и передачи результатов в интерфейс."""
            try:
                result = self._command_runner(argv, cwd=self.repo)
            except OSError as exc:
                text = f"$ {cli_line}\nне удалось запустить: {exc}"
            except Exception as exc:
                # An unhandled worker error would exit the whole TUI; show it in the output instead.
                text = f"$ {cli_line}\nсбой запуска: {exc!r}"
            else:
                text = render_result(cli_line, result)
            call_from_worker(self, lambda: self._show(text))

        self.run_worker(work, thread=True, exclusive=True, group="command")

    def _show(self, text: str) -> None:
        """Обновляет содержимое текстовой панели вывода команды."""
        self.query_one("#command-output", Static).update(colorize_log(text))
