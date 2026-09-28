"""Экран Diagnostics: полный отчёт `harness health` и три действия — «Online checks» (аналог запуска
`harness health --online`), «Apply fixes» (аналог запуска `harness health --fix`, применяющий только
внутрипроцессные фиксеры FIXERS после явного подтверждения) и «Export» (экспорт отчёта в Markdown).
Каждое действие отображает эквивалент команды CLI, а сбор данных выполняется в фоновом потоке.

Команды из блока «Как исправить» предназначены для ручного выполнения разработчиком и только отображаются
на экране: они изменяют настройки машины или удаляют данные, что исключено из автоматического применения.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Static

from .. import brand
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
    """Формирует текстовое представление отчёта проверок health со статусами и рекомендациями."""
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


_MARKER_COLORS = {
    "[OK]": brand.PALETTE["success"],
    "[WARN]": brand.PALETTE["warning"],
    "[FAIL]": brand.PALETTE["error"],
}


def _styled_report(report: Report) -> Text:
    """The same text as `_render_report`, with status markers and remedies coloured on screen."""
    styled = Text()
    for index, line in enumerate(_render_report(report).split("\n")):
        if index:
            styled.append("\n")
        marker = next((m for m in _MARKER_COLORS if line.startswith(m + " ")), None)
        if marker is not None:
            styled.append(marker, style=f"bold {_MARKER_COLORS[marker]}")
            styled.append(line[len(marker) :])
        elif line.startswith("  -> "):
            styled.append(line, style=brand.PALETTE["secondary"])
        elif line.startswith("     ") or line.startswith("- "):
            styled.append(line, style=brand.PALETTE["muted"])
        else:
            styled.append(line)
    return styled


def health_document(report: Report) -> MarkdownDocument:
    """Формирует структурированный Markdown-документ отчёта health для экспорта в папку артефактов."""
    return MarkdownDocument(
        title="Health-отчёт",
        slug="health-report",
        meta=[
            ("Репозиторий", report.repo),
            ("Онлайн-проверки", "да" if report.online else "нет"),
        ],
        sections=[
            MarkdownSection("Проверки", _render_report(report), preformatted=True)
        ],
    )


class _CollectDiagnostics(Protocol):
    """Протокол функции сбора диагностического отчёта health."""

    def __call__(self, repo: Path, *, online: bool = False) -> Report:
        """Выполняет проверку состояния репозитория и возвращает отчёт."""
        ...


class _ApplyLocalFixes(Protocol):
    """Протокол функции применения локальных автоматических исправлений."""

    def __call__(self, repo: Path, *, online: bool = False) -> Report:
        """Применяет локальные исправления и возвращает обновлённый отчёт проверок."""
        ...


class DiagnosticsScreen(Screen[None]):
    """Экран диагностики состояния репозитория с возможностью запуска онлайн-проверок и применения исправлений."""

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
        """Инициализирует экран диагностики для указанного репозитория."""
        super().__init__()
        self.repo = repo
        self._collect_diagnostics = collect_diagnostics
        self._apply_local_fixes = apply_local_fixes
        self._report: Report | None = None
        self._confirming_apply = False

    def compose(self) -> ComposeResult:
        """Формирует структуру виджетов экрана диагностики."""
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
        """Запускает первичное построение отчёта проверок при монтировании экрана."""
        self._run_health(lambda: self._collect_diagnostics(self.repo))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Обрабатывает нажатия кнопок экрана (онлайн-проверки, применение фиксов, экспорт)."""
        if event.button.id == "online-checks":
            self._reset_apply()
            self._run_health(lambda: self._collect_diagnostics(self.repo, online=True))
        elif event.button.id == "apply-fixes":
            self._on_apply_fixes_pressed()
        elif event.button.id == "export":
            self.action_export()

    def action_export(self) -> None:
        """Выполняет экспорт текущего отчёта health в файл Markdown."""
        if self._report is None:
            self.notify("нечего экспортировать: отчёт ещё строится", severity="warning")
            return
        export_document(self, self.repo, health_document(self._report))

    def _on_apply_fixes_pressed(self) -> None:
        """Обрабатывает нажатие кнопки применения фиксов с запросом подтверждения."""
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
        """Сбрасывает состояние подтверждения кнопки применения фиксов к исходному."""
        self._confirming_apply = False
        self._apply_button().label = _APPLY_LABEL

    def _run_health(self, run: Callable[[], Report]) -> None:
        """Запускает процедуру проверки health в фоновом потоке worker."""
        self.query_one("#diagnostics-report", Static).update(_RUNNING)

        def work() -> None:
            """Фоновая задача выполнения проверки health и передачи отчёта в основной поток UI."""
            report = run()
            self.app.call_from_thread(self._show, report)

        self.run_worker(work, thread=True, exclusive=True, group="diagnostics")

    def _show(self, report: Report) -> None:
        """Отображает готовый отчёт health и обновляет строку эквивалента CLI на экране."""
        self._report = report
        self.query_one("#diagnostics-report", Static).update(_styled_report(report))
        flags = ("--online", "--fix") if report.online else ("--fix",)
        self.query_one("#apply-fixes-cli", Static).update(
            f"$ {console_data.health_cli_line(self.repo, *flags)}"
        )

    def _apply_button(self) -> Button:
        """Возвращает кнопку применения фиксов экрана."""
        return self.query_one("#apply-fixes", Button)
