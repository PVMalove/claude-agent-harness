"""Diagnostics screen: the full `harness health` report plus three actions - "online checks"
(the same run `harness health --online` makes), "apply fixes" (the same run `harness health --fix`
makes, #399: only the in-process `FIXERS` - missing `.harness` directories, a stale skill registry
and, after online checks, missing tracker labels - after an explicit confirmation press) and
"export" (the report as Markdown under docs/tasks/, story 57). Every action shows its CLI
equivalent, and health runs in a worker thread so the TUI never freezes on a network timeout.

A check's `fix.command` is a remedy for the developer to run by hand and is only shown here, never
executed: those commands change machine settings (Windows registry, Developer Mode, global git
config, PATH) or delete data, which epic #341 keeps out of both `--fix` and the console."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Static

from .. import data as console_data
from ...health.model import Report
from ..export import MarkdownDocument, MarkdownSection
from .export import EXPORT_BINDING_KEY, export_document

_STATUS_MARKERS = {"ok": "[OK]", "warn": "[WARN]", "fail": "[FAIL]", "skipped": "-"}
_APPLY_LABEL = "Apply fixes"
_CONFIRM_LABEL = "Подтвердить применение фиксов"
_MANUAL_NOTE = "Команды «Как исправить» пульт не выполняет — запустите их вручную."
_RUNNING = "health выполняется…"


def _render_report(report: Report) -> str:
    summary = report.summary()
    lines = [
        f"Итого: ok={summary['ok']} warn={summary['warn']} "
        f"fail={summary['fail']} skipped={summary['skipped']}",
        _MANUAL_NOTE,
        "",
    ]
    for check in report.checks:
        lines.append(f"{_STATUS_MARKERS[check.status]} {check.id}: {check.message}")
        if check.fix is not None:
            lines.append(f"  -> Как исправить: {check.fix.text}")
            if check.fix.command:
                lines.append(f"     {check.fix.command}")
    if report.fixes_applied:
        lines.append("")
        lines.append("Применённые фиксы:")
        lines.extend(f"  - {command}" for command in report.fixes_applied)
    return "\n".join(lines)


def health_document(report: Report) -> MarkdownDocument:
    """The report as an exportable Markdown document; it has no ticket, so it lands in the common
    console-exports folder."""
    return MarkdownDocument(
        title="Health-отчёт",
        slug="health-report",
        meta=[("Репозиторий", report.repo), ("Онлайн-проверки", "да" if report.online else "нет")],
        sections=[MarkdownSection("Проверки", _render_report(report), preformatted=True)],
    )


class _CollectDiagnostics(Protocol):
    def __call__(self, repo: Path, *, online: bool = False) -> Report: ...


class _ApplyLocalFixes(Protocol):
    def __call__(self, repo: Path, *, online: bool = False) -> Report: ...


class DiagnosticsScreen(Screen[None]):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Назад"),
        Binding(EXPORT_BINDING_KEY, "export", "Экспорт в Markdown"),
    ]

    def __init__(
        self,
        repo: Path,
        *,
        collect_diagnostics: _CollectDiagnostics = console_data.collect_diagnostics,
        apply_local_fixes: _ApplyLocalFixes = console_data.apply_local_fixes,
    ) -> None:
        super().__init__()
        self.repo = repo
        self._collect_diagnostics = collect_diagnostics
        self._apply_local_fixes = apply_local_fixes
        self._report: Report | None = None
        self._confirming_apply = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(Static(_RUNNING, id="diagnostics-report", markup=False))
        yield Button("Online checks", id="online-checks")
        yield Static(
            f"$ {console_data.health_cli_line(self.repo, '--online')}",
            id="online-checks-cli",
            markup=False,
        )
        yield Button(_APPLY_LABEL, id="apply-fixes")
        yield Static(
            f"$ {console_data.health_cli_line(self.repo, '--fix')}",
            id="apply-fixes-cli",
            markup=False,
        )
        yield Button("Export", id="export")
        yield Footer()

    def on_mount(self) -> None:
        self._run_health(lambda: self._collect_diagnostics(self.repo))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "online-checks":
            self._reset_apply()
            self._run_health(lambda: self._collect_diagnostics(self.repo, online=True))
        elif event.button.id == "apply-fixes":
            self._on_apply_fixes_pressed()
        elif event.button.id == "export":
            self.action_export()

    def action_export(self) -> None:
        if self._report is None:
            self.notify("нечего экспортировать: отчёт ещё строится", severity="warning")
            return
        export_document(self, self.repo, health_document(self._report))

    def _on_apply_fixes_pressed(self) -> None:
        if self._report is None:
            return
        if not self._confirming_apply:
            self._confirming_apply = True
            self._apply_button().label = _CONFIRM_LABEL
            return
        self._reset_apply()
        online = self._report.online
        self._run_health(lambda: self._apply_local_fixes(self.repo, online=online))

    def _reset_apply(self) -> None:
        self._confirming_apply = False
        self._apply_button().label = _APPLY_LABEL

    def _run_health(self, run: Callable[[], Report]) -> None:
        self.query_one("#diagnostics-report", Static).update(_RUNNING)

        def work() -> None:
            report = run()
            self.app.call_from_thread(self._show, report)

        self.run_worker(work, thread=True, exclusive=True, group="diagnostics")

    def _show(self, report: Report) -> None:
        self._report = report
        self.query_one("#diagnostics-report", Static).update(_render_report(report))
        flags = ("--online", "--fix") if report.online else ("--fix",)
        self.query_one("#apply-fixes-cli", Static).update(
            f"$ {console_data.health_cli_line(self.repo, *flags)}"
        )

    def _apply_button(self) -> Button:
        return self.query_one("#apply-fixes", Button)
