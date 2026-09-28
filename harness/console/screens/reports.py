"""Reports section: completion reports from the selected ledger generation, filterable by ticket,
role, outcome and date; one report read section by section (text wraps to the screen width); and a
batch chronology - states, dispatches by role, coordinator decisions and risks; QA gate logs (verdict
and tail) with failed qa-lane attempts. A report or a chronology exports to Markdown (`e`) through
the shared screens/export.py action. All facts come from
harness.console.reports; these screens only render them. Report text is untrusted role output, so
every widget renders it with `markup=False`."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, ListItem, ListView, Static

from .. import reports as console_reports
from ..reports import BatchTimeline, LedgerView, ReportEntry
from .. import brand
from .export import EXPORT_BINDING_KEY, export_document

_FILTERS = (
    ("filter-ticket", "тикет", "ticket"),
    ("filter-role", "роль", "role"),
    ("filter-outcome", "outcome", "outcome"),
    ("filter-date", "дата (2026-09-27)", "date"),
)


def _report_label(entry: ReportEntry) -> str:
    date = entry.reported_at[:16].replace("T", " ") if entry.reported_at else "дата ?"
    return f"{date} · {entry.ticket} · {entry.role} · {entry.outcome} · {entry.dispatch_id}"


def _section_id(title: str) -> str:
    return "section-" + title.lower().replace(" ", "-")


class ReportsScreen(Screen[None]):
    """The report list with its filters, and the batch list that opens a chronology."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Назад"),
        Binding("f3", "open_qa_logs", "QA-логи"),
    ]
    DEFAULT_CSS = """
    ReportsScreen #reports-status { height: auto; }
    ReportsScreen #report-filters { height: auto; }
    ReportsScreen #report-filters Input { width: 1fr; }
    ReportsScreen ListView { height: auto; min-height: 3; max-height: 40%; margin: 0 1; }
    ReportsScreen #open-qa-logs { margin: 0 1; }
    """

    def __init__(
        self,
        repo: Path,
        *,
        load_view: Callable[[Path], LedgerView] = console_reports.load_ledger_view,
    ) -> None:
        super().__init__()
        self.repo = repo
        self.view = load_view(repo)
        self.shown: list[ReportEntry] = list(self.view.reports)

    def compose(self) -> ComposeResult:
        yield Header(icon=brand.MENU_ICON)
        status = Static(
            self._status(), id="reports-status", classes="frame", markup=False
        )
        status.border_title = "Состояние"
        yield status
        with Horizontal(id="report-filters", classes="frame") as filters:
            filters.border_title = "Фильтры"
            for widget_id, placeholder, _ in _FILTERS:
                yield Input(placeholder=placeholder, id=widget_id)
        reports = ListView(*self._report_items(), id="report-list")
        reports.border_title = "Completion reports"
        yield reports
        yield Button("QA-логи", id="open-qa-logs")
        batches = ListView(
            *(
                ListItem(
                    Static(
                        f"{batch.ticket} · {batch.state} · {batch.batch_id}",
                        markup=False,
                    ),
                    name=batch.batch_id,
                )
                for batch in self.view.batches
            ),
            id="batch-list",
        )
        batches.border_title = "Батчи (хронология)"
        yield batches
        yield Footer()

    def _status(self) -> str:
        if self.view.unavailable:
            return self.view.unavailable
        return f"отчётов: {len(self.shown)} из {len(self.view.reports)}"

    def _report_items(self) -> list[ListItem]:
        return [
            ListItem(Static(_report_label(entry), markup=False), name=entry.dispatch_id)
            for entry in self.shown
        ]

    async def on_input_changed(self, event: Input.Changed) -> None:
        values = {
            key: self.query_one(f"#{widget_id}", Input).value
            for widget_id, _, key in _FILTERS
        }
        self.shown = console_reports.filter_reports(self.view.reports, **values)
        self.query_one("#reports-status", Static).update(self._status())
        report_list = self.query_one("#report-list", ListView)
        await report_list.clear()
        await report_list.extend(self._report_items())

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        name = event.item.name
        if name is None:
            return
        if event.list_view.id == "report-list":
            entry = next(item for item in self.view.reports if item.dispatch_id == name)
            self.app.push_screen(ReportScreen(entry, self.view, self.repo))
        elif event.list_view.id == "batch-list":
            timeline = self.view.timeline(name)
            if timeline is not None:
                self.app.push_screen(BatchTimelineScreen(timeline, self.repo))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "open-qa-logs":
            self.action_open_qa_logs()

    def action_open_qa_logs(self) -> None:
        self.app.push_screen(QaLogsScreen(self.view))


class ReportScreen(Screen[None]):
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
        yield Header(icon=brand.MENU_ICON)
        with VerticalScroll(id="report-body", classes="frame") as frame:
            frame.border_title = "Отчёт"
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
            actions.border_title = "Действия"
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


def _render_timeline(timeline: BatchTimeline) -> str:
    batch = timeline.batch
    lines = [
        f"{batch.ticket} · {batch.batch_id} · состояние: {batch.state}",
        f"маршрут: {console_reports.role_flow_text(timeline)}",
        "",
    ]
    for event in timeline.events:
        when = event.at[:19].replace("T", " ") if event.at else "время ?"
        lines.append(f"{when}  [{event.kind}] {event.text}")
    return "\n".join(lines)


class BatchTimelineScreen(Screen[None]):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Назад"),
        Binding(EXPORT_BINDING_KEY, "export", "Экспорт в Markdown"),
    ]

    def __init__(self, timeline: BatchTimeline, repo: Path) -> None:
        super().__init__()
        self.timeline = timeline
        self.repo = repo

    def compose(self) -> ComposeResult:
        yield Header(icon=brand.MENU_ICON)
        timeline = VerticalScroll(
            Static(_render_timeline(self.timeline), id="batch-timeline", markup=False),
            classes="frame",
        )
        timeline.border_title = "Хронология batch"
        yield timeline
        yield Footer()

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


class QaLogsScreen(Screen[None]):
    """Every QA gate log in the generation (newest first) and every failed qa-lane attempt."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Назад")]

    def __init__(self, view: LedgerView) -> None:
        super().__init__()
        self.view = view

    def compose(self) -> ComposeResult:
        yield Header(icon=brand.MENU_ICON)
        logs = VerticalScroll(
            Static(_render_qa_logs(self.view), id="qa-logs", markup=False),
            classes="frame",
        )
        logs.border_title = "QA-логи"
        yield logs
        yield Footer()
