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
from textual.widgets import Button, Input, ListItem, ListView, Select, Static

from ..shell import SHELL_CSS, ConsoleShell, FieldGrid, Fold, SectionCard, WrappedValue
from ..status import Status
from ..translate import translate_bindings
from ..widgets import OpenableList, ScrollingView
from .. import reports as console_reports
from ..reports import BatchTimeline, LedgerView, ReportEntry, StageEntry
from .. import brand
from .. import i18n
from ..json_fields import strings, text
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


_DETAIL_CSS = brand.shell_css("""
.report-detail .report-page { height: 1fr; padding: 0 2; }
.report-detail .page-head { height: auto; margin-bottom: 1; }
.report-detail .page-heading { width: 1fr; height: auto; }
.report-detail .page-title { height: 1; text-style: bold; }
.report-detail .page-subtitle { height: auto; color: $text-muted; }
.report-detail .page-actions { width: auto; height: 1; }
.report-detail .page-actions Button { margin-left: 1; }
.report-detail .field-grid { margin-bottom: 1; }
.report-detail .section-card { margin-bottom: 1; }
.report-detail .section-body, .report-detail .timeline-event { height: auto; }
.report-detail .notice { height: auto; margin-bottom: 1; }
.report-detail.-narrow .page-head { layout: vertical; }
.report-detail.-narrow .page-actions { margin-top: 1; }
.report-detail.-narrow .page-actions Button:first-child { margin-left: 0; }
""")
_KIND_COLOURS = {
    "state": brand.ROLE_COLORS["verification"],
    "dispatch": brand.PALETTE["primary"],
    "report": brand.PALETTE["success"],
    "decision": brand.PALETTE["warning"],
    "risk": brand.PALETTE["error"],
}


def _status_text(status: Status | None, body: str, language: str) -> Text:
    """A line of a report: the status (symbol and label) in its colour, then the text."""
    line = Text()
    if status is not None:
        line.append(status.render(language), style=status.color)
        if body:
            line.append("  ")
    line.append(body)
    return line


def _lines_text(lines: list[console_reports.ReportLine], language: str) -> Text:
    """Several report lines as one `Text`; never markup, the lines are untrusted role output."""
    return Text("\n").join(
        _status_text(status, i18n.tr_text(language, body) if status else body, language)
        for status, body in lines
    )


def _utc_text(value: str) -> str:
    """A recorded time as `2026-10-16 12:59 UTC`; `?` when it is missing or unreadable."""
    moment = console_reports.timestamp(value)
    return moment.strftime("%Y-%m-%d %H:%M UTC") if moment else "?"


class ReportScreen(ConsoleShell):
    """One completion report, section by section; every section wraps to the screen width."""

    DEFAULT_CLASSES = "shell report-detail"
    CRUMBS = "Главная › Отчёты › Отчёт"
    SECTION = "Reports"
    IS_SECTION_ROOT = False
    AUTO_FOCUS = "#report-body"
    CSS = SHELL_CSS + _DETAIL_CSS
    BINDINGS = [
        Binding("b", "open_timeline", "Хронология"),
        Binding(EXPORT_BINDING_KEY, "export", "Экспорт"),
    ]

    def __init__(self, entry: ReportEntry, view: LedgerView, repo: Path) -> None:
        super().__init__()
        self.entry = entry
        self.view = view
        self.repo = repo

    def _language(self) -> str:
        return getattr(self.app, "language", "ru")

    def _subtitle(self) -> Text:
        entry = self.entry
        language = self._language()
        status = console_reports.outcome_status(entry.outcome)
        line = Text(f"{entry.ticket} · {entry.role} · ")
        line.append(status.render(language), style=status.color)
        when = (
            _utc_text(entry.reported_at)
            if entry.reported_at
            else i18n.tr(language, "дата неизвестна")
        )
        line.append(f" · {when}")
        return line

    def _fields(self) -> list[tuple[str, str]]:
        entry = self.entry
        files = ", ".join(strings(entry.report.get("changed_files")))
        return [
            ("dispatch", entry.dispatch_id),
            ("batch", entry.batch_id or "?"),
            ("commit", text(entry.report.get("commit_sha"), "—")),
            ("файлы", files or "—"),
        ]

    def compose_strip(self) -> ComposeResult:
        yield WrappedValue(self.entry.dispatch_id)

    def compose_content(self) -> ComposeResult:
        language = self._language()
        with ScrollingView(id="report-body", classes="report-page"):
            with Horizontal(classes="page-head"):
                with Vertical(classes="page-heading"):
                    yield Static("Отчёт", classes="page-title")
                    yield Static(
                        self._subtitle(),
                        id="report-header",
                        classes="page-subtitle no-i18n",
                        markup=False,
                    )
                with Horizontal(classes="page-actions"):
                    yield Button("b Хронология batch", id="open-timeline", compact=True)
                    yield Button("e Экспорт в Markdown", id="export", compact=True)
            yield FieldGrid(self._fields(), id="report-fields")
            for title, lines in console_reports.report_section_lines(self.entry.report):
                with SectionCard(title=title, id="card-" + _section_id(title)):
                    yield Static(
                        _lines_text(lines, language),
                        id=_section_id(title),
                        classes="section-body no-i18n",
                        markup=False,
                    )
            qa_log = console_reports.qa_log_text(self.view, self.entry.dispatch_id)
            if qa_log:
                with SectionCard(title="QA log", id="card-section-qa-log"):
                    yield Static(
                        qa_log,
                        id="section-qa-log",
                        classes="section-body no-i18n",
                        markup=False,
                    )

    def sync_language(self) -> None:
        ConsoleShell.sync_language(self)
        language = self._language()
        self.query_one("#report-header", Static).update(self._subtitle())
        sections = dict(console_reports.report_section_lines(self.entry.report))
        for title, lines in sections.items():
            self.query_one(f"#{_section_id(title)}", Static).update(
                _lines_text(lines, language)
            )
        for scroll in self.query(ScrollingView):
            translate_bindings(scroll, language)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        super().on_button_pressed(event)
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


