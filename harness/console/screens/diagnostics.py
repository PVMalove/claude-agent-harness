"""Экран Diagnostics: полный отчёт `harness health` и три действия — «Онлайн» (аналог запуска
`harness health --online`), «Исправить» (аналог запуска `harness health --fix`, применяющий только
внутрипроцессные фиксеры FIXERS после явного подтверждения) и «Экспорт» (экспорт отчёта в Markdown).
Каждое действие отображает эквивалент команды CLI, а сбор данных выполняется в фоновом потоке.

Команды из блока «Как исправить» предназначены для ручного выполнения разработчиком и только отображаются
на экране: они изменяют настройки машины или удаляют данные, что исключено из автоматического применения.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Protocol

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Static

from ..shell import SHELL_CSS, ConsoleShell, WrappedValue
from ..logtext import colorize_log
from .. import brand, i18n
from .. import data as console_data
from ...health.model import Report
from ..export import MarkdownDocument, MarkdownSection
from .commands import call_from_worker
from .export import EXPORT_BINDING_KEY, export_document

_STATUS_MARKERS = {"ok": "[OK]", "warn": "[WARN]", "fail": "[FAIL]", "skipped": "-"}
_APPLY_LABEL = "Исправить"
_CONFIRM_LABEL = "Подтвердить фиксы"
_MANUAL_NOTE = "Команды «Как исправить» пульт не выполняет — запустите их вручную."
_RUNNING = "health выполняется…"


def render_report(report: Report) -> str:
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


_FIXED_REPORT_LINES = ("Итого:", "Команды «Как исправить»", "Применённые фиксы:")


def _translated_report(report: Report, language: str) -> str:
    """`render_report` with only its own fixed lines translated; check messages are data."""
    lines = []
    for line in render_report(report).split("\n"):
        if line.startswith(_FIXED_REPORT_LINES):
            line = i18n.tr_text(language, line)
        elif line.startswith("  -> Как исправить:"):
            line = line.replace(
                "Как исправить:", i18n.tr_text(language, "Как исправить:"), 1
            )
        lines.append(line)
    return "\n".join(lines)


_COUNTER = re.compile(r"\b(ok|warn|fail|skipped)=(\d+)")
_CHECK_MARKERS = {"[OK] ": "ok", "[WARN] ": "warn", "[FAIL] ": "fail"}
# The totals line in either language (see `render_report` and its translation): the whole counter
# list is required, so foreign output that merely starts with "Итого:" does not start a report.
_TOTALS = re.compile(r"(Итого|Total): ok=\d+ warn=\d+ fail=\d+ skipped=\d+")


def _icon(status: str, *, muted: bool = False) -> Text:
    """The status symbol; a zero counter is muted, like its number."""
    color = brand.PALETTE["muted"] if muted else brand.STATUS_COLORS[status]
    return Text(f"{brand.STATUS_ICONS[status]} ", style=f"bold {color}")


def _summary_line(line: str) -> Text:
    """The totals line with a status symbol before each counter."""
    styled = colorize_log(line)
    shown = Text()
    position = 0
    for counter in _COUNTER.finditer(line):
        shown.append_text(styled[position : counter.start()])
        shown.append_text(_icon(counter.group(1), muted=counter.group(2) == "0"))
        position = counter.start()
    shown.append_text(styled[position:])
    return shown


def _styled_line(line: str, *, in_report: bool) -> Text:
    for marker, status in _CHECK_MARKERS.items():
        if line.startswith(marker):
            return _icon(status) + colorize_log(line)
    # "- " marks a skipped check only inside a report; elsewhere it is a plain list item.
    if in_report and line.startswith("- "):
        return _icon("skipped") + colorize_log(line)
    return colorize_log(line)


def styled_log(text: str) -> Text:
    """`colorize_log(text)` for a log that may hold a health report: every check status and every
    counter of the report also gets its symbol (a colour is never the only cue). The text itself is
    only prefixed on screen; nothing exported or compared changes."""
    shown = Text()
    in_report = False
    for index, line in enumerate(text.split("\n")):
        if index:
            shown.append("\n")
        if _TOTALS.match(line):
            in_report = True
            shown.append_text(_summary_line(line))
        else:
            shown.append_text(_styled_line(line, in_report=in_report))
    return shown


def _styled_report(report: Report, language: str = "ru") -> Text:
    """The same text as `render_report`, with statuses, counters and remedies coloured on screen
    and every status and counter also marked by its symbol."""
    return styled_log(_translated_report(report, language))


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
            MarkdownSection("Проверки", render_report(report), preformatted=True)
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


class DiagnosticsScreen(ConsoleShell):
    """Экран диагностики состояния репозитория с возможностью запуска онлайн-проверок и применения исправлений."""

    CRUMBS = "Главная › Диагностика"
    SECTION = "Diagnostics"
    AUTO_FOCUS = "#diagnostics-scroll"
    CSS = SHELL_CSS + brand.shell_css("""
    DiagnosticsScreen #diagnostics-scroll { height: 1fr; }
    DiagnosticsScreen #diagnostics-actions { height: auto; }
    DiagnosticsScreen .action-row { height: auto; }
    DiagnosticsScreen .action-row Button { width: 20; }
    DiagnosticsScreen .action-cli { width: 1fr; padding: 0 0 0 2; color: $text-muted; }
    """)

    BINDINGS = [Binding(EXPORT_BINDING_KEY, "export", "Экспорт в Markdown")]

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

    def compose_strip(self) -> ComposeResult:
        yield WrappedValue(str(self.repo))

    def compose_content(self) -> ComposeResult:
        """Формирует структуру виджетов экрана диагностики: отчёт и действия в рамках."""
        with VerticalScroll(id="diagnostics-scroll", classes="frame") as report:
            report.border_title = brand.frame_title("Health-отчёт")
            yield Static(_RUNNING, id="diagnostics-report", markup=False)
        with Vertical(id="diagnostics-actions", classes="frame") as actions:
            actions.border_title = brand.frame_title("Действия")
            with Horizontal(classes="action-row"):
                yield Button("Офлайн", id="offline-checks", compact=True)
                yield Static(
                    f"$ {console_data.health_cli_line(self.repo)}",
                    id="offline-checks-cli",
                    classes="action-cli",
                    markup=False,
                )
            with Horizontal(classes="action-row"):
                yield Button("Онлайн", id="online-checks", compact=True)
                yield Static(
                    f"$ {console_data.health_cli_line(self.repo, '--online')}",
                    id="online-checks-cli",
                    classes="action-cli",
                    markup=False,
                )
            with Horizontal(classes="action-row"):
                yield Button(_APPLY_LABEL, id="apply-fixes", compact=True)
                yield Static(
                    f"$ {console_data.health_cli_line(self.repo, '--fix')}",
                    id="apply-fixes-cli",
                    classes="action-cli",
                    markup=False,
                )
            with Horizontal(classes="action-row"):
                yield Button("Экспорт", id="export", compact=True)

    def on_mount(self) -> None:
        """Запускает первичное построение отчёта проверок при монтировании экрана."""
        self._run_health(lambda: self._collect_diagnostics(self.repo))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Обрабатывает нажатия кнопок экрана (онлайн-проверки, применение фиксов, экспорт)."""
        if event.button.id == "offline-checks":
            self._reset_apply()
            self._run_health(lambda: self._collect_diagnostics(self.repo))
        elif event.button.id == "online-checks":
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

    def sync_language(self) -> None:
        """Show the finished report in the current language."""
        ConsoleShell.sync_language(self)
        if self._report is not None:
            self.query_one("#diagnostics-report", Static).update(
                _styled_report(self._report, getattr(self.app, "language", "ru"))
            )

    def on_unmount(self) -> None:
        """Отменяет фоновые воркеры при размонтировании экрана."""
        self.workers.cancel_all()

    def _run_health(self, run: Callable[[], Report]) -> None:
        """Запускает процедуру проверки health в фоновом потоке worker."""
        self.query_one("#diagnostics-report", Static).update(_RUNNING)

        def work() -> None:
            """Фоновая задача выполнения проверки health и передачи отчёта в основной поток UI."""
            try:
                report = run()
            except Exception as exc:
                # An unhandled worker error would exit the whole TUI; show it in the report instead.
                failure = f"health не выполнен: {exc!r}"
                call_from_worker(self, lambda: self._show_failure(failure))
                return
            call_from_worker(self, lambda: self._show(report))

        self.run_worker(work, thread=True, exclusive=True, group="diagnostics")

    def _show_failure(self, text: str) -> None:
        """Отображает причину, по которой отчёт health не построен."""
        self.query_one("#diagnostics-report", Static).update(text)

    def _show(self, report: Report) -> None:
        """Отображает готовый отчёт health и обновляет строку эквивалента CLI на экране."""
        self._report = report
        self.query_one("#diagnostics-report", Static).update(
            _styled_report(report, getattr(self.app, "language", "ru"))
        )
        flags = ("--online", "--fix") if report.online else ("--fix",)
        self.query_one("#apply-fixes-cli", Static).update(
            f"$ {console_data.health_cli_line(self.repo, *flags)}"
        )

    def _apply_button(self) -> Button:
        """Возвращает кнопку применения фиксов экрана."""
        return self.query_one("#apply-fixes", Button)
