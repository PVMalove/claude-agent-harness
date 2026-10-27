"""Live local worker view in the shared Harness console; observation never prompts a model."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Callable

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import (
    Button,
    ListItem,
    ListView,
    RichLog,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

from harness.orchestration.core.utils import JsonObject
from .. import brand, i18n, reports
from ..chrome import TopBar
from ..runner import CommandRunner
from ..session_card import session_card, status_caption
from ..status import PERMISSION_PENDING, process_status, session_status
from ..crumbs import Crumb
from ..shell import (
    HOME_SECTION,
    SHELL_CSS,
    Breadcrumbs,
    ConsoleShell,
    ShellIds,
    WrappedValue,
)
from ..translate import Translated
from ..wrap import wrap_value
from .commands import call_from_worker

Observer = Callable[[Path, str, int], JsonObject]


def observe(repo: Path, dispatch: str, offset: int) -> JsonObject:
    from harness.orchestration.workflow.delivery import observe_dispatch

    return observe_dispatch(
        argparse.Namespace(
            repo=str(repo), state_dir=None, dispatch=dispatch, offset=offset
        )
    )


class WorkerDetails(Translated, ModalScreen[None]):
    BINDINGS = [Binding("escape", "dismiss", "Назад")]
    DEFAULT_CSS = """
    WorkerDetails { align: center middle; }
    WorkerDetails > Vertical { width: 90%; height: 80%; border: round $primary; padding: 1; background: $background; }
    WorkerDetails VerticalScroll { height: 1fr; }
    """

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical():
            with VerticalScroll():
                yield Static(self.text, markup=False, classes="no-i18n")
            yield Button("Назад", id="close-worker-details")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)


class NativePermissionDialog(Translated, ModalScreen[bool | None]):
    """One native operator response; cancelling leaves the request pending."""

    BINDINGS = [Binding("escape", "cancel", "Отмена")]
    DEFAULT_CSS = WorkerDetails.DEFAULT_CSS.replace(
        "WorkerDetails", "NativePermissionDialog"
    )

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Запрос разрешения native runtime", markup=False)
            with VerticalScroll():
                yield Static(self.text, markup=False, classes="no-i18n")
            with Horizontal(classes="actions"):
                yield Button("Отклонить", id="native-deny")
                yield Button("Разрешить один раз", id="native-allow", variant="primary")
                yield Button("Отмена", id="native-cancel")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(
            True
            if event.button.id == "native-allow"
            else False
            if event.button.id == "native-deny"
            else None
        )


class WorkerObservationScreen(ConsoleShell):
    """A passive view. Closing/reconnecting never starts or terminates a worker."""

    SECTION = "Orchestration"
    IS_SECTION_ROOT = False
    CRUMBS = "Главная › Оркестрация › Worker / Наблюдение"
    HAS_CONTEXT = True
    IDS = ShellIds(
        navigation="worker-navigation",
        menu="worker-sections",
        content="worker-content",
        breadcrumbs="worker-breadcrumbs",
        toggle="worker-menu",
        body="worker-columns",
        context="worker-context",
    )
    # Four panes (menu, sessions, stream, context) need more room than the shell's default: below
    # 140 columns the context folds into a collapsible block under the stream.
    CONTEXT_SIDE_MIN = 140
    CONTEXT_TITLE = "Контекст запуска"
    BINDINGS = [
        Binding("r", "refresh", "Обновить"),
        *(Binding(f"alt+{i+1}", f"select_session({i})", show=False) for i in range(3)),
    ]
    DEFAULT_CSS = brand.shell_css("""
    WorkerObservationScreen #worker-panes { height: 1fr; }
    WorkerObservationScreen #worker-sessions { width: 27; height: 1fr; background: transparent; border-right: solid $border-subtle; padding: 1; }
    WorkerObservationScreen #worker-session-count { height: 2; padding: 0 1; color: $text-muted; }
    WorkerObservationScreen #worker-picker { height: 1fr; border: none; margin: 0; padding: 0; }
    WorkerObservationScreen #worker-picker > ListItem { height: auto; min-height: 7; padding: 1; border-left: solid transparent; }
    WorkerObservationScreen #worker-picker > ListItem.-highlight, WorkerObservationScreen #worker-picker:focus > ListItem.-highlight { background: $row-highlight; border-left: solid $selection-edge; text-style: none; }
    WorkerObservationScreen #worker-picker Static { height: auto; }
    WorkerObservationScreen #worker-list-foot { height: auto; border-top: solid $border-subtle; padding: 1; color: $text-muted; }
    WorkerObservationScreen #worker-main { width: 1fr; min-width: 0; height: 1fr; }
    WorkerObservationScreen #worker-heading { height: auto; min-height: 4; padding: 1 2 0 2; }
    WorkerObservationScreen #worker-title { width: 1fr; height: 2; text-style: bold; }
    WorkerObservationScreen #worker-badge { width: auto; color: $text-muted; }
    WorkerObservationScreen #worker-subtitle { height: 1; padding: 0 2; color: $text-muted; }
    WorkerObservationScreen #worker-pipeline { height: 2; padding: 0 2; }
    WorkerObservationScreen #worker-status { height: auto; max-height: 5; padding: 0 2; border-top: solid $border-subtle; border-bottom: solid $border-subtle; color: $text-muted; }
    WorkerObservationScreen #worker-tabs { height: 1fr; }
    WorkerObservationScreen TabPane { padding: 0; }
    WorkerObservationScreen ContentSwitcher { height: 1fr; }
    WorkerObservationScreen Tabs { padding: 0 2; }
    WorkerObservationScreen #worker-output, WorkerObservationScreen #worker-events { height: 1fr; border: none; margin: 0; padding: 1 2; }
    WorkerObservationScreen #worker-filter { height: 3; margin: 0 1; }
    WorkerObservationScreen #worker-access-scroll { height: 1fr; padding: 1 2; }
    WorkerObservationScreen #worker-access-facts { height: auto; }
    WorkerObservationScreen #worker-actions { height: 4; padding: 0 2; border-top: solid $border-subtle; overflow-x: auto; }
    WorkerObservationScreen #worker-actions Button { margin-right: 1; min-width: 5; }
    WorkerObservationScreen #worker-stop { color: $error; }
    WorkerObservationScreen #worker-cost { height: 1; padding: 0 2; color: $text-muted; }
    WorkerObservationScreen #worker-switch { display: none; }
    WorkerObservationScreen.-narrow #worker-switch { display: block; }
    WorkerObservationScreen.-narrow #worker-sessions { display: none; }
    WorkerObservationScreen.-narrow.-sessions-open #worker-sessions { display: block; }
    WorkerObservationScreen.-narrow #worker-pipeline { height: 1; }
    WorkerObservationScreen.-narrow #worker-heading { height: 1; min-height: 1; padding: 0 2; }
    WorkerObservationScreen.-narrow #worker-title { height: 1; }
    WorkerObservationScreen.-narrow #worker-cost { display: none; }
    WorkerObservationScreen.-narrow #worker-output, WorkerObservationScreen.-narrow #worker-events { padding: 0 2; }
    WorkerObservationScreen.-narrow #worker-badge { max-width: 30; }
    WorkerObservationScreen.-focus #worker-navigation, WorkerObservationScreen.-focus #worker-sessions, WorkerObservationScreen.-focus #worker-context { display: none; }
    """)
    # The shell's rules plus this screen's own, which must outrank the app's generic widget rules.
    CSS = SHELL_CSS + brand.shell_css("""
    .shell #worker-sessions { background: transparent; }
    .shell #worker-picker, .shell #worker-picker:focus { border: none; background: transparent; }
    .shell #worker-picker > ListItem.-highlight, .shell #worker-picker:focus > ListItem.-highlight {
        background: $row-highlight; color: $foreground; text-style: none;
    }
    .shell #worker-actions Button { border: round $component-border; color: $foreground; }
    .shell #worker-actions Button:hover, .shell #worker-actions Button:focus { border: round $primary; color: $primary; }
    .shell #worker-actions #worker-stop { color: $error; }
    .shell.-wide #worker-actions { height: 2; padding: 0 1; }
    .shell.-wide #worker-actions Button, .shell.-wide #worker-actions Button:hover, .shell.-wide #worker-actions Button:focus { border: none; height: 1; min-width: 0; padding: 0; margin-right: 0; }
    .shell.-wide #worker-actions Button:focus { background: $selection-background; color: $selection-foreground; }
    """)

    def __init__(
        self, repo: Path, dispatch: str | None = None, *, observer: Observer = observe
    ) -> None:
        super().__init__()
        self.repo = repo
        self.dispatch = dispatch
        self.observer = observer
        self._stream_offset = 0
        self.snapshot: JsonObject = {}
        self._events: list[JsonObject] = []
        self._last_operation = "—"
        self._polling = False
        view = reports.load_ledger_view(repo)
        ordered = sorted(view.stages, key=lambda stage: stage.at, reverse=True)
        ordered.sort(key=lambda stage: stage.finished)
        self._stages = {stage.dispatch_id: stage for stage in ordered}
        self._batches = {batch.batch_id for batch in view.batches}
        self._choices = [
            (f"{stage.ticket} · {stage.role}", stage.dispatch_id) for stage in ordered
        ]
        if dispatch and not any(value == dispatch for _, value in self._choices):
            self._choices.insert(0, (dispatch, dispatch))
        if not dispatch and self._choices:
            self.dispatch = self._choices[0][1]
        self._timer: Timer | None = None

    def _session_card(self, label: str, dispatch_id: str) -> Text:
        stage = self._stages.get(dispatch_id)
        language = getattr(self.app, "language", "ru")
        if stage is None:
            card = Text(
                wrap_value(label, 18) + "\n\n", style=brand.PALETTE["foreground"]
            )
            return card.append_text(status_caption(session_status("unknown"), language))
        return session_card(
            ticket=stage.ticket,
            role=stage.role,
            dispatch_id=dispatch_id,
            state=stage.state,
            language=language,
            id_limit=22,
            stacked=True,
        )

    def compose_strip(self) -> ComposeResult:
        yield Button("Сессии", id="worker-switch", compact=True)
        yield WrappedValue("Выбранный batch / dispatch", id="worker-selection")
        yield Button("История batch →", id="worker-recovery", compact=True)

    def compose_content(self) -> ComposeResult:
        with Horizontal(id="worker-panes"):
            with Vertical(id="worker-sessions"):
                yield Static(
                    f"РАБОЧИЕ СЕССИИ   {len(self._choices)}",
                    id="worker-session-count",
                )
                yield ListView(
                    *(
                        ListItem(
                            Static(
                                self._session_card(label, value),
                                classes="no-i18n",
                            ),
                            name=value,
                        )
                        for label, value in self._choices
                    ),
                    id="worker-picker",
                )
                yield Static(
                    "Каждый worker — отдельная сессия\n\nВыбор сессии не запускает новый процесс.",
                    id="worker-list-foot",
                )
            with Vertical(id="worker-main"):
                with Horizontal(id="worker-heading"):
                    yield Static(
                        "Наблюдение за работой",
                        id="worker-title",
                        classes="no-i18n",
                        markup=False,
                    )
                    yield Static(
                        "○ Ожидание",
                        id="worker-badge",
                        classes="no-i18n",
                        markup=False,
                    )
                yield Static(
                    "Выберите рабочую сессию",
                    id="worker-subtitle",
                    markup=False,
                    classes="no-i18n",
                )
                yield Static(
                    "architect  →  developer  →  review  →  QA  →  publish",
                    id="worker-pipeline",
                    classes="no-i18n",
                )
                yield Static(
                    "○ Ожидание наблюдения",
                    id="worker-status",
                    markup=False,
                    classes="no-i18n",
                )
                with TabbedContent(id="worker-tabs"):
                    with TabPane("Сессия", id="session"):
                        yield RichLog(
                            id="worker-output",
                            max_lines=500,
                            min_width=1,
                            wrap=True,
                            markup=False,
                            highlight=False,
                        )
                    with TabPane("События", id="events"):
                        yield Select(
                            [
                                ("Все события", "all"),
                                ("Вывод", "output"),
                                ("Инструменты", "tool"),
                                ("Ошибки", "error"),
                                ("Разрешения", "permission"),
                            ],
                            value="all",
                            allow_blank=False,
                            id="worker-filter",
                        )
                        yield RichLog(
                            id="worker-events",
                            max_lines=500,
                            min_width=1,
                            wrap=True,
                            markup=False,
                            highlight=False,
                        )
                    with TabPane("Доступы", id="access"):
                        with VerticalScroll(id="worker-access-scroll"):
                            yield WrappedValue(
                                "Данные проверки ещё не получены",
                                id="worker-access-facts",
                            )
                            yield Button(
                                "Запрос native",
                                id="worker-permission-inline",
                                disabled=True,
                            )
                with Horizontal(id="worker-actions"):
                    yield Button("Развернуть сессию", id="worker-focus")
                    yield Button("Детали", id="worker-details")
                    stop = Button("□ Остановить", id="worker-stop", disabled=True)
                    stop.tooltip = "Этот адаптер не предоставляет подтверждённый канал остановки. Закрытие панели не останавливает worker."
                    yield stop
                    yield Button("↻", id="worker-reconnect")
                yield Static(
                    "Наблюдение · без дополнительных LLM-вызовов",
                    id="worker-cost",
                )

    def compose_context(self) -> ComposeResult:
        yield Static("◉ КОНТЕКСТ ЗАПУСКА", classes="context-label")
        yield Button(
            "Запрос native",
            id="worker-permission",
            disabled=True,
            compact=True,
        )
        yield WrappedValue(
            "Ожидание данных сессии", id="worker-context-facts", classes="context-value"
        )
        yield Static("ГОТОВНОСТЬ СРЕДЫ", classes="context-label")
        yield Static(
            "? Не проверено",
            id="worker-readiness",
            classes="context-value no-i18n",
            markup=False,
        )
        yield Button("Проверка доступов", id="worker-access", compact=True)
        yield Static(
            "Готовность проверяется в контексте инструментов worker, а не только процесса запуска.",
            classes="context-value",
        )
        yield Static(
            "NATIVE PERMISSIONS\nРазрешения и hooks действуют. Монитор не выдаёт права.",
            classes="context-note",
        )
        yield Static(
            "НАБЛЮДЕНИЕ · БЕЗ LLM\nСобытия runtime → консоль. Без дополнительных запросов к модели.",
            classes="context-note",
        )

    def on_mount(self) -> None:
        if self._choices:
            self.query_one("#worker-picker", ListView).index = next(
                (
                    i
                    for i, (_, value) in enumerate(self._choices)
                    if value == self.dispatch
                ),
                0,
            )
        self._timer = self.set_interval(1, self.action_refresh)
        self.action_refresh()
        self.translate_now()

    def on_unmount(self) -> None:
        if self._timer:
            self._timer.stop()
        # Only local observation jobs are cancelled by Textual. The external runtime is never
        # owned by this screen and keeps running when the screen closes.

    def action_refresh(self) -> None:
        if self._polling or not self.dispatch:
            return
        self._polling = True
        dispatch, offset = self.dispatch, self._stream_offset

        def poll() -> None:
            try:
                snapshot = self.observer(self.repo, dispatch, offset)
            except Exception:
                snapshot = {"dispatch_id": dispatch, "observation_error": True}
            call_from_worker(self, lambda: self._render_snapshot(snapshot, dispatch))

        self.run_worker(poll, thread=True, group="observation", exit_on_error=False)

    def _render_snapshot(self, snapshot: JsonObject, dispatch: str) -> None:
        self._polling = False
        if dispatch != self.dispatch:
            return
        language = getattr(self.app, "language", "ru")

        def t(text: str) -> str:
            return i18n.tr_text(language, text)

        if snapshot.get("observation_error"):
            self.query_one("#worker-status", Static).update(
                t(
                    "⚠ Наблюдение недоступно; исход процесса неизвестен. Повторный worker не запускается."
                )
            )
            return
        self.snapshot = snapshot
        for button_id in ("worker-permission", "worker-permission-inline"):
            self.query_one(f"#{button_id}", Button).disabled = not bool(
                snapshot.get("permission_requests")
            )
        self.query_one("#worker-permission", Button).display = bool(
            snapshot.get("permission_requests")
        )
        startup = snapshot.get("startup", {})
        observation = startup.get("observation")
        worker = (
            observation.get("state", "unknown")
            if isinstance(observation, dict)
            else startup.get("worker_state", "unknown")
        )
        if worker in {"reported", "rejected-at-startup"}:
            worker = "unknown"  # Lifecycle evidence does not prove process termination.
        staged = snapshot.get("report_staged") and not snapshot.get("report_recorded")
        for event in snapshot.get("events", []):
            if event.get("kind") in {"tool", "output", "permission", "error"}:
                self._last_operation = str(event.get("text") or "—").splitlines()[0][
                    :100
                ]
        glyph = {"live": "●", "stopped": "■", "absent": "○"}.get(worker, "?")
        age = snapshot.get("last_event_age_seconds")
        lines = [
            f"{snapshot.get('batch_id')} / {dispatch} · {snapshot.get('role')}",
            f"{glyph} {t('Процесс')}: {worker} · ledger: {snapshot.get('state')} · {t('Поток')}: {snapshot.get('stream_state')}",
            f"heartbeat: {snapshot.get('heartbeat_at') or t('не наблюдался')} · {t('Событие')}: {str(age) + 's' if age is not None else '—'}",
            f"{t('Операция')}: {self._last_operation}",
        ]
        exit_evidence = snapshot.get("runtime_exit")
        if isinstance(exit_evidence, dict):
            lines.append(
                f"exit: {exit_evidence['exit_code']} · {snapshot.get('access_mode', 'inherit')}"
            )
        if snapshot.get("permission_requests"):
            lines.append(
                t("⚠ Требуется разрешение native runtime; решение принимает человек.")
            )
        failure = snapshot.get("failure")
        if isinstance(failure, dict):
            lines.append(
                "⚠ "
                + str(
                    failure.get("error") or failure.get("kind") or "refused"
                ).splitlines()[0][:140]
            )
        if staged:
            lines.append(
                t(
                    "⚠ Отчёт ожидает доставки; завершение процесса не означает приёмку работы."
                )
            )
        if startup.get("deadline_exceeded"):
            lines.append(
                t("⚠ Истёк срок первого self-report; таймаут не доказывает остановку.")
            )
        if snapshot.get("stream_state") in {"unavailable", "waiting"}:
            lines.append(t("○ Нет новых событий; это не проверка здоровья worker."))
        self._paint_context(snapshot, worker, lines)
        self.query_one("#worker-status", Static).update(
            "\n".join(lines[1:3] + lines[4:])
        )
        output = self.query_one("#worker-output", RichLog)
        if snapshot.get("reset"):
            output.write(
                Text(
                    t(
                        "⚠ Поток был сокращён или перезаписан; показана доступная история."
                    )
                )
            )
        for event in snapshot.get("events", []):
            self._events.append(event)
            if self._event_visible(event):
                output.write(self._transcript(event))
        self._events = self._events[-500:]
        events_log = self.query_one("#worker-events", RichLog)
        events_log.clear()
        for row in self._events:
            if self._event_visible(row):
                events_log.write(
                    Text(f"[{row.get('kind', 'event')}] {row.get('text', '')}")
                )
        self._stream_offset = snapshot.get("next_offset", self._stream_offset)
        bar = self.query_one(TopBar)
        bar.crumbs = f"Главная › Оркестрация › {snapshot.get('batch_id')} › Сессии"
        bar.refresh_status()

    def _transcript(self, event: JsonObject) -> Text:
        kind, text = event.get("kind", "event"), str(event.get("text", ""))
        row = Text()
        if kind in {"tool", "tool-result"}:
            row.append(
                "  —   │ " + text.replace("\n", "\n      │ ") + "\n",
                style=f"{brand.PALETTE['foreground']} on {brand.SHELL_TOKENS['event-background']}",
            )
        elif kind == "output":
            row.append("  —   WORKER\n", style=brand.PALETTE["primary"])
            row.append("      " + text.replace("\n", "\n      ") + "\n\n")
        else:
            color = (
                "error"
                if kind == "error"
                else "warning"
                if kind == "permission"
                else "success"
                if kind == "result"
                else "muted"
            )
            row.append("  —   " + text + "\n\n", style=brand.PALETTE[color])
        return row

    def _worker_crumbs(self, snapshot: JsonObject) -> list[Crumb]:
        """The path with the observed batch, which links to that batch's timeline when there is one."""
        batch = str(snapshot.get("batch_id") or "—")
        return [
            Crumb("Главная", section=HOME_SECTION),
            Crumb("Оркестрация", section="Orchestration"),
            Crumb(
                brand.shorten(batch, 23),
                action="open_batch" if batch in self._batches else None,
            ),
            Crumb("Worker / Наблюдение"),
        ]

    def action_open_batch(self) -> None:
        from .reports import BatchTimelineScreen

        timeline = reports.load_ledger_view(self.repo).timeline(
            self.snapshot.get("batch_id", "")
        )
        if timeline:
            self.app.push_screen(BatchTimelineScreen(timeline, self.repo))

    def _paint_context(
        self, snapshot: JsonObject, worker: str, lines: list[str]
    ) -> None:
        language = getattr(self.app, "language", "ru")

        def t(value: str) -> str:
            return i18n.tr_text(language, value)

        ticket, role = snapshot.get("ticket") or "—", snapshot.get("role") or "—"
        status = session_status(snapshot.get("state", "unknown"))
        if snapshot.get("permission_requests"):
            status = PERMISSION_PENDING
        elif snapshot.get("state") in {"working", "dispatched"} and worker != "live":
            status = process_status(worker)
        self.query_one("#worker-title", Static).update(
            f"{ticket} · {t('Наблюдение за работой')}"
        )
        self.query_one("#worker-badge", Static).update(
            Text(status.render(language), style=status.color)
        )
        runtime = {"claude": "Claude Code", "codex": "Codex"}.get(
            str(snapshot.get("runtime") or ""),
            str(snapshot.get("runtime") or "runtime —"),
        )
        self.query_one("#worker-subtitle", Static).update(
            f"{role} / {runtime} / {brand.shorten(str(snapshot.get('dispatch_id') or ''), 18)}"
        )
        pipeline = Text()
        for i, stage in enumerate(
            ("architect", "developer", "review", "QA", "publish")
        ):
            if i:
                pipeline.append("  →  ", style=brand.PALETTE["muted"])
            active = stage == {"code-review": "review", "qa": "QA"}.get(role, role)
            pipeline.append(
                f" {stage} ",
                style=f"{brand.PALETTE['primary']} on {brand.SHELL_TOKENS['event-active-background']}"
                if active
                else brand.PALETTE["muted"],
            )
        self.query_one("#worker-pipeline", Static).update(pipeline)
        self.query_one(Breadcrumbs).set_crumbs(self._worker_crumbs(snapshot))
        self.query_one("#worker-selection", WrappedValue).set_value(
            f"{ticket} · {role} · {snapshot.get('dispatch_id') or ''}"
        )
        path = str(snapshot.get("worktree") or "—")
        failure = snapshot.get("failure")
        reason = (
            str(failure.get("error") or failure.get("kind") or "")
            if isinstance(failure, dict)
            else ""
        )
        facts = f"Dispatch\n{snapshot.get('dispatch_id') or '—'}\n\n"
        if reason:
            facts += f"Причина блокировки\n{reason}\n\n"
        facts += f"Runtime-сессия\n{snapshot.get('runtime_session_id') or t('не наблюдался')}\n\nWorktree\n{path}\n\nSnapshot\n{snapshot.get('snapshot_commit') or t('не наблюдался')}\n\nМодель / effort\n{snapshot.get('model') or '—'} / {snapshot.get('effort') or '—'}\n\nРежим запуска\n{snapshot.get('access_mode') or 'inherit'}"
        self.query_one("#worker-context-facts", WrappedValue).set_value(t(facts))
        model_report = snapshot.get("model_self_report") or {}
        tree = snapshot.get("worktree_attestation") or {}
        checks = Text()
        for index, (name, verified) in enumerate(
            (
                ("Checkout", tree.get("match")),
                ("Git metadata", None),
                ("Ledger / inbox", None),
                ("Self-report", model_report.get("match")),
            )
        ):
            if index:
                checks.append("\n")
            checks.append(name + "  ", style=brand.PALETTE["muted"])
            checks.append(
                "✓" if verified is True else "✖" if verified is False else "?",
                style=brand.PALETTE[
                    "success"
                    if verified is True
                    else "error"
                    if verified is False
                    else "muted"
                ],
            )
        self.query_one("#worker-readiness", Static).update(checks)
        self.query_one("#worker-access-facts", WrappedValue).set_value(
            t(facts)
            + "\n\n"
            + checks.plain
            + "\n\n"
            + "\n".join(lines)
            + "\n\n"
            + str((snapshot.get("failure") or {}).get("error", ""))
        )

    def sync_language(self) -> None:
        language = getattr(self.app, "language", "ru")
        select = self.query_one("#worker-filter", Select)
        selected = select.value
        select.set_options(
            (i18n.tr(language, label), value)
            for label, value in [
                ("Все события", "all"),
                ("Вывод", "output"),
                ("Инструменты", "tool"),
                ("Ошибки", "error"),
                ("Разрешения", "permission"),
            ]
        )
        select.value = selected
        for item in self.query("#worker-picker > ListItem").results(ListItem):
            if item.name:
                label = next(
                    (label for label, value in self._choices if value == item.name),
                    item.name,
                )
                item.query_one(Static).update(self._session_card(label, item.name))
        if self.snapshot:
            self._render_snapshot({**self.snapshot, "events": []}, self.dispatch or "")
        super().sync_language()

    def _event_visible(self, event: JsonObject) -> bool:
        selected = self.query_one("#worker-filter", Select).value
        return selected == "all" or str(event.get("kind", "")).startswith(str(selected))

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "worker-filter":
            output = self.query_one("#worker-output", RichLog)
            output.clear()
            events = self.query_one("#worker-events", RichLog)
            events.clear()
            for row in self._events:
                if self._event_visible(row):
                    output.write(self._transcript(row))
                    events.write(
                        Text(f"[{row.get('kind', 'event')}] {row.get('text', '')}")
                    )
            return

    def _select_dispatch(self, dispatch: str) -> None:
        if dispatch != self.dispatch:
            self.dispatch = dispatch
            self._stream_offset = 0
            self.snapshot = {}
            self._events = []
            self._last_operation = "—"
            self.query_one("#worker-output", RichLog).clear()
            self.query_one("#worker-events", RichLog).clear()
            self.action_refresh()

    def action_select_session(self, index: int) -> None:
        if index < len(self._choices):
            self.query_one("#worker-picker", ListView).index = index
            self._select_dispatch(self._choices[index][1])

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "worker-picker" and event.item.name:
            self._select_dispatch(event.item.name)

    async def action_section(self, index: int) -> None:
        # This screen is a view inside Orchestration: its menu entry still opens that section.
        await self.open_section(index)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "worker-focus":
            self.toggle_class("-focus")
            event.button.label = i18n.tr(
                getattr(self.app, "language", "ru"),
                "Свернуть сессию" if self.has_class("-focus") else "Развернуть сессию",
            )
        elif event.button.id == "worker-switch":
            self.remove_class("-menu-open")
            self.toggle_class("-sessions-open")
            if self.has_class("-sessions-open"):
                self.call_after_refresh(
                    self.query_one("#worker-picker", ListView).focus
                )
        elif event.button.id == "worker-access":
            self.query_one("#worker-tabs", TabbedContent).active = "access"
        elif event.button.id == "worker-reconnect":
            self.action_refresh()
        elif event.button.id == "worker-menu":
            self.remove_class("-sessions-open")  # the shell toggles the menu itself
        elif event.button.id in {"worker-permission", "worker-permission-inline"}:
            requests = self.snapshot.get("permission_requests", [])
            if not requests or not self.dispatch:
                return
            pending = requests[0]
            dispatch_id = self.dispatch
            description = f"{self.snapshot.get('batch_id')} / {dispatch_id}\n{self.snapshot.get('worktree')}\nRequest: {pending['request_id']}\nTool: {pending['tool_name']}\n{pending['input_preview']}"

            def answered(allow: bool | None) -> None:
                if allow is None:
                    return

                def send_response() -> None:
                    from harness.orchestration.workflow.delivery import (
                        respond_native_permission,
                    )
                    from harness.orchestration.core.utils import CoordinatorError

                    try:
                        respond_native_permission(
                            self.repo,
                            dispatch_id,
                            pending["request_id"],
                            pending["request_sha256"],
                            allow=allow,
                        )
                    except CoordinatorError as exc:
                        message = exc.message
                        call_from_worker(
                            self, lambda: self.notify(message, severity="error")
                        )
                    call_from_worker(self, self.action_refresh)

                self.run_worker(
                    send_response,
                    thread=True,
                    group="native-permission",
                    exit_on_error=False,
                )

            self.app.push_screen(NativePermissionDialog(description), answered)
        elif event.button.id == "worker-details":
            lines = [
                f"{key}: {self.snapshot.get(key)}"
                for key in (
                    "batch_id",
                    "dispatch_id",
                    "role",
                    "worktree",
                    "model",
                    "runtime",
                    "effort",
                    "snapshot_commit",
                    "worktree_attestation",
                    "access_mode",
                    "runtime_exit",
                    "failure",
                    "launch_id",
                    "runtime_session_id",
                    "heartbeat_at",
                    "log_path",
                    "report_staged",
                    "report_recorded",
                )
            ]
            lines.append(
                "Stop/permissions: use the native runtime; observation grants no permissions."
            )
            self.app.push_screen(WorkerDetails("\n".join(lines)))
        elif event.button.id == "worker-recovery":
            self.action_open_batch()
        elif event.button.id == "worker-back":
            self.app.pop_screen()

    def launch(self, argv: list[str], runner: CommandRunner) -> None:
        """The already-confirmed send runs once, with its visible monitor mounted first."""

        def send() -> None:
            env = {**os.environ, "HARNESS_CONSOLE_ATTACHED": "1"}
            try:
                result = runner(argv, env=env, cwd=self.repo)
                if result.returncode:
                    from harness.orchestration.worker_observation import redact_visible

                    text = redact_visible(
                        result.stderr
                        or result.stdout
                        or "Runtime handoff failed; inspect dispatch status and native permissions."
                    )
                    call_from_worker(
                        self,
                        lambda: self.query_one("#worker-output", RichLog).write(
                            Text(text)
                        ),
                    )
            except OSError:
                call_from_worker(
                    self,
                    lambda: self.query_one("#worker-output", RichLog).write(
                        Text("Runtime handoff could not start.")
                    ),
                )
            call_from_worker(self, self.action_refresh)

        self.run_worker(send, thread=True, group="send", exit_on_error=False)
