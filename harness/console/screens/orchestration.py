"""Раздел Orchestration: команды координатора из `harness.console.coordinator_catalog.COORDINATOR_COMMANDS`,
сгруппированные по группам `group` (batch, decide, packet, dispatch, qa, risk, context-package, ledger).
Каждая команда открывает модальный экран `CoordinatorCommandFormScreen`, запрашивающий аргументы,
поясняющий причины подтверждения для терминальных действий и валидирующий обязательные поля
(и ограничения choices=) перед выполнением. Отмена формы ничего не выполняет.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping, Sequence

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    ListItem,
    ListView,
    Static,
    TextArea,
)

from .. import brand, i18n
from .. import reports as console_reports
from .. import stats as console_stats
from ..coordinator_catalog import COORDINATOR_COMMANDS, CoordinatorCommand, process_argv
from ..runner import CommandRunner, capturing_runner
from ..shell import SHELL_CSS, ConsoleShell, FieldGrid
from ..widgets import CheckBox
from .commands import CommandForm, CommandMenuScreen, describe_command

_GROUP_TITLES = {
    "batch": "Batch",
    "decide": "Решения",
    "packet": "Решения",
    "dispatch": "Dispatch",
    "qa": "QA",
    "risk": "Риск",
    "context-package": "Context Package",
    "ledger": "Ledger",
}
_GROUP_ORDER = list(dict.fromkeys(_GROUP_TITLES.values()))


def _group_title(group: str) -> str:
    return _GROUP_TITLES.get(group, group)


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
            with VerticalScroll(id="command-form-body"):
                yield from self.form_header(
                    entry.title, entry.cli_line(self.repo), entry.reversibility
                )
                for field in entry.fields:
                    if not field.takes_value:
                        yield CheckBox(field.flag, id=f"input-{field.dest}")
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
            # Outside the scrolling body: Run and the error stay visible however long the form is.
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


class OrchestrationScreen(ConsoleShell, CommandMenuScreen):
    """Экран раздела Orchestration на общей оболочке консоли: статистика пайплайна, история batch
    с фильтром по состоянию (выбор batch открывает его хронологию) и список команд координатора."""

    CRUMB = "Оркестрация"
    CRUMBS = "Главная › Оркестрация"
    SECTION = "Orchestration"

    # The shell's rules plus this screen's own. Textual matches a type selector along the first base
    # class only, so `CommandMenuScreen` rules never reach this screen: everything it needs is here.
    CSS = SHELL_CSS + brand.shell_css("""
    OrchestrationScreen #orchestration-page { height: 1fr; padding: 0 2; }
    OrchestrationScreen #orchestration-header { height: auto; margin-bottom: 1; }
    OrchestrationScreen #orchestration-title { width: auto; height: 1; text-style: bold; padding-right: 2; }
    OrchestrationScreen #orchestration-subtitle { width: 1fr; height: 1; color: $text-muted; }
    OrchestrationScreen .overview { height: auto; margin: 0 0 1 0; padding: 0 1; border: round $component-border; border-title-color: $primary; }
    OrchestrationScreen #pipeline-stats { layout: horizontal; }
    OrchestrationScreen #pipeline-stats .field-column { width: 1fr; padding-right: 2; }
    OrchestrationScreen #pipeline-history { height: 11; margin: 0 0 1 0; }
    OrchestrationScreen #state-filter { width: 32; height: 100%; border-title-color: $primary; }
    OrchestrationScreen #batch-history { width: 1fr; height: 100%; border-title-color: $primary; }
    OrchestrationScreen #command-body { height: 16; margin: 0 0 1 0; }
    OrchestrationScreen #command-menu { height: 1fr; margin: 0; border: round $component-border; border-title-color: $primary; }
    OrchestrationScreen #command-menu:focus { border: round $primary; }
    OrchestrationScreen.-wide #command-menu, OrchestrationScreen.-spacious #command-menu { width: 48; }
    OrchestrationScreen #command-detail-scroll { height: 1fr; padding: 0 1; border: round $component-border; border-title-color: $primary; }
    OrchestrationScreen #command-output-scroll { height: 8; margin: 0; padding: 0 1; border: round $component-border; border-title-color: $primary; }
    OrchestrationScreen #command-menu > ListItem { height: 1; padding: 0 1; }
    OrchestrationScreen #command-menu Static { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    OrchestrationScreen .group-title { color: $text-muted; text-style: bold; }
    OrchestrationScreen.-narrow #orchestration-subtitle { display: none; }
    OrchestrationScreen.-narrow #orchestration-title { width: 1fr; }
    OrchestrationScreen.-narrow #command-body { layout: vertical; height: 22; }
    OrchestrationScreen.-narrow #command-detail-scroll { height: 6; }
    """)

    def __init__(
        self,
        repo: Path,
        *,
        command_runner: CommandRunner = capturing_runner,
        catalog: Sequence[CoordinatorCommand] = COORDINATOR_COMMANDS,
        load_view: Callable[
            [Path], console_reports.LedgerView
        ] = console_reports.load_ledger_view,
    ) -> None:
        """Инициализирует экран раздела Orchestration со списком доступных команд координатора."""
        super().__init__(repo, command_runner=command_runner)
        self._entries = {entry.key: entry for entry in catalog}
        self._view = load_view(repo)

    def compose_strip(self) -> ComposeResult:
        yield Static(str(self.repo), classes="no-i18n", markup=False)

    def _sessions_label(self) -> str:
        active = sum(1 for stage in self._view.stages if not stage.finished)
        return f"Рабочие сессии · {active}"

    def compose_content(self) -> ComposeResult:
        with VerticalScroll(id="orchestration-page"):
            with Horizontal(id="orchestration-header"):
                yield Static("Оркестрация", id="orchestration-title")
                yield Static(
                    "Пайплайн batch, рабочие сессии и команды координатора.",
                    id="orchestration-subtitle",
                )
                yield Button(self._sessions_label(), id="worker-sessions", compact=True)
            yield from self.compose_commands()

    def sync_language(self) -> None:
        ConsoleShell.sync_language(self)
        CommandMenuScreen.sync_language(self)

    def _repaint_overview(self, language: str) -> None:
        """The unavailable-ledger message is plain text; the statistics grid relabels itself."""
        for panel in self.query(".overview"):
            source = getattr(panel, "i18n_source", None)
            if isinstance(panel, Static) and source:
                panel.update(i18n.tr_text(language, source))

    def overview(self) -> ComposeResult:
        """Статистика пайплайна и история batch: слева состояния с числом batch, справа batch
        выбранного состояния."""
        view = self._view
        panel: Widget
        if view.unavailable:
            panel = Static(
                view.unavailable, id="pipeline-stats", classes="overview", markup=False
            )
            panel.i18n_source = view.unavailable  # type: ignore[attr-defined]
        else:
            fields = console_stats.pipeline_stats_fields(
                console_stats.pipeline_stats(view)
            )
            panel = (
                FieldGrid(
                    fields,
                    translate_values=True,
                    id="pipeline-stats",
                    classes="field-grid overview",
                )
                if fields
                else Static(
                    console_stats.pipeline_stats_text(
                        console_stats.pipeline_stats(view)
                    ),
                    id="pipeline-stats",
                    classes="overview",
                    markup=False,
                )
            )
        panel.border_title = brand.frame_title("Состояние и статистика")
        yield panel
        if view.unavailable:
            return
        with Horizontal(id="pipeline-history"):
            states = ListView(
                *(
                    ListItem(Static(label, markup=False), name=key)
                    for key, label in console_stats.state_filter_items(view)
                ),
                id="state-filter",
            )
            states.border_title = brand.frame_title("Состояния")
            yield states
            history = ListView(id="batch-history")
            history.border_title = brand.frame_title("История пайплайна")
            yield history

    def on_mount(self) -> None:
        if not self._view.unavailable:
            self._show_batches(console_stats.ALL_STATES)
        # The command menu takes focus on arrival; the page still opens at its top.
        page = self.query_one("#orchestration-page", VerticalScroll)
        self.set_timer(0.05, lambda: page.scroll_home(animate=False))

    def _show_batches(self, state: str) -> None:
        history = self.query_one("#batch-history", ListView)
        history.clear()
        batches = console_stats.batches_in_state(self._view, state)
        if not batches:
            history.append(ListItem(Static("batch в этом состоянии нет", markup=False)))
            return
        for batch in batches:
            history.append(
                ListItem(
                    Static(console_stats.batch_label(batch), markup=False),
                    name=batch.batch_id,
                )
            )
        # Highlight the newest batch once the refreshed items are mounted, so Enter opens it.
        self.call_after_refresh(setattr, history, "index", 0)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if (
            event.list_view.id == "state-filter"
            and event.item is not None
            and event.item.name
        ):
            self._show_batches(event.item.name)

    def menu_items(self) -> Sequence[tuple[str, str]]:
        """Команды координатора по группам; необратимые отмечены ⚠."""
        ordered = sorted(
            self._entries.values(),
            key=lambda entry: _GROUP_ORDER.index(_group_title(entry.group)),
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
        return _group_title(self._entries[key].group)

    def menu_detail(self, key: str) -> Text:
        entry = self._entries[key]
        return describe_command(
            i18n.tr(self._language, entry.title),
            entry.reversibility,
            self.display_cli(entry.cli_line(self.repo)),
            [field.dest for field in entry.fields],
            language=self._language,
        )

    def menu_cli_line(self, key: str) -> str:
        return self._entries[key].cli_line(self.repo)

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Выбор состояния показывает его batch, выбор batch открывает хронологию, выбор команды
        открывает форму её параметров."""
        if event.item.name is None or event.list_view.id == self.IDS.menu:
            return
        if event.list_view.id == "state-filter":
            self._show_batches(event.item.name)
            self.query_one("#batch-history", ListView).focus()
            return
        if event.list_view.id == "batch-history":
            self._open_timeline(event.item.name)
            return
        entry = self._entries[event.item.name]

        def on_form_closed(values: "dict[str, str] | None") -> None:
            """Коллбэк закрытия модальной формы параметров с запуском команды при подтверждении."""
            if values is not None:
                self._run(entry, values)

        self.app.push_screen(
            CoordinatorCommandFormScreen(entry, self.repo), on_form_closed
        )

    def _open_timeline(self, batch_id: str) -> None:
        from .reports import BatchTimelineScreen

        timeline = self._view.timeline(batch_id)
        if timeline is not None:
            self.app.push_screen(BatchTimelineScreen(timeline, self.repo))

    @on(Button.Pressed, "#worker-sessions")
    def open_worker_sessions(self, event: Button.Pressed) -> None:
        from .worker_observation import WorkerObservationScreen

        event.stop()
        self.app.push_screen(WorkerObservationScreen(self.repo))

    def _run(self, entry: CoordinatorCommand, values: Mapping[str, str]) -> None:
        """Формирует аргументы и запускает команду координатора через runner."""
        if entry.key == "dispatch-send" and "--visibility=background" not in values.get(
            "adapter_arg", ""
        ):
            from .worker_observation import WorkerObservationScreen

            screen = WorkerObservationScreen(self.repo, values.get("dispatch"))
            self.app.push_screen(screen)
            self.app.call_after_refresh(
                screen.launch,
                process_argv(entry.cli_argv(self.repo, values)),
                self._command_runner,
            )
            return
        self.run_process(
            entry.cli_line(self.repo, values),
            process_argv(entry.cli_argv(self.repo, values)),
        )
