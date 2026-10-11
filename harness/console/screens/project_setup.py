"""Select, inspect and install a target project through the existing CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    RadioButton,
    RadioSet,
    Static,
)

from .. import data
from ..catalog import process_argv
from ..project_target import (
    ProjectTarget,
    capability_options,
    inspect_target,
    installation_blockers,
    installation_plan,
)
from ..runner import CommandRunner
from .commands import call_from_worker, render_result
from .diagnostics import _render_report


class ProjectSetupScreen(Screen[Path | None]):
    BINDINGS = [Binding("escape", "cancel", "Назад")]
    DEFAULT_CSS = """
    ProjectSetupScreen .actions { height: auto; }
    ProjectSetupScreen #setup-scroll { height: 1fr; margin: 0 1; }
    ProjectSetupScreen RadioSet { height: auto; }
    ProjectSetupScreen Static { height: auto; }
    """

    def __init__(self, repo: Path, *, command_runner: CommandRunner) -> None:
        super().__init__()
        self.repo = repo
        self.runner = command_runner
        self.target: ProjectTarget | None = None
        self.busy = False
        self.options = capability_options()

    def _set_busy(self, value: bool) -> None:
        self.busy = value
        for identifier in ("project-path", "capability", "confirm-install"):
            self.query_one("#" + identifier).disabled = value

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="setup-scroll"):
            yield Static("Открыть / подключить проект")
            yield Input(
                value=str(self.repo),
                placeholder="Путь к локальному Git-проекту",
                id="project-path",
            )
            with Horizontal(classes="actions"):
                yield Button("Проверить путь", id="inspect-project")
                yield Button(
                    "Сделать целевым / открыть", id="activate-project", disabled=True
                )
            yield Static("", id="target-status", markup=False)
            yield Static(
                "Состав Harness: workflow нужен для project.json; Backend добавляет orchestration.json."
            )
            with RadioSet(id="capability"):
                for name, label in self.options:
                    yield RadioButton(label, value=name == "pvmalove-suite")
            yield Static("", id="installation-plan", markup=False)
            yield Checkbox(
                "Подтверждаю установку/обновление и открытие указанного проекта",
                id="confirm-install",
            )
            with Horizontal(classes="actions"):
                yield Button("Установить", id="install-project", disabled=True)
                yield Button("Offline", id="setup-offline", disabled=True)
                yield Button("Online", id="setup-online", disabled=True)
                yield Button("Открыть настройки", id="setup-config", disabled=True)
            yield Static(
                "Проверки песочницы анализируют настройки; фактические доступы могут оставаться непроверенными."
            )
            yield Static("", id="setup-output", markup=False)
        yield Footer()

    def _capability(self) -> str:
        index = self.query_one("#capability", RadioSet).pressed_index
        return self.options[max(0, index)][0]

    def _show_plan(self) -> None:
        if self.target is None:
            return
        import shlex

        argv = installation_plan(self.target, self._capability())
        text = "Цель: " + str(self.target.repo) + "\n$ " + shlex.join(argv)
        if self.target.conflicts:
            text += "\nСуществующие пути: " + ", ".join(self.target.conflicts)
            text += "\nForce/replace не включены. При конфликте операция остановится."
        if self.target.capabilities and self.target.capabilities != (
            self._capability(),
        ):
            text += "\nВыбранный состав заменит прежний набор управляемых capabilities."
        try:
            blocked = installation_blockers(self.target, self._capability())
            if blocked:
                text += (
                    "\nУстановка заблокирована; проверьте собственные/изменённые пути:\n"
                    + "\n".join(blocked)
                )
            self.query_one("#install-project", Button).disabled = bool(blocked)
        except (ValueError, OSError):
            text += (
                "\nПлан не удалось проверить; исправьте существующую установку вручную."
            )
            self.query_one("#install-project", Button).disabled = True
        self.query_one("#installation-plan", Static).update(text)

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        if event.radio_set.id == "capability":
            self.query_one("#confirm-install", Checkbox).value = False
            self._show_plan()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "project-path":
            self.target = None
            for name in (
                "activate-project",
                "install-project",
                "setup-offline",
                "setup-online",
                "setup-config",
            ):
                self.query_one("#" + name, Button).disabled = True
            self.query_one("#confirm-install", Checkbox).value = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if self.busy:
            return
        key = event.button.id
        if key == "inspect-project":
            value = self.query_one("#project-path", Input).value
            self._inspect(value)
        elif key == "activate-project" and self.target:
            self.dismiss(self.target.repo)
        elif key == "install-project" and self.target:
            if not self.query_one("#confirm-install", Checkbox).value:
                self.query_one("#setup-output", Static).update(
                    "Сначала подтвердите показанный план установки."
                )
                return
            target = self.target
            argv = installation_plan(target, self._capability())
            self._install(target, argv)
        elif key in ("setup-offline", "setup-online") and self.target:
            repo = self.target.repo
            online = key == "setup-online"
            self._run(
                lambda: _render_report(data.collect_diagnostics(repo, online=online))
            )
        elif key == "setup-config" and self.target:
            from .configuration import ConfigurationScreen

            self.app.push_screen(ConfigurationScreen(self.target.repo))

    def _inspect(self, value: str) -> None:
        self._set_busy(True)
        self.query_one("#target-status", Static).update("Проверяется путь…")

        def work() -> None:
            try:
                target = inspect_target(value)
            except ValueError as exc:
                message = str(exc)
                call_from_worker(self, lambda: self._failed(message))
            else:
                call_from_worker(self, lambda: self._inspected(target))

        self.run_worker(work, thread=True, group="setup", exclusive=True)

    def _install(self, target: ProjectTarget, argv: list[str]) -> None:
        self._set_busy(True)
        self.query_one("#setup-output", Static).update("Устанавливается Harness…")

        def work() -> None:
            try:
                blocked = installation_blockers(target, self._capability_value)
                if blocked:
                    raise ValueError("План изменился; заново проверьте пути проекта")
                result = self.runner(process_argv(argv), cwd=target.repo)
                text, success = (
                    render_result(" ".join(argv), result),
                    result.returncode == 0,
                )
            except (OSError, ValueError) as exc:
                text, success = str(exc), False
            call_from_worker(self, lambda: self._installed(target, text, success))

        self._capability_value = self._capability()
        self.run_worker(work, thread=True, group="setup", exclusive=True)

    def _installed(self, target: ProjectTarget, text: str, success: bool) -> None:
        self._set_busy(False)
        if success:
            self.notify(
                "Harness установлен. Проект стал целевым; откройте настройки и диагностику."
            )
            self.dismiss(target.repo)
        else:
            self._finished(text, True)

    def _failed(self, message: str) -> None:
        self._set_busy(False)
        self.query_one("#target-status", Static).update(message)

    def _inspected(self, target: ProjectTarget) -> None:
        # A changed path invalidates a result returned by an earlier inspection.
        current = (
            Path(self.query_one("#project-path", Input).value).expanduser().resolve()
        )
        if not current.is_relative_to(target.repo):
            self._failed("Путь изменился; проверьте его ещё раз")
            return
        self._set_busy(False)
        self.target = target
        text = (
            str(target.repo)
            + "\nДействие: "
            + target.action
            + "\nУстановлено: "
            + (", ".join(target.capabilities) or "нет")
        )
        self.query_one("#target-status", Static).update(text)
        for name in (
            "activate-project",
            "install-project",
            "setup-offline",
            "setup-online",
            "setup-config",
        ):
            self.query_one("#" + name, Button).disabled = False
        self._show_plan()

    def _run(self, run: "Callable[[], str]", *, refresh: bool = False) -> None:
        self._set_busy(True)
        self.query_one("#setup-output", Static).update("Выполняется…")

        def work() -> None:
            try:
                text = run()
            except Exception:
                text = "Операция не выполнена; проверьте окружение и доступы"
            call_from_worker(self, lambda: self._finished(text, refresh))

        self.run_worker(work, thread=True, group="setup", exclusive=True)

    def _finished(self, text: str, refresh: bool) -> None:
        self._set_busy(False)
        self.query_one("#setup-output", Static).update(text)
        self.query_one("#confirm-install", Checkbox).value = False
        if refresh and self.target is not None:
            try:
                self._inspected(inspect_target(str(self.target.repo)))
            except ValueError as exc:
                self._failed(str(exc))

    def action_cancel(self) -> None:
        if self.busy:
            self.notify(
                "Дождитесь окончания операции перед сменой проекта", severity="warning"
            )
        else:
            self.dismiss(None)

    def on_unmount(self) -> None:
        self.workers.cancel_node(self)
