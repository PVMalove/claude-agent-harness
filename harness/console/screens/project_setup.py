"""Select, inspect and install a target project through the existing CLI."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Callable, Iterable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    DirectoryTree,
    Footer,
    Header,
    Input,
    RadioButton,
    RadioSet,
    Static,
)

from .. import data
from ...health.model import Report
from ..catalog import process_argv
from ..project_target import (
    ProjectTarget,
    capability_details,
    capability_options,
    inspect_target,
    installation_blockers,
    installation_entry,
)
from ..runner import CommandRunner
from .commands import call_from_worker, render_result
from .diagnostics import render_report
from .configuration import ConfirmConfiguration
from .harness import CommandFormScreen

DEFAULT_CAPABILITY = "pvmalove-suite"
_STAGES = (
    "Выбор проекта",
    "Предварительные проверки",
    "Установка / внедрение",
    "Настройка",
    "Итоговая диагностика",
)
_AFTER_INSPECT = (
    "activate-project",
    "install-project",
    "setup-offline",
    "setup-online",
    "setup-fix",
    "setup-config",
)


def describe_report(report: Report) -> str:
    """Summary above the report: what was checked, what was not, what needs a fix."""
    checks = report.checks
    skipped = [check for check in checks if check.status == "skipped"]
    failing = [check for check in checks if check.status in ("warn", "fail")]
    config_only = any(
        check.id.endswith(("_sandbox", "_uv_cache")) and check.status != "skipped"
        for check in checks
    )
    lines = [
        f"Проверено: {len(checks) - len(skipped)} из {len(checks)}.",
        f"Требует исправления: {len(failing)}"
        + (" — см. «Как исправить» у пунктов WARN/FAIL." if failing else "."),
        f"Не проверено: {len(skipped)}"
        + (" — пропущенная проверка не считается успешной." if skipped else "."),
    ]
    if skipped and not report.online:
        lines.append(
            "Онлайн-проверки GitHub/GitLab запускаются отдельной кнопкой Online."
        )
    if config_only:
        lines.append(
            "Проверки песочницы Codex/Claude анализируют файлы настроек, а не фактический доступ."
        )
    return "\n".join(lines)


def _full_report(report: Report) -> str:
    return describe_report(report) + "\n\n" + render_report(report)


class _DirectoryOnlyTree(DirectoryTree):
    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        return [path for path in paths if path.is_dir()]


class ProjectSetupScreen(Screen[Path | None]):
    BINDINGS = [Binding("escape", "cancel", "Назад")]
    DEFAULT_CSS = """
    ProjectSetupScreen .actions { height: auto; }
    ProjectSetupScreen #setup-scroll { height: 1fr; margin: 0 1; }
    ProjectSetupScreen RadioSet { height: auto; }
    ProjectSetupScreen Static { height: auto; }
    ProjectSetupScreen #directory-browser { height: 12; }
    """

    def __init__(self, repo: Path, *, command_runner: CommandRunner) -> None:
        super().__init__()
        self.repo = repo
        self.runner = command_runner
        self.target: ProjectTarget | None = None
        self.busy = False
        self.stage = 0
        self.options = capability_options()
        self._capability_value = ""

    def _set_busy(self, value: bool) -> None:
        self.busy = value
        for identifier in ("project-path", "capability", "browse-project"):
            self.query_one("#" + identifier).disabled = value

    def _set_stage(self, stage: int) -> None:
        self.stage = stage
        self.query_one("#setup-stage", Static).update(
            " → ".join(
                f"[{name}]" if index == stage else name
                for index, name in enumerate(_STAGES)
            )
            + f"\nШаг {stage + 1} из {len(_STAGES)}"
        )

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="setup-scroll"):
            yield Static("Открыть / подключить проект")
            yield Static("", id="setup-stage", markup=False)
            yield Input(
                value=str(self.repo),
                placeholder="Путь к локальному Git-проекту",
                id="project-path",
            )
            with Horizontal(classes="actions"):
                yield Button("Обзор каталогов", id="browse-project")
                yield Button("Проверить путь", id="inspect-project")
                yield Button(
                    "Сделать целевым / завершить",
                    id="activate-project",
                    disabled=True,
                )
            yield Static("", id="target-status", markup=False)
            yield Static(
                "Состав Harness: workflow нужен для project.json; Backend добавляет orchestration.json."
            )
            with RadioSet(id="capability"):
                for name, label in self.options:
                    yield RadioButton(label, value=name == DEFAULT_CAPABILITY)
            yield Static("", id="installation-plan", markup=False)
            with Horizontal(classes="actions"):
                yield Button("Установить", id="install-project", disabled=True)
                yield Button("Offline", id="setup-offline", disabled=True)
                yield Button("Online", id="setup-online", disabled=True)
                yield Button("Исправить локально", id="setup-fix", disabled=True)
                yield Button("Открыть настройки", id="setup-config", disabled=True)
            yield Static(
                "Offline — локальные проверки; Online — отдельная проверка GitHub/GitLab "
                "и прав доступа. Проверки песочницы анализируют настройки; фактические "
                "доступы могут оставаться непроверенными."
            )
            yield Static("", id="setup-output", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self._set_stage(0)

    def _capability(self) -> str:
        index = self.query_one("#capability", RadioSet).pressed_index
        return self.options[max(0, index)][0]

    def _select_capability(self, name: str) -> None:
        buttons = list(self.query_one("#capability", RadioSet).query(RadioButton))
        for (option, _), button in zip(self.options, buttons):
            if option == name:
                button.value = True

    def _show_plan(self) -> None:
        if self.target is None:
            return
        capability = self._capability()
        entry = installation_entry(self.target, capability)
        text = (
            "Цель: "
            + str(self.target.repo)
            + "\n$ "
            + shlex.join(entry.cli_argv(self.target.repo))
            + "\nСостав: "
            + capability_details(capability)
        )
        if self.target.conflicts:
            text += "\nСуществующие пути: " + ", ".join(self.target.conflicts)
            text += "\nForce/replace не включены. При конфликте операция остановится."
        if self.target.capabilities and self.target.capabilities != (capability,):
            text += (
                "\nВыбранный состав заменит прежний набор управляемых capabilities: "
                + ", ".join(self.target.capabilities)
                + "."
            )
        try:
            blocked = installation_blockers(self.target, capability)
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
            self._show_plan()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "project-path":
            self.target = None
            for name in _AFTER_INSPECT:
                self.query_one("#" + name, Button).disabled = True
            self._set_stage(0)

    def on_directory_tree_directory_selected(
        self, event: DirectoryTree.DirectorySelected
    ) -> None:
        self.query_one("#project-path", Input).value = str(event.path)
        self._toggle_browser()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if self.busy:
            return
        key = event.button.id
        if key == "browse-project":
            self._toggle_browser()
        elif key == "inspect-project":
            self._inspect(self.query_one("#project-path", Input).value)
        elif key == "activate-project" and self.target:
            self.dismiss(self.target.repo)
        elif key == "install-project" and self.target:
            self._confirm_install(self.target)
        elif key in ("setup-offline", "setup-online") and self.target:
            self._diagnose(self.target.repo, online=key == "setup-online")
        elif key == "setup-fix" and self.target:
            self._confirm_fixes(self.target.repo)
        elif key == "setup-config" and self.target:
            from .configuration import ConfigurationScreen

            self.app.push_screen(
                ConfigurationScreen(self.target.repo), self._configured
            )

    def _toggle_browser(self) -> None:
        # Mounted on demand: a directory tree starts reading the file system immediately.
        existing = self.query("#directory-browser")
        if existing:
            existing.remove()
            return
        start = Path(self.query_one("#project-path", Input).value).expanduser()
        root = start if start.is_dir() else Path.home()
        self.query_one("#setup-scroll").mount(
            _DirectoryOnlyTree(root, id="directory-browser"),
            after=self.query_one("#target-status"),
        )

    def _confirm_install(self, target: ProjectTarget) -> None:
        # The same confirmation form as the Harness section: command line and reason.
        capability = self._capability()
        entry = installation_entry(target, capability)

        def confirmed(values: dict[str, str] | None) -> None:
            if values is not None:
                self._install(target, capability)

        self.app.push_screen(CommandFormScreen(entry, target.repo), confirmed)

    def _diagnose(self, repo: Path, *, online: bool) -> None:
        self._run(lambda: _full_report(data.collect_diagnostics(repo, online=online)))

    def _confirm_fixes(self, repo: Path) -> None:
        def confirmed(yes: bool | None) -> None:
            if yes:
                # Local fixes only; the report is checked again right after applying them.
                self._run(
                    lambda: _full_report(data.apply_local_fixes(repo, online=False))
                )

        self.app.push_screen(
            ConfirmConfiguration(
                "Применить локальные исправления health?\nЦель: "
                + str(repo)
                + "\nСетевые действия и внешний трекер не затрагиваются; после исправлений проверка повторится."
            ),
            confirmed,
        )

    def _configured(self, _: None) -> None:
        # Final diagnostics of the configured project, local checks only.
        if self.target is not None:
            self._set_stage(4)
            self._diagnose(self.target.repo, online=False)

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

    def _install(self, target: ProjectTarget, capability: str) -> None:
        self._set_busy(True)
        self.query_one("#setup-output", Static).update("Устанавливается Harness…")
        entry = installation_entry(target, capability)
        argv = entry.cli_argv(target.repo)
        self._capability_value = capability

        def work() -> None:
            try:
                if installation_blockers(target, capability):
                    raise ValueError("План изменился; заново проверьте пути проекта")
                result = self.runner(process_argv(argv), cwd=target.repo)
                text, success = (
                    render_result(shlex.join(argv), result),
                    result.returncode == 0,
                )
            except (OSError, ValueError) as exc:
                text, success = str(exc), False
            call_from_worker(self, lambda: self._installed(target, text, success))

        self.run_worker(work, thread=True, group="setup", exclusive=True)

    def _installed(self, target: ProjectTarget, text: str, success: bool) -> None:
        self._finished(text, True)
        if success:
            self._set_stage(3)
            self.notify(
                "Harness установлен. Откройте настройки, затем выполните итоговую диагностику."
            )

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
        first = self.target is None or self.target.repo != target.repo
        self.target = target
        text = (
            str(target.repo)
            + "\nДействие: "
            + target.action
            + "\nУстановлено: "
            + (", ".join(target.capabilities) or "нет")
        )
        self.query_one("#target-status", Static).update(text)
        for name in _AFTER_INSPECT:
            self.query_one("#" + name, Button).disabled = False
        if first:
            installed = [
                name for name, _ in self.options if name in target.capabilities
            ]
            # Keep the installed set by default so one click cannot silently downgrade it.
            self._select_capability(installed[-1] if installed else DEFAULT_CAPABILITY)
            self._set_stage(1)
            # Deferred: this callback runs inside the inspection worker's thread call.
            self.call_later(self._diagnose, target.repo, online=False)
        self._show_plan()

    def _run(self, run: Callable[[], str], *, refresh: bool = False) -> None:
        self._set_busy(True)
        self.query_one("#setup-output", Static).update("Выполняется…")

        def work() -> None:
            try:
                text = run()
            except Exception:
                text = "Операция не выполнена; проверьте окружение и доступы"
            call_from_worker(self, lambda: self._finished(text, refresh))

        self.run_worker(work, thread=True, group="setup-run", exclusive=True)

    def _finished(self, text: str, refresh: bool) -> None:
        self._set_busy(False)
        self.query_one("#setup-output", Static).update(text)
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
