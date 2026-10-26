"""Select, inspect and install a target project through the existing CLI."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Callable, Iterable

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import (
    Button,
    DirectoryTree,
    Input,
    RadioButton,
    RadioSet,
    Static,
)

from .. import brand, i18n
from ..shell import SHELL_CSS, ConsoleShell, WrappedValue
from ..status import Status
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
from ..translate import retranslate
from .commands import call_from_worker, render_result
from .diagnostics import render_report, styled_log
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
_TARGET_HINT = "Укажите путь к проекту и нажмите «Проверить путь»."
_BUSY_NOTE = "Дождитесь окончания операции"
_PLAN_HINT = "План появится после проверки пути."
_OUTPUT_HINT = "Результат проверок и установки появится здесь."
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
    """Folders only. A click or Space opens and closes a folder, Right opens it, Enter chooses it:
    only Enter puts the folder into the path."""

    BINDINGS = [
        Binding("right", "expand", "Раскрыть", show=False),
        Binding("enter", "choose", "Подставить путь", show=False),
    ]

    class Chosen(Message):
        def __init__(self, path: Path) -> None:
            super().__init__()
            self.path = path

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        return [path for path in paths if path.is_dir()]

    def action_expand(self) -> None:
        node = self.cursor_node
        if node is not None and node.allow_expand:
            node.expand()

    def action_choose(self) -> None:
        node = self.cursor_node
        if node is not None and node.data is not None:
            self.post_message(self.Chosen(node.data.path))


class ProjectSetupScreen(ConsoleShell[Path | None]):
    """Open project on the shared shell; it is dismissed with the chosen project or with None."""

    CRUMBS = "Главная › Открыть проект"
    SECTION = "Open project"
    NARROW_FOOTER_KEYS = ("escape", "f1", "f2")
    AUTO_FOCUS = "#project-path"
    CSS = SHELL_CSS + brand.shell_css("""
    ProjectSetupScreen #setup-scroll { width: 100%; height: 1fr; overflow-x: hidden; padding: 0 2; }
    ProjectSetupScreen #setup-columns { width: 100%; height: auto; layout: vertical; }
    ProjectSetupScreen.-spacious #setup-columns { layout: horizontal; }
    ProjectSetupScreen #setup-left, ProjectSetupScreen #setup-right { width: 1fr; height: auto; }
    ProjectSetupScreen .frame { height: auto; }
    ProjectSetupScreen Vertical.frame { overflow: hidden hidden; }
    ProjectSetupScreen .actions { height: auto; }
    ProjectSetupScreen .actions Button { margin-right: 1; }
    ProjectSetupScreen Static { height: auto; }
    ProjectSetupScreen RadioSet { height: auto; border: none; padding: 0; }
    ProjectSetupScreen #project-path { width: 1fr; }
    ProjectSetupScreen #target-status, ProjectSetupScreen .hint { color: $text-muted; }
    ProjectSetupScreen #directory-browser { height: 12; }
    ProjectSetupScreen #setup-output-scroll { height: auto; max-height: 18; }
    """)

    def __init__(self, repo: Path, *, command_runner: CommandRunner) -> None:
        super().__init__()
        self.repo = repo
        self.runner = command_runner
        self.target: ProjectTarget | None = None
        self.busy = False
        self.stage = 0
        self.options = capability_options()
        self._capability_value = ""
        # What `#target-status` shows as a status: (kind, text); None while it shows the hint.
        self._target_status: tuple[str, str] | None = None
        # The plan whose blockers are being read; an older plan's result is dropped.
        self._plan_epoch = 0

    def _set_busy(self, value: bool) -> None:
        self.busy = value
        for identifier in ("project-path", "capability", "browse-project"):
            self.query_one("#" + identifier).disabled = value
        # A press during an operation is ignored, so the actions look it: disabled until it ends.
        self.query_one("#inspect-project", Button).disabled = value
        if value:
            for name in _AFTER_INSPECT:
                self.query_one("#" + name, Button).disabled = True
        elif self.target is not None:
            for name in _AFTER_INSPECT:
                self.query_one("#" + name, Button).disabled = False
            self._show_plan()

    def _put(self, selector: str, text: str) -> None:
        """Replace a fixed text (a hint) in the console language."""
        widget = self.query_one(selector, Static)
        widget.update(text)
        retranslate(widget, getattr(self.app, "language", "ru"))

    def _reset_target(self) -> None:
        """No target: the previous one, its plan, status and result are not available any more."""
        self.target = None
        for name in _AFTER_INSPECT:
            self.query_one("#" + name, Button).disabled = True
        self._target_status = None
        self._put("#target-status", _TARGET_HINT)
        self._put("#installation-plan", _PLAN_HINT)
        self._put("#setup-output", _OUTPUT_HINT)
        self._set_stage(0)

    def _set_stage(self, stage: int) -> None:
        self.stage = stage
        text = (
            " → ".join(
                f"[{name}]" if index == stage else name
                for index, name in enumerate(_STAGES)
            )
            + f"\nШаг {stage + 1} из {len(_STAGES)}"
        )
        self.query_one("#setup-stage", Static).update(
            i18n.tr_text(getattr(self.app, "language", "ru"), text)
        )

    def sync_language(self) -> None:
        ConsoleShell.sync_language(self)
        self._set_stage(self.stage)
        if self._target_status is not None:
            self._show_target(*self._target_status)

    def _show_target(self, kind: str, text: str) -> None:
        """The target line as a status: its symbol and colour on the first line, the facts below."""
        self._target_status = (kind, text)
        language = getattr(self.app, "language", "ru")
        # Line by line: a known whole line (a status, a hint) has its own English text.
        first, _, rest = "\n".join(
            i18n.tr_text(language, line) for line in text.split("\n")
        ).partition("\n")
        status = Status(kind, first, first)
        shown = Text(status.render(language), style=status.color)
        if rest:
            shown.append("\n" + rest)
        self.query_one("#target-status", Static).update(shown)

    def _show_running(self, text: str) -> None:
        """The output block while a command runs: a running symbol and the text."""
        language = getattr(self.app, "language", "ru")
        status = Status("running", text, i18n.tr_text("en", text))
        self.query_one("#setup-output", Static).update(
            Text(status.render(language), style=status.color)
        )

    def compose_strip(self) -> ComposeResult:
        yield WrappedValue(str(self.repo))

    def compose_content(self) -> ComposeResult:
        with VerticalScroll(id="setup-scroll"):
            with Vertical(id="setup-stage-frame", classes="frame") as stages:
                stages.border_title = brand.section_title("Этапы")
                yield Static("", id="setup-stage", markup=False)
            with Horizontal(id="setup-columns"):
                with Vertical(id="setup-left"):
                    with Vertical(id="project-frame", classes="frame") as project:
                        project.border_title = brand.section_title("Проект")
                        yield Input(
                            value=str(self.repo),
                            placeholder="Путь к локальному Git-проекту",
                            id="project-path",
                        )
                        with Horizontal(classes="actions"):
                            yield Button(
                                "Обзор папок", id="browse-project", compact=True
                            )
                            yield Button(
                                "Проверить путь", id="inspect-project", compact=True
                            )
                            yield Button(
                                "Открыть проект",
                                id="activate-project",
                                disabled=True,
                                compact=True,
                            )
                        yield Static(
                            _TARGET_HINT,
                            id="target-status",
                            markup=False,
                        )
                    with Vertical(id="capability-frame", classes="frame") as capability:
                        capability.border_title = brand.section_title("Состав Harness")
                        yield Static(
                            "workflow нужен для project.json; Backend добавляет orchestration.json.",
                            classes="hint",
                        )
                        with RadioSet(id="capability"):
                            for name, label in self.options:
                                yield RadioButton(
                                    label, value=name == DEFAULT_CAPABILITY
                                )
                with Vertical(id="setup-right"):
                    with Vertical(id="plan-frame", classes="frame") as plan:
                        plan.border_title = brand.section_title("План установки")
                        yield Static(_PLAN_HINT, id="installation-plan", markup=False)
                        with Horizontal(classes="actions"):
                            yield Button(
                                "Установить",
                                id="install-project",
                                disabled=True,
                                compact=True,
                            )
                    with Vertical(id="checks-frame", classes="frame") as checks:
                        checks.border_title = brand.section_title("Проверки")
                        with Horizontal(classes="actions"):
                            yield Button(
                                "Офлайн",
                                id="setup-offline",
                                disabled=True,
                                compact=True,
                            )
                            yield Button(
                                "Онлайн",
                                id="setup-online",
                                disabled=True,
                                compact=True,
                            )
                            yield Button(
                                "Исправить",
                                id="setup-fix",
                                disabled=True,
                                compact=True,
                            )
                            yield Button(
                                "Настройка",
                                id="setup-config",
                                disabled=True,
                                compact=True,
                            )
                        yield Static(
                            "Offline — локальные проверки; Online — отдельная проверка GitHub/GitLab "
                            "и прав доступа. Проверки песочницы анализируют настройки; фактические "
                            "доступы могут оставаться непроверенными.",
                            classes="hint",
                        )
            with VerticalScroll(id="setup-output-scroll", classes="frame") as output:
                output.border_title = brand.section_title("Результат")
                yield Static(_OUTPUT_HINT, id="setup-output", markup=False)

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

    def _plan_text(self, target: ProjectTarget, capability: str) -> str:
        """The plan without the blockers: no file system or git access, so it is built at once."""
        entry = installation_entry(target, capability)
        text = (
            "Цель: "
            + str(target.repo)
            + "\n$ "
            + shlex.join(entry.cli_argv(target.repo))
            + "\nСостав: "
            + capability_details(capability)
        )
        if target.conflicts:
            text += "\nСуществующие пути: " + ", ".join(target.conflicts)
            text += "\nForce/replace не включены. При конфликте операция остановится."
        if target.capabilities and target.capabilities != (capability,):
            text += (
                "\nВыбранный состав заменит прежний набор управляемых capabilities: "
                + ", ".join(target.capabilities)
                + "."
            )
        return text

    def _show_plan(self) -> None:
        """Show the plan at once; the blockers come from a worker (they read files and run git),
        and "Установить" stays off until they are known."""
        if self.target is None:
            return
        target, capability = self.target, self._capability()
        text = self._plan_text(target, capability)
        self.query_one("#installation-plan", Static).update(text)
        self.query_one("#install-project", Button).disabled = True
        self._plan_epoch += 1
        epoch = self._plan_epoch

        def work() -> None:
            blocked: tuple[str, ...] = ()
            error: Exception | None = None
            try:
                blocked = installation_blockers(target, capability)
            except (ValueError, OSError) as exc:
                error = exc
            call_from_worker(
                self, lambda: self._planned(epoch, target, text, blocked, error)
            )

        # Not exclusive: a thread cannot be cancelled, an outdated result is dropped by its epoch.
        self.run_worker(work, thread=True, group="plan")

    def _planned(
        self,
        epoch: int,
        target: ProjectTarget,
        text: str,
        blocked: tuple[str, ...],
        error: Exception | None,
    ) -> None:
        # The target or the capability changed while the blockers were being read: not this plan.
        if epoch != self._plan_epoch or self.target is not target:
            return
        if isinstance(error, ValueError):
            self._failed(str(error))
            return
        install = self.query_one("#install-project", Button)
        if error is not None:
            text += (
                "\nПлан не удалось проверить; исправьте существующую установку вручную."
            )
            install.disabled = True
        else:
            if blocked:
                text += (
                    "\nУстановка заблокирована; проверьте собственные/изменённые пути:\n"
                    + "\n".join(blocked)
                )
            install.disabled = self.busy or bool(blocked)
        self.query_one("#installation-plan", Static).update(text)

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        if event.radio_set.id == "capability":
            self._show_plan()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "project-path":
            self._reset_target()

    def on_directory_tree_directory_selected(
        self, event: DirectoryTree.DirectorySelected
    ) -> None:
        # A click on a folder only highlights and opens it; Enter chooses (`_DirectoryOnlyTree.Chosen`).
        event.stop()

    @on(_DirectoryOnlyTree.Chosen)
    def _folder_chosen(self, event: _DirectoryOnlyTree.Chosen) -> None:
        event.stop()
        field = self.query_one("#project-path", Input)
        field.value = str(event.path)
        self._toggle_browser()
        field.focus()

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
            # An auto-height frame keeps its old height unless its layout is recomputed.
            self.call_after_refresh(self._relayout)
            return
        start = Path(self.query_one("#project-path", Input).value).expanduser()
        root = start if start.is_dir() else Path.home()
        tree = _DirectoryOnlyTree(root, id="directory-browser")
        self.query_one("#project-frame").mount(
            tree, after=self.query_one("#target-status")
        )
        self.call_after_refresh(tree.focus)

    def _relayout(self) -> None:
        for identifier in (
            "project-frame",
            "setup-left",
            "setup-columns",
            "setup-scroll",
        ):
            self.query_one("#" + identifier).refresh(layout=True)

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
        self._show_target("running", "Проверяется путь…")

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
        self._set_stage(2)
        self._show_running("Устанавливается Harness…")
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
            except ValueError as exc:
                # The plan or the lock changed: an error with a next step, as after a path check.
                message = str(exc)
                call_from_worker(self, lambda: self._failed(message))
                return
            except OSError as exc:
                text, success = str(exc), False
            call_from_worker(self, lambda: self._installed(target, text, success))

        # Its own group: the check that follows the installation must not cancel this worker.
        self.run_worker(work, thread=True, group="setup-install", exclusive=True)

    def _installed(self, target: ProjectTarget, text: str, success: bool) -> None:
        self._finished(text, True)
        if success:
            self._set_stage(3)
            self.notify(
                "Harness установлен. Откройте настройки, затем выполните итоговую диагностику."
            )
        else:
            self._set_stage(1)

    def _failed(self, message: str) -> None:
        self._reset_target()
        self._set_busy(False)
        self._show_target("error", message)

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
        self._show_target("accepted", text)
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
        self._show_running("Выполняется…")

        def work() -> None:
            try:
                text = run()
            except (Exception, SystemExit):
                # `SystemExit`: the CLI helpers the checks use exit the process on a broken file.
                text = "Операция не выполнена; проверьте окружение и доступы"
            call_from_worker(self, lambda: self._finished(text, refresh))

        self.run_worker(work, thread=True, group="setup-run", exclusive=True)

    def _finished(self, text: str, refresh: bool) -> None:
        self.query_one("#setup-output", Static).update(styled_log(text))
        if refresh and self.target is not None:
            # Checked again in the inspection worker (it runs git), the screen stays busy until then.
            self._inspect(str(self.target.repo))
        else:
            self._set_busy(False)

    def _wait_for_operation(self) -> bool:
        """True (after telling the user) while an operation runs: the screen is not left meanwhile."""
        if self.busy:
            language = getattr(self.app, "language", "ru")
            self.notify(i18n.tr(language, _BUSY_NOTE), severity="warning")
        return self.busy

    async def open_section(self, index: int) -> None:
        if not self._wait_for_operation():
            await super().open_section(index)

    async def action_back(self) -> None:
        # Not `ConsoleShell.action_back` (`pop_screen`): it would drop the push callback, which
        # receives None when the screen is closed without a choice.
        if self.query("#directory-browser"):
            # Esc closes the folder browser first; leaving the screen is the next press.
            self._toggle_browser()
            self.query_one("#browse-project", Button).focus()
        elif not self._wait_for_operation():
            self.dismiss(None)

    def on_unmount(self) -> None:
        self.workers.cancel_node(self)
