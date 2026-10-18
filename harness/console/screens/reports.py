"""Reports section: current stages and completion reports from the selected ledger generation,
with manual refresh, combined filters and ordering; one report read section by section; and a
batch chronology - states, dispatches by role, coordinator decisions and risks; QA gate logs (verdict
and tail) with failed qa-lane attempts. A report or a chronology exports to Markdown (`e`) through
the shared screens/export.py action. All facts come from
harness.console.reports; these screens only render them. Report text is untrusted role output, so
every widget renders it with `markup=False`."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Input, ListItem, ListView, Select, Static

from ..chrome import TopBar
from ..shell import SHELL_CSS, ConsoleShell, Fold, SectionCard
from ..status import Status
from ..translate import Translated, translate_bindings
from ..widgets import OpenableList
from .. import reports as console_reports
from ..reports import BatchTimeline, LedgerView, ReportEntry, StageEntry
from .. import brand
from .. import i18n
from ..pr_timings import load_reports_view
from .export import EXPORT_BINDING_KEY, export_document
from .commands import call_from_worker

_FILTERS = (
    ("filter-ticket", "тикет", "ticket"),
    ("filter-role", "роль", "role"),
    ("filter-outcome", "outcome", "outcome"),
    ("filter-date", "дата (2026-09-27)", "date"),
    ("filter-state", "статус стадии", "state"),
)

_SELECT_OPTIONS = {
    "filter-completion": [
        ("Все стадии", "all"),
        ("Незавершённые", "open"),
        ("Завершённые", "finished"),
    ],
    "sort-field": [
        ("По времени", "time"),
        ("По тикету", "ticket"),
        ("По роли", "role"),
        ("По статусу", "state"),
    ],
    "sort-direction": [("По убыванию", "desc"), ("По возрастанию", "asc")],
}
_REFRESH_LABELS = ("r Обновить", "↻")
_QA_LABELS = ("F3 QA-логи", "QA")


def _short_id(identifier: str, prefix: str) -> str:
    return identifier.removeprefix(prefix)[:8]


def _report_label(
    entry: StageEntry, language: str = "ru", narrow: bool = False
) -> Text:
    """One row: date, ticket, role badge and short id, then the stage status (symbol and label).
    A `Text`, never markup: the fields come from untrusted role output. The row wraps, never cuts."""
    at = console_reports.timestamp(entry.at)
    date = at.strftime("%m-%d %H:%M") if at else "?"
    colour = brand.ROLE_COLORS.get(entry.role, brand.PALETTE["muted"])
    label = Text(f"{date:<11} {entry.ticket:<6} ")
    label.append(f"[{entry.role}]", style=f"bold {colour}")
    label.append(f" {_short_id(entry.dispatch_id, 'dispatch-')}\n")
    status = console_reports.stage_status(entry, narrow=narrow)
    label.append(status.render(language), style=status.color)
    return label


def _batch_label(batch: console_reports.BatchSummary, language: str = "ru") -> Text:
    """One batch row: start, ticket and short id, then the batch status and the time to PR."""
    status = console_reports.batch_status(batch)
    start = i18n.tr_text(language, console_reports.batch_start_text(batch))
    label = Text(f"{start} · {batch.ticket} {_short_id(batch.batch_id, 'batch-')}\n")
    label.append(status.render(language), style=status.color)
    label.append(
        f" · {console_reports.batch_timing_text(batch, language=language)}",
        style=brand.PALETTE["muted"],
    )
    return label


def _section_id(title: str) -> str:
    return "section-" + title.lower().replace(" ", "-")


async def _replace_items(listing: ListView, items: list[ListItem]) -> None:
    """Rebuild a list while keeping its selected record when it remains available."""
    selected = listing.highlighted_child
    selected_id = selected.name if selected else None
    await listing.clear()
    await listing.extend(items)
    listing.index = next(
        (index for index, item in enumerate(items) if item.name == selected_id),
        0 if items else None,
    )


class ReportsScreen(ConsoleShell):
    """Current stages and reports with filters, ordering, refresh and batch chronology."""

    CRUMBS = "Главная › Отчёты"
    SECTION = "Reports"
    NARROW_FOOTER_KEYS = ("escape", "enter", "f3", "f2")
    AUTO_FOCUS = "#report-list"
    CSS = SHELL_CSS + brand.shell_css("""
    ReportsScreen #reports-page { height: 1fr; padding: 0 2; }
    ReportsScreen #report-header { height: auto; margin-bottom: 1; }
    ReportsScreen #report-heading { width: 1fr; height: auto; }
    ReportsScreen #reports-title { height: 1; text-style: bold; }
    ReportsScreen #reports-subtitle { height: 1; color: $text-muted; }
    ReportsScreen #reports-status { width: auto; height: 1; margin-right: 2; }
    ReportsScreen #report-toolbar { width: auto; height: 1; }
    ReportsScreen #report-toolbar Button { margin-left: 1; }
    ReportsScreen #report-filter-fold { margin-bottom: 1; }
    ReportsScreen.-wide #report-filter-fold > CollapsibleTitle, ReportsScreen.-spacious #report-filter-fold > CollapsibleTitle { display: none; }
    ReportsScreen.-wide #report-filter-fold > Contents, ReportsScreen.-spacious #report-filter-fold > Contents { padding: 0; }
    ReportsScreen #report-filters { height: 1; }
    ReportsScreen #report-filters Input { width: 1fr; height: 1; margin-right: 1; border: none; padding: 0 1; background: $background; }
    ReportsScreen #report-filters Input:focus { border: none; background: $row-highlight; }
    ReportsScreen #report-order { height: 1; margin-top: 1; }
    ReportsScreen #report-order Select { width: 1fr; margin-right: 1; }
    ReportsScreen #report-order Select > SelectCurrent { border: none; height: 1; padding: 0 1; background: $background; }
    ReportsScreen #reports-body { height: 1fr; min-height: 10; }
    ReportsScreen #report-panel { width: 1fr; height: 100%; margin-bottom: 1; }
    ReportsScreen .card-hint { height: 1; color: $text-muted; }
    ReportsScreen .report-list { height: 1fr; margin: 0; padding: 0; border: none; background: transparent; }
    ReportsScreen .report-list:focus { border: none; }
    ReportsScreen .report-list > ListItem { height: auto; padding: 0 1; border-left: blank; }
    ReportsScreen .report-list > ListItem Static { height: auto; }
    ReportsScreen .report-list > ListItem.-highlight { background: $row-highlight; color: $foreground; text-style: none; }
    ReportsScreen .report-list:focus > ListItem.-highlight { background: $selection-background; color: $selection-foreground; border-left: outer $primary; text-style: none; }
    ReportsScreen #batch-list { height: 8; }
    ReportsScreen #reports-empty { height: auto; color: $text-muted; }
    ReportsScreen.-narrow #report-header { layout: vertical; }
    ReportsScreen.-narrow #reports-subtitle { display: none; }
    ReportsScreen.-narrow #report-toolbar { margin-top: 1; }
    ReportsScreen.-narrow #report-toolbar Button:first-child { margin-left: 0; }
    ReportsScreen.-narrow .card-hint { display: none; }
    ReportsScreen.-narrow #report-filters { layout: vertical; height: auto; }
    ReportsScreen.-narrow #report-filters Input { width: 100%; margin-right: 0; }
    ReportsScreen.-narrow #report-order { layout: vertical; height: auto; }
    ReportsScreen.-narrow #report-order Select { width: 100%; margin-right: 0; }
    ReportsScreen.-narrow #reports-body { height: auto; }
    ReportsScreen.-narrow #report-panel { height: auto; }
    ReportsScreen.-narrow #report-list { height: 12; }
    ReportsScreen.-narrow #batch-list { height: 6; }
    """)

    def __init__(
        self,
        repo: Path,
        *,
        load_view: Callable[[Path], LedgerView] | None = None,
    ) -> None:
        super().__init__()
        self.repo = repo
        self._load_view = load_view or load_reports_view
        self.view = (load_view or console_reports.load_ledger_view)(repo)
        self._initial_timings = load_view is None
        self.shown: list[StageEntry] = list(self.view.stages)
        self.refreshing = False
        self.refresh_error = ""
        self._narrow = False

    BINDINGS = [
        Binding("r", "refresh", "Обновить"),
        Binding("f3", "open_qa_logs", "QA-логи", key_display="F3"),
    ]

    def on_mount(self) -> None:
        self._narrow = self.app.size.width < brand.NARROW_WIDTH
        self._apply_layout()
        self.query_one("#reports-empty", Static).display = not self.shown or bool(
            self.view.unavailable
        )
        if self._initial_timings and any(batch.branch for batch in self.view.batches):
            self.action_refresh()

    def compose_strip(self) -> ComposeResult:
        yield Static(f"Проект: {self.repo.name}", id="reports-project", markup=False)

    def compose_content(self) -> ComposeResult:
        narrow = self.app.size.width < brand.NARROW_WIDTH
        with VerticalScroll(id="reports-page"):
            with Horizontal(id="report-header"):
                with Vertical(id="report-heading"):
                    yield Static("Отчёты", id="reports-title")
                    yield Static(
                        "Стадии, completion reports и хронология batch из ledger.",
                        id="reports-subtitle",
                    )
                yield Static(
                    self._status(),
                    id="reports-status",
                    classes="no-i18n",
                    markup=False,
                )
                with Horizontal(id="report-toolbar"):
                    yield Button(
                        _REFRESH_LABELS[narrow], id="refresh-reports", compact=True
                    )
                    yield Button(_QA_LABELS[narrow], id="open-qa-logs", compact=True)
            with Fold(
                title="Фильтры",
                summary=self._filters_summary(),
                collapsed=narrow,
                id="report-filter-fold",
            ):
                with Horizontal(id="report-filters"):
                    for widget_id, placeholder, _ in _FILTERS:
                        yield Input(placeholder=placeholder, id=widget_id, compact=True)
                with Horizontal(id="report-order"):
                    yield Select(
                        _SELECT_OPTIONS["filter-completion"],
                        value="all",
                        allow_blank=False,
                        id="filter-completion",
                        compact=True,
                    )
                    yield Select(
                        _SELECT_OPTIONS["sort-field"],
                        value="time",
                        allow_blank=False,
                        id="sort-field",
                        compact=True,
                    )
                    yield Select(
                        _SELECT_OPTIONS["sort-direction"],
                        value="desc",
                        allow_blank=False,
                        id="sort-direction",
                        compact=True,
                    )
            with Horizontal(id="reports-body"):
                with SectionCard(
                    title="Стадии и отчёты",
                    note=str(len(self.shown)),
                    id="report-panel",
                ):
                    yield Static(
                        "Enter — открыть отчёт", classes="card-hint", id="report-hint"
                    )
                    yield OpenableList(
                        *self._report_items(), id="report-list", classes="report-list"
                    )
                    yield Static(
                        self._empty_text(),
                        id="reports-empty",
                        classes="no-i18n",
                        markup=False,
                    )
            with Fold(
                title="Батчи",
                summary=self._batches_summary(),
                collapsed=narrow,
                id="batch-fold",
            ):
                yield Static("Enter — хронология", classes="card-hint", id="batch-hint")
                yield OpenableList(
                    *self._batch_items(), id="batch-list", classes="report-list"
                )

    def _filters_summary(self) -> str:
        """The number of filters in use, shown beside the folded filter card's title."""
        if not self.is_mounted:
            return ""
        used = sum(bool(self.query_one(f"#{i}", Input).value) for i, _, _ in _FILTERS)
        used += self.query_one("#filter-completion", Select).value != "all"
        return str(used) if used else ""

    def _batches_summary(self) -> str:
        return f"хронология · {len(self.view.batches)}"

    def _empty_text(self) -> Text:
        """The line under an empty list: why the ledger is unavailable, or that nothing matched."""
        if self.view.unavailable:
            return Text(
                f"{brand.STATUS_ICONS['fail']} {self.view.unavailable}",
                style=brand.STATUS_COLORS["fail"],
            )
        return Text(
            f"{brand.STATUS_ICONS['skipped']} "
            + i18n.tr(self._language(), "Ничего не найдено")
        )

    def _status(self) -> Text | str:
        language = self._language()
        if self.refresh_error:
            return Text(
                f"{brand.STATUS_ICONS['fail']} "
                + i18n.tr_text(language, self.refresh_error),
                style=brand.STATUS_COLORS["fail"],
            )
        if self.view.unavailable:
            return Text(
                f"{brand.STATUS_ICONS['fail']} {self.view.unavailable}",
                style=brand.STATUS_COLORS["fail"],
            )
        if self.refreshing:
            running = Status("running", "Обновление…", "Refreshing…")
            return Text(running.render(language), style=running.color)
        shown, total = len(self.shown), len(self.view.reports)
        if any(entry.report is None for entry in self.view.stages):
            stages = len(self.view.stages)
            if language == "en":
                return f"stages: {shown} of {stages} · reports: {total}"
            return f"стадий: {shown} из {stages} · отчётов: {total}"
        return (
            f"reports: {shown} of {total}"
            if language == "en"
            else f"отчётов: {shown} из {total}"
        )

    def _language(self) -> str:
        return getattr(self.app, "language", "ru")

    def _relabel_buttons(self) -> None:
        language = self._language()
        for identifier, labels in (
            ("refresh-reports", _REFRESH_LABELS),
            ("open-qa-logs", _QA_LABELS),
        ):
            button = self.query_one(f"#{identifier}", Button)
            button.label = i18n.tr(language, labels[self._narrow])
        self.query_one("#refresh-reports", Button).tooltip = i18n.tr(
            language, "Обновить"
        )

    def _relabel_rows(self) -> None:
        language = self._language()
        for item, entry in zip(
            self.query_one("#report-list", ListView).children, self.shown
        ):
            item.query_one(Static).update(_report_label(entry, language, self._narrow))
        for item, batch in zip(
            self.query_one("#batch-list", ListView).children, self.view.batches
        ):
            item.query_one(Static).update(_batch_label(batch, language))

    def _apply_layout(self) -> None:
        """The narrow terminal folds the filters and the batches; a wide one opens them."""
        for fold in self.query(Fold):
            fold.collapsed = self._narrow
        self._relabel_buttons()
        self._relabel_rows()

    def on_resize(self, event: events.Resize) -> None:
        narrow = event.size.width < brand.NARROW_WIDTH
        if narrow != self._narrow:
            self._narrow = narrow
            self._apply_layout()

    def sync_language(self) -> None:
        ConsoleShell.sync_language(self)
        language = self._language()
        if getattr(self, "_translated_language", None) != language:
            self._translated_language = language
            with self.prevent(Select.Changed):
                for identifier, options in _SELECT_OPTIONS.items():
                    select = self.query_one(f"#{identifier}", Select)
                    value = select.value
                    select.set_options(
                        [(i18n.tr(language, label), key) for label, key in options]
                    )
                    select.value = value
        self._relabel_buttons()
        self._relabel_rows()
        self.query_one("#reports-status", Static).update(self._status())
        self.query_one("#reports-empty", Static).update(self._empty_text())
        self.query_one("#reports-project", Static).update(
            i18n.tr_text(language, f"Проект: {self.repo.name}")
        )
        for listing in self.query(OpenableList):
            translate_bindings(listing, language)

    def _batch_items(self) -> list[ListItem]:
        items = []
        for batch in self.view.batches:
            item = ListItem(
                Static(_batch_label(batch, self._language()), markup=False),
                name=batch.batch_id,
            )
            item.tooltip = Text(
                f"{batch.ticket} · {batch.batch_id} · {batch.state}\n"
                + (batch.pr_url or "")
            )
            items.append(item)
        return items

    def _report_items(self) -> list[ListItem]:
        items = []
        for entry in self.shown:
            item = ListItem(
                Static(
                    _report_label(entry, self._language(), self._narrow), markup=False
                ),
                name=entry.dispatch_id,
            )
            item.tooltip = Text(
                f"{entry.ticket} · {entry.role} · {entry.at or 'время неизвестно'} · {entry.state} · "
                + (entry.report.outcome if entry.report else "отчёт ещё не получен")
            )
            items.append(item)
        return items

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id in {widget_id for widget_id, _, _ in _FILTERS}:
            await self._apply_filters()

    async def on_select_changed(self, event: Select.Changed) -> None:
        await self._apply_filters()

    async def _apply_filters(self) -> None:
        values = {
            key: self.query_one(f"#{widget_id}", Input).value
            for widget_id, _, key in _FILTERS
        }
        self.shown = console_reports.select_stages(
            self.view.stages,
            **values,
            completion=str(self.query_one("#filter-completion", Select).value),
            sort_by=str(self.query_one("#sort-field", Select).value),
            descending=self.query_one("#sort-direction", Select).value == "desc",
        )
        self.query_one("#reports-status", Static).update(self._status())
        self.query_one("#report-panel", SectionCard).set_note(str(len(self.shown)))
        self.query_one("#report-filter-fold", Fold).set_summary(self._filters_summary())
        self.query_one("#batch-fold", Fold).set_summary(self._batches_summary())
        self.query_one("#reports-empty", Static).display = not self.shown or bool(
            self.view.unavailable
        )
        report_list = self.query_one("#report-list", ListView)
        await _replace_items(report_list, self._report_items())
        self.translate_now()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        super().on_button_pressed(event)
        if event.button.id == "refresh-reports":
            self.action_refresh()
        elif event.button.id == "open-qa-logs":
            self.action_open_qa_logs()

    def action_refresh(self) -> None:
        if self.refreshing:
            return
        self.refreshing = True
        self.refresh_error = ""
        self.query_one("#refresh-reports", Button).disabled = True
        self.query_one("#reports-status", Static).update(self._status())

        def read() -> None:
            try:
                view, error = self._load_view(self.repo), ""
            except Exception as exc:
                view, error = self.view, f"Ошибка обновления: {exc}"
            call_from_worker(self, lambda: self._refreshed(view, error))

        self.run_worker(read, thread=True, group="reports-refresh", exclusive=True)

    async def _refreshed(self, view: LedgerView, error: str) -> None:
        self.view = view
        self.refresh_error = error
        self.refreshing = False
        self.query_one("#refresh-reports", Button).disabled = False
        batch_list = self.query_one("#batch-list", ListView)
        await _replace_items(batch_list, self._batch_items())
        await self._apply_filters()

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        name = event.item.name
        if name is None or event.list_view.id == self.IDS.menu:
            return
        if event.list_view.id == "report-list":
            entry = next(
                (item for item in self.view.stages if item.dispatch_id == name), None
            )
            if entry is not None and entry.report is not None:
                self.app.push_screen(ReportScreen(entry.report, self.view, self.repo))
            elif entry is not None:
                timeline = self.view.timeline(entry.batch_id)
                if timeline is not None:
                    self.app.push_screen(
                        BatchTimelineScreen(
                            timeline,
                            self.repo,
                            notice=f"{entry.dispatch_id} · {entry.state}: отчёт ещё не получен",
                        )
                    )
                else:
                    self.notify("Хронология батча недоступна", severity="warning")
        elif event.list_view.id == "batch-list":
            timeline = self.view.timeline(name)
            if timeline is not None:
                self.app.push_screen(BatchTimelineScreen(timeline, self.repo))

    def action_open_qa_logs(self) -> None:
        self.app.push_screen(QaLogsScreen(self.view))


