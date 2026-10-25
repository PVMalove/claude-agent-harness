"""Экран Diagnostics: полный отчёт `harness health` и три действия — «Онлайн» (аналог запуска
`harness health --online`), «Исправить» (аналог запуска `harness health --fix`, применяющий только
внутрипроцессные фиксеры FIXERS после явного подтверждения) и «Экспорт» (экспорт отчёта в Markdown).
Каждое действие отображает эквивалент команды CLI, а сбор данных выполняется в фоновом потоке.

Команды из блока «Как исправить» предназначены для ручного выполнения разработчиком и только отображаются
на экране: они изменяют настройки машины или удаляют данные, что исключено из автоматического применения.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Callable, Protocol

from rich.text import Text
from textual import events
from textual.message import Message
from textual.widget import Widget
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Collapsible, Static

from ..crumbs import Crumb
from ..shell import (
    SHELL_CSS,
    ConsoleShell,
    Fold,
    JumpLink,
    SectionCard,
    WrappedValue,
    check_route,
)
from ..logtext import colorize_log
from .. import brand, i18n
from .. import data as console_data
from ...health.model import CheckResult, Report
from ...health.render import GROUP_LABELS_RU
from ..export import MarkdownDocument, MarkdownSection
from .commands import call_from_worker
from .export import EXPORT_BINDING_KEY, export_document

_STATUS_MARKERS = {"ok": "[OK]", "warn": "[WARN]", "fail": "[FAIL]", "skipped": "-"}
_APPLY_LABEL = "Исправить"
_CONFIRM_LABEL = "Подтвердить фиксы"
_MANUAL_NOTE = "Команды «Как исправить» пульт не выполняет — запустите их вручную."
_RUNNING = "health выполняется…"
_CORE_LOCK = threading.Lock()


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


class _CheckCard(SectionCard):
    """Real health data stays literal; only labels belong to the interface."""

    def __init__(self, check: CheckResult, index: int) -> None:
        self.check = check
        super().__init__(title=check.id, id=f"attention-{index}", classes="check-card")

    def compose(self) -> ComposeResult:
        yield Static(
            _icon(self.check.status) + Text(f"{self.check.id} · {self.check.message}"),
            classes="check-message no-i18n",
        )
        yield Static("", classes="check-remedy no-i18n", markup=False)
        yield Static(
            self.check.fix.command if self.check.fix and self.check.fix.command else "",
            classes="check-command no-i18n",
            markup=False,
        )
        with Horizontal(classes="check-links"):
            if self.check.fix and self.check.fix.command:
                yield Button(
                    "Копировать команду", id=f"copy-command-{self.id}", compact=True
                )
            yield JumpLink(check_route(self.check.id).section)

    def relabel(self) -> None:
        language = getattr(self.app, "language", "ru")
        # IDs, fixes and commands are report data, never translated by the shell.
        self.border_title = self.check.id
        if not self.query(".check-remedy"):
            return
        fix = self.check.fix
        text = (
            i18n.tr(language, "Как исправить:") + " " + fix.text
            if fix
            else i18n.tr(language, "Рекомендация отсутствует")
        )
        self.query_one(".check-remedy", Static).update(text)
        self.query_one(".check-command", Static).update(
            fix.command
            if fix and fix.command
            else i18n.tr(language, "Команда отсутствует — проверьте настройки вручную")
        )


class DiagnosticsScreen(ConsoleShell):
    """Экран диагностики состояния репозитория с возможностью запуска онлайн-проверок и применения исправлений."""

    CRUMBS = "Главная › Диагностика"
    SECTION = "Diagnostics"
    NARROW_FOOTER_KEYS = ("escape", "e", "f2")
    AUTO_FOCUS = "#diagnostics-scroll"
    CSS = SHELL_CSS + brand.shell_css("""
    DiagnosticsScreen #diagnostics-scroll { height: 1fr; padding: 0 1; }
    DiagnosticsScreen #diagnostics-heading { height: 1; text-style: bold; }
    DiagnosticsScreen #diagnostics-manual { height: auto; color: $text-muted; }
    DiagnosticsScreen #diagnostics-overview { height: auto; }
    DiagnosticsScreen #diagnostics-summary { height: 1; }
    DiagnosticsScreen #diagnostics-actions { height: 1; }
    DiagnosticsScreen #diagnostics-actions Button { width: auto; min-width: 10; height: 1; margin-right: 1; }
    DiagnosticsScreen .action-cli { height: auto; color: $text-muted; }
    DiagnosticsScreen #diagnostics-report { height: auto; }
    DiagnosticsScreen #diagnostics-attention { height: auto; max-height: 17; }
    DiagnosticsScreen #attention-cards { layout: grid; grid-size: 2; grid-columns: 1fr 1fr; grid-rows: auto; height: auto; }
    DiagnosticsScreen .check-card { height: auto; }
    DiagnosticsScreen .check-card Static { height: auto; }
    DiagnosticsScreen .check-links { height: 1; }
    DiagnosticsScreen .check-links Button { height: 1; width: auto; min-width: 0; margin-right: 1; }
    DiagnosticsScreen #diagnostics-filters { height: 1; }
    DiagnosticsScreen #groups-heading { width: 1fr; }
    DiagnosticsScreen #diagnostics-filters Button { height: 1; min-width: 7; width: auto; margin-right: 1; }
    DiagnosticsScreen #diagnostics-groups { height: auto; }
    DiagnosticsScreen #remaining-groups { border: none; padding: 0; }
    DiagnosticsScreen #remaining-groups > Contents { padding: 0; }
    DiagnosticsScreen #remaining-groups > CollapsibleTitle { display: none; }
    DiagnosticsScreen.-narrow #diagnostics-manual, DiagnosticsScreen.-narrow .action-cli { display: none; }
    DiagnosticsScreen.-narrow #diagnostics-attention { max-height: 6; }
    DiagnosticsScreen.-narrow #attention-cards { grid-size: 1; grid-columns: 1fr; }
    DiagnosticsScreen.-narrow #remaining-groups { border: round $border-subtle; padding: 0 1; }
    DiagnosticsScreen.-narrow #remaining-groups > CollapsibleTitle { display: block; }
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
        self._generation = 0
        self._cancel = threading.Event()
        self._disposed = False
        self._state = "running"
        self._language = "ru"
        self._filter = "all"
        self._failure = ""

    def compose_strip(self) -> ComposeResult:
        yield WrappedValue(str(self.repo))

    def compose_content(self) -> ComposeResult:
        """Summary, actions, attention cards and all checks in report order."""
        with VerticalScroll(id="diagnostics-scroll"):
            yield Static("Диагностика", id="diagnostics-heading")
            yield Static(_MANUAL_NOTE, id="diagnostics-manual")
            with SectionCard(title="Health-отчёт", id="diagnostics-overview"):
                yield Static("", id="diagnostics-summary", classes="no-i18n")
                with Horizontal(id="diagnostics-actions"):
                    yield Button("Офлайн", id="offline-checks", compact=True)
                    yield Button("Онлайн", id="online-checks", compact=True)
                    yield Button(_APPLY_LABEL, id="apply-fixes", compact=True)
                    yield Button("Экспорт", id="export", compact=True)
                yield Static(
                    "",
                    id="offline-checks-cli",
                    classes="action-cli no-i18n",
                    markup=False,
                )
                yield Static(
                    "",
                    id="online-checks-cli",
                    classes="action-cli no-i18n",
                    markup=False,
                )
                yield Static(
                    "", id="apply-fixes-cli", classes="action-cli no-i18n", markup=False
                )
            yield Static(
                _RUNNING, id="diagnostics-report", classes="no-i18n", markup=False
            )
            with VerticalScroll(id="diagnostics-attention"):
                yield Static("Требует исправления", id="attention-heading")
                yield Grid(id="attention-cards")
            with Horizontal(id="diagnostics-filters"):
                yield Static("Проверки по группам", id="groups-heading")
                for status, label in (
                    ("all", "Все"),
                    ("fail", "✖"),
                    ("warn", "⚠"),
                    ("skipped", "○"),
                ):
                    yield Button(
                        label, id=f"filter-{status}", compact=True, classes="no-i18n"
                    )
            yield Vertical(id="diagnostics-groups")

    def on_mount(self) -> None:
        """Запускает первичное построение отчёта проверок при монтировании экрана."""
        self._language = getattr(self.app, "language", "ru")
        self.app.message_signal.subscribe(self, self._observe_input, immediate=True)
        self._run_health(lambda: self._collect_diagnostics(self.repo))

    def _observe_input(self, message: Message) -> None:
        if not self._confirming_apply:
            return
        button = self._apply_button()
        if isinstance(message, events.Key):
            if message.key in ("enter", "space") and button.has_focus:
                return
            self._reset_apply()
        elif isinstance(message, events.MouseDown):
            if button.region.contains(message.screen_x, message.screen_y):
                return
            self._reset_apply()
        elif isinstance(message, (events.MouseScrollUp, events.MouseScrollDown)):
            self._reset_apply()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Обрабатывает нажатия кнопок экрана (онлайн-проверки, применение фиксов, экспорт)."""
        if event.button.id != "apply-fixes":
            self._reset_apply()
        button_id = event.button.id or ""
        if button_id.startswith("filter-"):
            self._filter = button_id.removeprefix("filter-")
            self._render_groups()
            return
        if button_id.startswith("copy-command-"):
            card = next(
                (
                    item
                    for item in self.query(_CheckCard)
                    if f"copy-command-{item.id}" == button_id
                ),
                None,
            )
            if card is not None and card.check.fix and card.check.fix.command:
                self.app.copy_to_clipboard(card.check.fix.command)
                self.notify(i18n.tr(self._language, "Команда скопирована"))
            return
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
        else:
            ConsoleShell.on_button_pressed(self, event)

    async def action_section(self, index: int) -> None:
        self._reset_apply()
        await ConsoleShell.action_section(self, index)

    async def navigate_crumb(self, crumb: Crumb) -> None:
        self._reset_apply()
        await ConsoleShell.navigate_crumb(self, crumb)

    async def action_back(self) -> None:
        self._reset_apply()
        await ConsoleShell.action_back(self)

    def action_export(self) -> None:
        """Выполняет экспорт текущего отчёта health в файл Markdown."""
        self._reset_apply()
        if self._report is None:
            text = (
                "нечего экспортировать: отчёт ещё строится"
                if self._state == "running"
                else "нечего экспортировать: отчёт не построен"
            )
            self.notify(
                i18n.tr(getattr(self.app, "language", "ru"), text), severity="warning"
            )
            return
        export_document(self, self.repo, health_document(self._report))

    def _on_apply_fixes_pressed(self) -> None:
        """Обрабатывает нажатие кнопки применения фиксов с запросом подтверждения."""
        if self._report is None:
            text = (
                "Исправление недоступно: дождитесь отчёта"
                if self._state == "running"
                else "Исправление недоступно: повторите проверку"
            )
            self.notify(
                i18n.tr(getattr(self.app, "language", "ru"), text), severity="warning"
            )
            return
        if not self._confirming_apply:
            self._confirming_apply = True
            self._apply_button().label = i18n.tr(
                getattr(self.app, "language", "ru"), _CONFIRM_LABEL
            )
            return
        self._reset_apply()
        online = self._report.online
        self._run_health(lambda: self._apply_local_fixes(self.repo, online=online))

    def _reset_apply(self) -> None:
        """Сбрасывает состояние подтверждения кнопки применения фиксов к исходному."""
        self._confirming_apply = False
        self._apply_button().label = i18n.tr(
            getattr(self.app, "language", "ru"), _APPLY_LABEL
        )

    def sync_language(self) -> None:
        """Show the finished report in the current language."""
        ConsoleShell.sync_language(self)
        language = getattr(self.app, "language", "ru")
        if language != self._language:
            self._language = language
            self._reset_apply()
        self._render_overview()
        self._render_state()
        for card in self.query(_CheckCard):
            card.relabel()
        self._relabel_groups()

    def on_unmount(self) -> None:
        """Отменяет фоновые воркеры при размонтировании экрана."""
        self._disposed = True
        self._generation += 1
        self._cancel.set()
        self.app.message_signal.unsubscribe(self)
        self.workers.cancel_node(self)

    def _run_health(self, run: Callable[[], Report]) -> None:
        """Запускает процедуру проверки health в фоновом потоке worker."""
        self._cancel.set()
        self._cancel = cancel = threading.Event()
        self._generation += 1
        generation = self._generation
        self._state = "running"
        self._report = None
        self._reset_apply()
        self.query_one("#attention-cards").remove_children()
        self.query_one("#diagnostics-groups").remove_children()
        self._render_overview()
        self._render_state()

        def current() -> bool:
            return (
                not (cancel.is_set() or self._disposed)
                and generation == self._generation
                and self.is_mounted
            )

        def publish(report: Report | None, failure: str = "") -> None:
            if current():
                if report is None:
                    self._show_failure(failure)
                else:
                    self._show(report)

        def work() -> None:
            while not cancel.is_set():
                if _CORE_LOCK.acquire(timeout=0.05):
                    break
            else:
                return
            report = None
            failure = ""
            try:
                if cancel.is_set():
                    return
                report = run()
            except Exception as exc:
                failure = repr(exc)
            finally:
                _CORE_LOCK.release()
            if current():
                call_from_worker(self, lambda: publish(report, failure))

        self.run_worker(work, thread=True, exclusive=True, group="diagnostics")

    def _show_failure(self, text: str) -> None:
        """Отображает причину, по которой отчёт health не построен."""
        self._state = "failed"
        self._report = None
        self._failure = text
        self._render_overview()
        self._render_state()

    def _show(self, report: Report) -> None:
        """Publish only actual health facts, with separate cards and complete groups."""
        self._state = "ready"
        self._report = report
        self._render_overview()
        self._render_state()
        attention = [
            check for check in report.checks if check.status in ("fail", "warn")
        ]
        self._replace_contents(
            "#attention-cards",
            *(_CheckCard(check, index) for index, check in enumerate(attention)),
        )
        self.query_one("#diagnostics-attention").display = bool(attention)
        self._render_groups()

    def _render_state(self) -> None:
        text = _RUNNING if self._state == "running" else ""
        if self._state == "failed":
            text = f"health не выполнен: {self._failure}\nОтчёт не построен. Повторить: «Офлайн». Если повторяется — «Справка» (9)."
        elif self._report and self._report.fixes_applied:
            text = "Применённые фиксы:\n" + "\n".join(self._report.fixes_applied)
        self.query_one("#diagnostics-report", Static).update(text)
        self.query_one("#diagnostics-report").display = bool(text)
        if self._report is None:
            self.query_one("#diagnostics-attention").display = False

    def _render_overview(self) -> None:
        report = self._report
        summary = Text()
        if report is not None:
            counts = report.summary()
            for status, label in (
                ("ok", "успешно"),
                ("warn", "предупреждения"),
                ("fail", "ошибки"),
                ("skipped", "не проверено"),
            ):
                summary.append_text(_icon(status, muted=counts[status] == 0))
                summary.append(f"{counts[status]} {label}  ")
            total = len(report.checks)
            summary.append(f"· проверено {total - counts['skipped']} из {total}")
        self.query_one("#diagnostics-summary", Static).update(summary)
        self.query_one("#diagnostics-summary").display = report is not None
        self.query_one("#offline-checks-cli", Static).update(
            f"$ {console_data.health_cli_line(self.repo)} · ○ не проверено ≠ успешно"
        )
        self.query_one("#online-checks-cli", Static).update(
            f"$ {console_data.health_cli_line(self.repo, '--online')}"
        )
        flags = ("--online", "--fix") if report and report.online else ("--fix",)
        self.query_one("#apply-fixes-cli", Static).update(
            f"$ {console_data.health_cli_line(self.repo, *flags)}"
        )
        for status in ("all", "fail", "warn", "skipped"):
            label = (
                "Все"
                if status == "all"
                else f"{brand.STATUS_ICONS[status]} {report.summary()[status] if report else 0}"
            )
            button = self.query_one(f"#filter-{status}", Button)
            button.label = label
            button.variant = "primary" if self._filter == status else "default"
        mode = "онлайн" if report and report.online else "офлайн"
        self.query_one(WrappedValue).set_value(f"{self.repo} · режим: {mode}")

    def _group_text(self, checks: list[CheckResult]) -> Text:
        text = Text()
        for check in checks:
            text.append_text(_icon(check.status))
            text.append(f"{check.id}: {check.message}\n")
            if check.fix:
                text.append(f"Как исправить: {check.fix.text}\n")
                if check.fix.command:
                    text.append(f"{check.fix.command}\n")
        return text

    def _replace_contents(self, selector: str, *widgets: Widget) -> None:
        async def replace() -> None:
            container = self.query_one(selector)
            await container.remove_children()
            if self.is_mounted and not self._disposed:
                await container.mount(*widgets)

        self.run_worker(replace(), group=f"render-{selector}", exclusive=True)

    def _render_groups(self) -> None:
        self._render_overview()
        if self._report is None:
            return
        groups: dict[str, list[CheckResult]] = {}
        indices: dict[str, int] = {}
        for check in self._report.checks:
            indices.setdefault(check.group, len(indices))
            if self._filter == "all" or check.status == self._filter:
                groups.setdefault(check.group, []).append(check)
        folds = []
        for group, checks in groups.items():
            counts = Report(1, "", False, checks).summary()
            summary = " · ".join(
                f"{brand.STATUS_ICONS[status]} {count}"
                for status, count in counts.items()
                if count
            )
            fold = Fold(
                Static(self._group_text(checks), classes="group-checks no-i18n"),
                title=GROUP_LABELS_RU.get(group, group),
                summary=summary,
                id=f"check-group-{indices[group]}",
            )
            folds.append(fold)
        if not folds:
            self._replace_contents(
                "#diagnostics-groups",
                Static(
                    "Нет проверок для этого фильтра", classes="no-i18n", markup=False
                ),
            )
            return
        remaining = Fold(
            *folds[1:],
            title="Остальные группы",
            summary=str(len(folds) - 1),
            collapsed=self.app.size.width < brand.NARROW_WIDTH,
            id="remaining-groups",
        )
        remaining.display = len(folds) > 1
        self._replace_contents("#diagnostics-groups", folds[0], remaining)

    def _relabel_groups(self) -> None:
        if self._report is None:
            return
        groups: dict[str, list[CheckResult]] = {}
        for check in self._report.checks:
            groups.setdefault(check.group, []).append(check)
        for index, checks in enumerate(groups.values()):
            matches = self.query(f"#check-group-{index} .group-checks")
            if matches:
                matches.first(Static).update(
                    self._group_text(
                        [
                            check
                            for check in checks
                            if self._filter == "all" or check.status == self._filter
                        ]
                    )
                )

    def on_collapsible_toggled(self, event: Collapsible.Toggled) -> None:
        self._reset_apply()

    def on_resize(self, event: events.Resize) -> None:
        ConsoleShell.on_resize(self, event)
        matches = self.query("#remaining-groups")
        if matches:
            matches.first(Fold).collapsed = event.size.width < brand.NARROW_WIDTH

    def _apply_button(self) -> Button:
        """Возвращает кнопку применения фиксов экрана."""
        return self.query_one("#apply-fixes", Button)
