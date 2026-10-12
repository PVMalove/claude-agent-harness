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

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Input, ListItem, ListView, Static

from ..chrome import TopBar
from ..translate import Translated
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


_OUTCOME_STATUS = {"completed": "ok", "failed": "fail", "blocked": "warn"}


def _short_id(identifier: str, prefix: str) -> str:
    return identifier.removeprefix(prefix)[:8]


def _report_label(entry: ReportEntry) -> Text:
    """One aligned row. A `Text`, never markup: the fields come from untrusted role output."""
    date = entry.reported_at[:16].replace("T", " ")[5:] if entry.reported_at else "?"
    status = _OUTCOME_STATUS.get(entry.outcome, "skipped")
    label = Text(f"{date:<11} {entry.ticket:<6} {entry.role:<13} ")
    label.append(
        f"{brand.STATUS_ICONS[status]} {entry.outcome:<10}",
        style=brand.STATUS_COLORS[status],
    )
    label.append(f" {_short_id(entry.dispatch_id, 'dispatch-')}")
    return label


def _section_id(title: str) -> str:
    return "section-" + title.lower().replace(" ", "-")


class ReportsScreen(Translated, Screen[None]):
    """The report list with its filters, and the batch list that opens a chronology."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Назад"),
        Binding("f3", "open_qa_logs", "QA-логи"),
    ]
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (100, "-wide")]
    DEFAULT_CSS = """
    ReportsScreen #report-filters { height: 1; margin: 0 2; }
    ReportsScreen #reports-status { width: auto; }
    ReportsScreen #report-filters Input { width: 1fr; margin-left: 1; }
    ReportsScreen #reports-body { width: 100%; height: 1fr; margin: 0 1; }
    ReportsScreen ListView > ListItem { height: 1; padding: 0 1; }
    ReportsScreen ListView Static { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    ReportsScreen #report-list { width: 2fr; height: 1fr; margin: 0; }
    ReportsScreen #batch-list { width: 1fr; height: 1fr; margin: 0; }
    ReportsScreen.-narrow #reports-body { layout: vertical; }
    ReportsScreen.-narrow #report-list { width: 1fr; }
    ReportsScreen.-narrow #batch-list { width: 1fr; height: 8; }
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
        yield TopBar("Главная › Отчёты")
        with Horizontal(id="report-filters"):
            yield Static(self._status(), id="reports-status", markup=False)
            for widget_id, placeholder, _ in _FILTERS:
                yield Input(placeholder=placeholder, id=widget_id, compact=True)
        with Horizontal(id="reports-body"):
            reports = ListView(*self._report_items(), id="report-list")
            reports.border_title = brand.frame_title("Отчёты ролей")
            yield reports
            batches = ListView(
                *(
                    ListItem(
                        Static(
                            f"{batch.ticket} · {batch.state} · "
                            f"{_short_id(batch.batch_id, 'batch-')}",
                            markup=False,
                        ),
                        name=batch.batch_id,
                    )
                    for batch in self.view.batches
                ),
                id="batch-list",
            )
            batches.border_title = brand.frame_title("Батчи (хронология)")
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


class BatchTimelineScreen(Translated, Screen[None]):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Назад"),
        Binding(EXPORT_BINDING_KEY, "export", "Экспорт в Markdown"),
    ]

    def __init__(self, timeline: BatchTimeline, repo: Path) -> None:
        super().__init__()
        self.timeline = timeline
        self.repo = repo

    def compose(self) -> ComposeResult:
        yield TopBar("Главная › Отчёты › Хронология")
        timeline = VerticalScroll(
            Static(_render_timeline(self.timeline), id="batch-timeline", markup=False),
            classes="frame",
        )
        timeline.border_title = brand.frame_title("Хронология batch")
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