class BatchTimelineScreen(ConsoleShell):
    """The chronology of one batch: its facts, the route of roles and the events in time order."""

    DEFAULT_CLASSES = "shell report-detail"
    CRUMBS = "Главная › Отчёты › Хронология"
    SECTION = "Reports"
    IS_SECTION_ROOT = False
    AUTO_FOCUS = "#batch-timeline"
    CSS = SHELL_CSS + _DETAIL_CSS
    BINDINGS = [Binding(EXPORT_BINDING_KEY, "export", "Экспорт")]

    def __init__(
        self, timeline: BatchTimeline, repo: Path, *, notice: str = ""
    ) -> None:
        super().__init__()
        self.timeline = timeline
        self.repo = repo
        self.notice = notice

    def _language(self) -> str:
        return getattr(self.app, "language", "ru")

    def _fields(self) -> list[tuple[str, str]]:
        batch = self.timeline.batch
        language = self._language()
        timing = console_reports.batch_timing_text(batch, language=language)
        return [
            ("Состояние", console_reports.batch_status(batch).render(language)),
            ("До PR", timing.removeprefix("до PR: ").removeprefix("to PR: ")),
            (
                "Начало",
                i18n.tr_text(language, console_reports.batch_start_text(batch)),
            ),
            ("PR", batch.pr_url or i18n.tr(language, "неизвестно")),
        ]

    def _route(self) -> str:
        language = self._language()
        return i18n.tr_text(
            language, f"маршрут: {console_reports.role_flow_text(self.timeline)}"
        )

    def _event_row(self, event: console_reports.TimelineEvent) -> Text:
        language = self._language()
        when = event.at[:16].replace("T", " ") if event.at else "?"
        row = Text(f"{when}  ")
        colour = _KIND_COLOURS.get(event.kind, brand.PALETTE["muted"])
        row.append(f"{event.kind:<9}", style=f"bold {colour}")
        row.append(f" {i18n.tr_text(language, event.text)}")
        return row

    def _notice_text(self) -> Text:
        return Text(
            f"{brand.STATUS_ICONS['warn']} {i18n.tr_text(self._language(), self.notice)}",
            style=brand.STATUS_COLORS["warn"],
        )

    def compose_strip(self) -> ComposeResult:
        yield WrappedValue(self.timeline.batch.batch_id)

    def compose_content(self) -> ComposeResult:
        batch = self.timeline.batch
        with ScrollingView(id="batch-timeline", classes="report-page"):
            with Horizontal(classes="page-head"):
                with Vertical(classes="page-heading"):
                    yield Static("Хронология batch", classes="page-title")
                    yield Static(
                        f"{batch.ticket} · {batch.batch_id}",
                        id="timeline-header",
                        classes="page-subtitle no-i18n",
                        markup=False,
                    )
                with Horizontal(classes="page-actions"):
                    yield Button("e Экспорт в Markdown", id="export", compact=True)
            if self.notice:
                yield Static(
                    self._notice_text(),
                    id="timeline-notice",
                    classes="notice no-i18n",
                    markup=False,
                )
            yield FieldGrid(self._fields(), id="timeline-fields")
            yield Static(
                self._route(), id="timeline-route", classes="no-i18n", markup=False
            )
            with SectionCard(
                title="События",
                note=f"{len(self.timeline.events)} · от старых к новым",
                id="timeline-events",
            ):
                for event in self.timeline.events:
                    yield Static(
                        self._event_row(event),
                        classes="timeline-event no-i18n",
                        markup=False,
                    )

    def sync_language(self) -> None:
        ConsoleShell.sync_language(self)
        self.query_one("#timeline-fields", FieldGrid).set_fields(self._fields())
        self.query_one("#timeline-route", Static).update(self._route())
        if self.notice:
            self.query_one("#timeline-notice", Static).update(self._notice_text())
        for row, event in zip(
            self.query(".timeline-event").results(Static), self.timeline.events
        ):
            row.update(self._event_row(event))
        for scroll in self.query(ScrollingView):
            translate_bindings(scroll, self._language())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        super().on_button_pressed(event)
        if event.button.id == "export":
            self.action_export()

    def action_export(self) -> None:
        export_document(
            self, self.repo, console_reports.timeline_document(self.timeline)
        )


