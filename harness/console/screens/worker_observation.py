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
from textual.screen import ModalScreen, Screen
from textual.timer import Timer
from textual.widgets import Button, Footer, ListItem, ListView, RichLog, Select, Static

from harness.orchestration.core.utils import JsonObject
from .. import brand, i18n, reports
from ..chrome import TopBar
from ..runner import CommandRunner
from ..translate import Translated
from .commands import call_from_worker
from .dashboard import SECTIONS

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


class WorkerObservationScreen(Translated, Screen[None]):
    """A passive view. Closing/reconnecting never starts or terminates a worker."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Назад"),
        Binding("r", "refresh", "Обновить"),
        *(
            Binding(str(i + 1), f"section({i})", name, show=False)
            for i, name in enumerate(SECTIONS)
        ),
    ]
    DEFAULT_CSS = """
    WorkerObservationScreen #worker-body { height: 1fr; }
    WorkerObservationScreen #worker-navigation { width: 25; margin: 0 1; }
    WorkerObservationScreen #worker-navigation ListView { height: 1fr; }
    WorkerObservationScreen #worker-main { width: 1fr; min-width: 0; }
    WorkerObservationScreen #worker-picker, WorkerObservationScreen #worker-filter { height: 3; }
    WorkerObservationScreen #worker-status { height: auto; max-height: 8; padding: 0 1; }
    WorkerObservationScreen #worker-output { height: 1fr; border: round $panel-lighten-2; margin: 0 1; }
    WorkerObservationScreen #worker-actions { height: auto; min-height: 3; overflow-x: auto; }
    WorkerObservationScreen #worker-actions Button { min-width: 10; margin-right: 1; }
    WorkerObservationScreen #worker-menu { display: none; }
    WorkerObservationScreen.-narrow #worker-navigation { display: none; }
    WorkerObservationScreen.-narrow.-menu-open #worker-navigation { display: block; width: 20; }
    WorkerObservationScreen.-narrow #worker-menu { display: block; }
    WorkerObservationScreen #worker-cost { height: 1; color: $text-muted; padding: 0 1; }
    """

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
        self._choices = [
            (f"{s.ticket} · {s.role} · {s.dispatch_id}", s.dispatch_id)
            for s in reversed(view.stages)
        ]
        if dispatch and not any(value == dispatch for _, value in self._choices):
            self._choices.insert(0, (dispatch, dispatch))
        if not dispatch and self._choices:
            self.dispatch = self._choices[0][1]
        self._timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield TopBar("Главная › Оркестрация › Сессии")
        with Horizontal(id="worker-body"):
            with Vertical(id="worker-navigation"):
                menu = ListView(
                    *(
                        ListItem(
                            Static(
                                f"{n + 1} {i18n.section_label(getattr(self.app, 'language', 'ru'), name)}",
                                markup=False,
                            ),
                            name=name,
                        )
                        for n, name in enumerate(SECTIONS)
                    ),
                    id="worker-sections",
                )
                menu.border_title = brand.frame_title("Главное меню")
                yield menu
                yield Static("Worker и QA — отдельные dispatch", markup=False)
            with Vertical(id="worker-main"):
                yield Select(
                    self._choices,
                    value=self.dispatch or Select.BLANK,
                    prompt="Выберите worker",
                    id="worker-picker",
                )
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
                yield Static(
                    "○ Ожидание наблюдения",
                    id="worker-status",
                    markup=False,
                    classes="no-i18n",
                )
                yield RichLog(
                    id="worker-output",
                    max_lines=500,
                    wrap=True,
                    markup=False,
                    highlight=False,
                )
                yield Static(
                    "Наблюдение без дополнительных LLM-вызовов", id="worker-cost"
                )
        with Horizontal(id="worker-actions"):
            yield Button("Меню", id="worker-menu")
            yield Button("Переподключить", id="worker-reconnect")
            yield Button("Детали", id="worker-details")
            yield Button("Запрос native", id="worker-permission", disabled=True)
            yield Button("История batch", id="worker-recovery")
            yield Button("Назад", id="worker-back")
        yield Footer()

    def on_mount(self) -> None:
        self.set_class(self.size.width < 110, "-narrow")
        self._timer = self.set_interval(1, self.action_refresh)
        self.action_refresh()
        self.translate_now()

    def on_resize(self) -> None:
        self.set_class(self.size.width < 110, "-narrow")

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
        self.query_one("#worker-permission", Button).disabled = not bool(
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
                self._last_operation = event.get("text", "—").splitlines()[0][:100]
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
                    failure.get("error", failure.get("kind", "refused"))
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
        self.query_one("#worker-status", Static).update("\n".join(lines))
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
                output.write(
                    Text(f"[{event.get('kind', 'event')}] {event.get('text', '')}")
                )
        self._events = self._events[-500:]
        self._stream_offset = snapshot.get("next_offset", self._stream_offset)
        bar = self.query_one(TopBar)
        bar.crumbs = f"Главная › Оркестрация › {snapshot.get('batch_id')} › Сессии"
        bar.refresh_status()

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
        if self.snapshot:
            self._render_snapshot({**self.snapshot, "events": []}, self.dispatch or "")
        language = getattr(self.app, "language", "ru")
        for index, item in enumerate(
            self.query("#worker-sections > ListItem").results(ListItem)
        ):
            item.query_one(Static).update(
                f"{index + 1} {i18n.section_label(language, SECTIONS[index])}"
            )

    def _event_visible(self, event: JsonObject) -> bool:
        selected = self.query_one("#worker-filter", Select).value
        return selected == "all" or str(event.get("kind", "")).startswith(str(selected))

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "worker-filter":
            output = self.query_one("#worker-output", RichLog)
            output.clear()
            for row in self._events:
                if self._event_visible(row):
                    output.write(
                        Text(f"[{row.get('kind', 'event')}] {row.get('text', '')}")
                    )
            return
        if event.select.id != "worker-picker" or event.value is Select.BLANK:
            return
        if str(event.value) != self.dispatch:
            self.dispatch = str(event.value)
            self._stream_offset = 0
            self.snapshot = {}
            self._events = []
            self._last_operation = "—"
            self.query_one("#worker-output", RichLog).clear()
            self.action_refresh()

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "worker-sections" and event.item.name:
            await self.action_section(SECTIONS.index(event.item.name))

    async def action_section(self, index: int) -> None:
        from .dashboard import DashboardScreen

        for screen in self.app.screen_stack:
            if isinstance(screen, DashboardScreen):
                await screen.action_open_section(index)
                return

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "worker-reconnect":
            self.action_refresh()
        elif event.button.id == "worker-menu":
            self.toggle_class("-menu-open")
        elif event.button.id == "worker-permission":
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
            from .reports import BatchTimelineScreen

            timeline = reports.load_ledger_view(self.repo).timeline(
                self.snapshot.get("batch_id", "")
            )
            if timeline:
                self.app.push_screen(BatchTimelineScreen(timeline, self.repo))
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