class ReportScreen(Translated, Screen[None]):
    """One completion report, section by section; every section wraps to the screen width."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Назад"),
        Binding("b", "open_timeline", "Хронология батча"),
        Binding(EXPORT_BINDING_KEY, "export", "Экспорт в Markdown"),
    ]
    DEFAULT_CSS = """
    ReportScreen #report-body { height: 1fr; }
    ReportScreen .section-title { margin-top: 1; text-style: bold; color: $primary; }
    ReportScreen .section-body { padding-left: 2; }
    ReportScreen .actions { height: auto; }
    """

    def __init__(self, entry: ReportEntry, view: LedgerView, repo: Path) -> None:
        super().__init__()
        self.entry = entry
        self.view = view
        self.repo = repo

    def compose(self) -> ComposeResult:
        yield TopBar("Главная › Отчёты › Отчёт")
        with VerticalScroll(id="report-body", classes="frame") as frame:
            frame.border_title = brand.frame_title("Отчёт")
            yield Static(
                console_reports.report_header(self.entry),
                id="report-header",
                markup=False,
            )
            for title, body in console_reports.report_sections(self.entry.report):
                yield Static(title, classes="section-title", markup=False)
                yield Static(
                    body or "—",
                    id=_section_id(title),
                    classes="section-body",
                    markup=False,
                )
            qa_log = console_reports.qa_log_text(self.view, self.entry.dispatch_id)
            if qa_log:
                yield Static("QA log", classes="section-title")
                yield Static(
                    qa_log, id="section-qa-log", classes="section-body", markup=False
                )
        with Horizontal(classes="actions frame") as actions:
            actions.border_title = brand.frame_title("Действия")
            yield Button("Хронология батча", id="open-timeline")
            yield Button("Экспорт в Markdown", id="export")
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "open-timeline":
            self.action_open_timeline()
        elif event.button.id == "export":
            self.action_export()

    def action_export(self) -> None:
        export_document(
            self, self.repo, console_reports.report_document(self.view, self.entry)
        )

    def action_open_timeline(self) -> None:
        timeline = self.view.timeline(self.entry.batch_id)
        if timeline is None:
            self.notify("батч этого отчёта не найден в ledger", severity="warning")
            return
        self.app.push_screen(BatchTimelineScreen(timeline, self.repo))


def _render_timeline(timeline: BatchTimeline, language: str = "ru") -> str:
    batch = timeline.batch
    lines = [
        f"{batch.ticket} · {batch.batch_id} · состояние: {batch.state} · {console_reports.batch_timing_text(batch, language=language)}",
        f"Начало: {console_reports.batch_start_text(batch)}",
        f"PR: {batch.pr_url or 'неизвестно'}",
        f"маршрут: {console_reports.role_flow_text(timeline)}",
        "",
    ]
    for event in timeline.events:
        when = event.at[:19].replace("T", " ") if event.at else "время ?"
        lines.append(f"{when}  [{event.kind}] {event.text}")
    return i18n.tr_text(language, "\n".join(lines))


class BatchTimelineScreen(Translated, Screen[None]):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Назад"),
        Binding(EXPORT_BINDING_KEY, "export", "Экспорт в Markdown"),
    ]

    def __init__(
        self, timeline: BatchTimeline, repo: Path, *, notice: str = ""
    ) -> None:
        super().__init__()
        self.timeline = timeline
        self.repo = repo
        self.notice = notice

    def compose(self) -> ComposeResult:
        yield TopBar("Главная › Отчёты › Хронология")
        timeline = VerticalScroll(
            Static(
                (self.notice + "\n\n" if self.notice else "")
                + _render_timeline(self.timeline),
                id="batch-timeline",
                markup=False,
                classes="no-i18n",
            ),
            classes="frame",
        )
        timeline.border_title = brand.frame_title("Хронология batch")
        yield timeline
        yield Footer()

    def sync_language(self) -> None:
        language = getattr(self.app, "language", "ru")
        notice = i18n.tr_text(language, self.notice)
        self.query_one("#batch-timeline", Static).update(
            (notice + "\n\n" if notice else "")
            + _render_timeline(self.timeline, language)
        )

    def action_export(self) -> None:
        export_document(
            self, self.repo, console_reports.timeline_document(self.timeline)
        )


def _render_qa_logs(view: LedgerView) -> str:
    if view.unavailable:
        return view.unavailable
    parts = [console_reports.qa_attempts_text(view.qa_attempts)]
    parts.extend(console_reports.qa_run_text(run) for run in view.qa_runs)
    if not view.qa_runs:
        parts.append("QA-логов в ledger нет")
    return "\n\n".join(parts)


class QaLogsScreen(Translated, Screen[None]):
    """Every QA gate log in the generation (newest first) and every failed qa-lane attempt."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Назад")]

    def __init__(self, view: LedgerView) -> None:
        super().__init__()
        self.view = view

    def compose(self) -> ComposeResult:
        yield TopBar("Главная › Отчёты › QA-логи")
        logs = VerticalScroll(
            Static(_render_qa_logs(self.view), id="qa-logs", markup=False),
            classes="frame",
        )
        logs.border_title = brand.frame_title("QA-логи")
        yield logs
        yield Footer()