class QaLogsScreen(ConsoleShell):
    """Every QA gate log in the generation (newest first) and every failed qa-lane attempt."""

    DEFAULT_CLASSES = "shell report-detail"
    CRUMBS = "Главная › Отчёты › QA-логи"
    SECTION = "Reports"
    IS_SECTION_ROOT = False
    AUTO_FOCUS = "#qa-logs"
    CSS = SHELL_CSS + _DETAIL_CSS

    def __init__(self, view: LedgerView) -> None:
        super().__init__()
        self.view = view

    def _language(self) -> str:
        return getattr(self.app, "language", "ru")

    def _strip_text(self) -> str:
        view = self.view
        if view.unavailable:
            return view.unavailable
        return i18n.tr_text(
            self._language(),
            f"прогонов: {len(view.qa_runs)} · неудачных попыток: {len(view.qa_attempts)}",
        )

    def _attempts(self) -> Text:
        attempts = self.view.qa_attempts
        language = self._language()
        if not attempts:
            return _status_text(
                Status("accepted", "неудачных попыток нет", "no failed attempts"),
                "",
                language,
            )
        unknown = "time ?" if language == "en" else "время ?"
        return Text("\n").join(
            Text(
                f"{brand.STATUS_ICONS['warn']} {attempt.failed_at or unknown} · "
                f"{attempt.dispatch_id} · {attempt.stage}: {attempt.message}",
                style=brand.STATUS_COLORS["warn"],
            )
            for attempt in attempts
        )

    def _run_text(self, run: console_reports.QaRun) -> Text:
        en = self._language() == "en"
        verdict = console_reports.qa_run_status(run)
        failed = [(command, code) for command, code in run.commands if code != 0]
        dispatch = run.dispatch_id or ("no report" if en else "без отчёта")
        outcome = ("report: " if en else "отчёт: ") + run.outcome
        counts = (
            f"{len(run.commands)} commands, {len(failed)} failed"
            if en
            else f"команд {len(run.commands)}, с ошибкой {len(failed)}"
        )
        if failed:
            counts += (
                " ("
                + ", ".join(f"{command}: exit {code}" for command, code in failed)
                + ")"
            )
        tail = (
            f"log tail (last {len(run.tail)} of {run.total_lines} lines):"
            if en
            else f"хвост лога (последние {len(run.tail)} из {run.total_lines} строк):"
        )
        text = Text()
        text.append(verdict.render(self._language()), style=f"bold {verdict.color}")
        text.append(f"  {run.artifact}\n")
        text.append(f"{dispatch} · {run.ticket} · {outcome}\n{counts}\n{tail}\n")
        text.append(
            "\n".join(f"  {line}" for line in run.tail), style=brand.PALETTE["muted"]
        )
        return text

    def compose_strip(self) -> ComposeResult:
        yield Static(
            self._strip_text(), id="qa-summary", classes="no-i18n", markup=False
        )

    def compose_content(self) -> ComposeResult:
        view = self.view
        with ScrollingView(id="qa-logs", classes="report-page"):
            with Vertical(classes="page-head"):
                yield Static("QA-логи", classes="page-title")
                yield Static(
                    "Попытки qa-lane и каждый прогон QA-gate, новые сверху.",
                    classes="page-subtitle",
                )
            if view.unavailable:
                yield Static(
                    Text(
                        f"{brand.STATUS_ICONS['fail']} {view.unavailable}",
                        style=brand.STATUS_COLORS["fail"],
                    ),
                    id="qa-unavailable",
                    classes="no-i18n",
                    markup=False,
                )
                return
            with SectionCard(
                title="Попытки qa-lane",
                note=f"{len(view.qa_attempts)} неудачных",
                id="qa-attempts",
            ):
                yield Static(
                    self._attempts(),
                    id="qa-attempts-list",
                    classes="no-i18n",
                    markup=False,
                )
            for index, run in enumerate(view.qa_runs):
                with SectionCard(
                    title=f"Прогон {Path(run.artifact).stem[:8]}…",
                    note=run.logged_at or "время ?",
                    id=f"qa-run-{index}",
                ):
                    yield Static(
                        self._run_text(run),
                        classes="section-body qa-run no-i18n",
                        markup=False,
                    )
            if not view.qa_runs:
                yield Static("QA-логов в ledger нет", id="qa-no-runs")

    def sync_language(self) -> None:
        ConsoleShell.sync_language(self)
        self.query_one("#qa-summary", Static).update(self._strip_text())
        if not self.view.unavailable:
            self.query_one("#qa-attempts-list", Static).update(self._attempts())
            for body, run in zip(
                self.query(".qa-run").results(Static), self.view.qa_runs
            ):
                body.update(self._run_text(run))
        for scroll in self.query(ScrollingView):
            translate_bindings(scroll, self._language())
