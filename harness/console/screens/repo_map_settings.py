"""Repo Map settings: the map tier, the parsers of the project's languages, and project memory.

One pane of the Repo Map screen. It shows the current state of each and offers the actions as
buttons: switch minimal / full, open the setup wizard on the parser policy, install the parser of a
language, initialise or refresh project memory. Anything that downloads or builds runs the CLI
command shown in the confirmation form; configuration is written through a validated draft."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, RadioButton, RadioSet, SelectionList, Static
from textual.widgets.selection_list import Selection

from harness.repo_map.bundle_fetch import ParserStatus, parser_status
from harness.repo_map.bundle_pins import LANGUAGES

from .. import brand, i18n, repo_map_settings
from ..catalog import (
    MEMORY_COMMANDS,
    CatalogEntry,
    parsers_install_entry,
    process_argv,
)
from ..logtext import colorize_log
from ..runner import CommandRunner, capturing_runner
from ..widgets import CheckList
from .commands import call_from_worker, render_result
from .harness import CommandFormScreen

_TIER_CHOICES = (
    ("minimal", "minimal — только пути файлов, без парсера"),
    ("full", "full — сигнатуры и связи (нужен парсер языка)"),
)
_MEMORY = {entry.key: entry for entry in MEMORY_COMMANDS}
# Why the parser bundle is not usable (`DegradationReason`), in words the user can act on.
_REASONS = {
    "offline parser bundle unavailable": "парсер не установлен: выберите языки и нажмите «Установить выбранные»",
    "parser bundle hash mismatch": "bundle повреждён или изменён: установите парсеры заново",
    "parser wheelhouse missing for interpreter/platform pair": "bundle собран для другого интерпретатора или платформы: установите парсеры заново",
    "parser subprocess failed": "не удалось определить интерпретатор парсера",
    "parser bundle install failed": "bundle не удалось установить локально",
    "uv executable unavailable": "не найден uv: он нужен для офлайн-установки bundle",
}


class RepoMapSettings(Vertical):
    """Frames of the tier, the language parsers and the project memory, with their actions."""

    DEFAULT_CSS = """
    RepoMapSettings { width: 100%; height: 1fr; }
    RepoMapSettings #settings-scroll { width: 100%; height: 1fr; overflow-x: hidden; }
    RepoMapSettings .frame { height: auto; }
    RepoMapSettings Vertical.frame { overflow: hidden hidden; }
    RepoMapSettings Static { height: auto; }
    RepoMapSettings .actions { height: auto; }
    RepoMapSettings RadioSet { height: auto; }
    RepoMapSettings SelectionList { height: auto; max-height: 8; }
    RepoMapSettings .hint { color: $text-muted; }
    RepoMapSettings #settings-output-scroll { height: auto; max-height: 14; }
    """

    def __init__(
        self,
        repo: Path,
        *,
        command_runner: CommandRunner = capturing_runner,
        load_parsers: Callable[[Path], ParserStatus] = parser_status,
    ) -> None:
        super().__init__(id="repo-map-settings")
        self.repo = repo
        self._runner = command_runner
        self._load_parsers = load_parsers
        self.busy = False
        self.parsers: ParserStatus | None = None
        self.files: dict[str, int] = {}
        self._output = ""
        self._seeded = False
        self._installed: set[str] = set()
        self._policy_tier: str | None = None

    # -- composition ---------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="settings-scroll"):
            with Vertical(id="tier-frame", classes="frame") as tier:
                tier.border_title = brand.frame_title("Режим карты")
                yield Static("", id="tier-status", classes="no-i18n", markup=False)
                with RadioSet(id="tier-choice"):
                    for key, label in _TIER_CHOICES:
                        yield RadioButton(label, id=f"tier-{key}")
                with Horizontal(classes="actions"):
                    yield Button("Применить режим", id="tier-apply", compact=True)
                    yield Button("Настроить парсер", id="parser-settings", compact=True)
            with Vertical(id="parsers-frame", classes="frame") as parsers:
                parsers.border_title = brand.frame_title("Парсеры языков")
                yield Static("", id="parser-status", classes="no-i18n", markup=False)
                yield CheckList(id="parser-languages")
                with Horizontal(classes="actions"):
                    yield Button(
                        "Установить выбранные", id="parser-install", compact=True
                    )
                    yield Button("Проверить заново", id="parser-refresh", compact=True)
            with Vertical(id="memory-frame", classes="frame") as memory:
                memory.border_title = brand.frame_title("Память проекта")
                yield Static("", id="memory-status", classes="no-i18n", markup=False)
                with Horizontal(classes="actions"):
                    yield Button("Инициализировать", id="memory-init", compact=True)
                    yield Button("Синхронизировать", id="memory-sync", compact=True)
                    yield Button("Пересобрать", id="memory-rebuild", compact=True)
                    yield Button("Настроить память", id="memory-settings", compact=True)
            with VerticalScroll(id="settings-output-scroll", classes="frame") as out:
                out.border_title = brand.frame_title("Результат")
                yield Static("", id="settings-output", classes="no-i18n")

    def on_mount(self) -> None:
        self.reload()

    # -- state ---------------------------------------------------------------------------------

    @property
    def language(self) -> str:
        return str(getattr(self.app, "language", "ru"))

    def t(self, text: str) -> str:
        return i18n.tr_text(self.language, text)

    def reload(self) -> None:
        """Re-read the tier, parsers and memory in a worker, then repaint."""

        def work() -> None:
            try:
                parsers: ParserStatus | None = self._load_parsers(self.repo)
            except Exception:
                parsers = None
            files = repo_map_settings.language_files(self.repo)
            call_from_worker(self, lambda: self._loaded(parsers, files))

        self.run_worker(work, thread=True, exclusive=True, group="settings-load")

    def _loaded(self, parsers: ParserStatus | None, files: dict[str, int]) -> None:
        self.parsers = parsers
        self.files = files
        self._seeded = False
        self.repaint()

    def repaint(self) -> None:
        """Show the current state in the current language."""
        t = self.t
        tier = repo_map_settings.tier_setting(self.repo)
        text = t("В политике проекта: ") + tier.tier
        effective = self._effective_note(tier.tier)
        if effective:
            text += "\n" + effective
        if tier.reason:
            text += "\n" + t(tier.reason)
        self.query_one("#tier-status", Static).update(text)
        choice = self.query_one("#tier-choice", RadioSet)
        if tier.tier != self._policy_tier:
            # Only when the policy changed: a repaint must not undo a choice not yet applied.
            self.query_one(f"#tier-{tier.tier}", RadioButton).value = True
            self._policy_tier = tier.tier
        choice.disabled = not tier.editable or self.busy
        self.query_one("#tier-apply", Button).disabled = not tier.editable or self.busy
        self.query_one("#parser-settings", Button).disabled = not tier.editable
        self._repaint_parsers()
        self._repaint_memory()
        self.query_one("#settings-output", Static).update(
            colorize_log(self._output) if self._output else t("Действий пока не было")
        )

    def _effective_note(self, policy_tier: str) -> str:
        """Why the map is minimal although the policy asks for full: the parser is not ready."""
        if policy_tier != "full" or self.parsers is None or self.parsers.ready:
            return ""
        reason = _REASONS.get(str(self.parsers.problem), str(self.parsers.problem))
        return self.t("Фактически карта строится в minimal: ") + self.t(reason)

    def _repaint_parsers(self) -> None:
        t = self.t
        status = self.parsers
        if status is None:
            line = t("Состояние парсеров не прочитано")
            installed: set[str] = set()
        else:
            installed = set(status.languages)
            line = (
                t("Bundle готов, режим full доступен")
                if status.ready
                else t("Bundle не готов: ")
                + t(_REASONS.get(str(status.problem), str(status.problem)))
            ) + (f"\n{t('Пара')}: {status.pair}" if status.pair else "")
            line += f"\n{t('Каталог')}: {status.registry}"
        self.query_one("#parser-status", Static).update(line)
        options = self.query_one("#parser-languages", SelectionList)
        self._installed = installed
        chosen = set(options.selected) - installed
        first = not self._seeded
        self._seeded = True
        options.clear_options()
        rows = []
        for language in LANGUAGES:
            count = self.files.get(language.key, 0)
            state = t("установлен") if language.key in installed else t("не установлен")
            label = f"{language.label} — {state}"
            if count:
                label += " · " + t("файлов в проекте") + f": {count}"
            if language.key in installed:
                # An installed parser is shown ticked and locked: it cannot be unticked here.
                rows.append(Selection(label, language.key, True, disabled=True))
                continue
            wanted = language.key in chosen if not first else bool(count)
            rows.append(Selection(label, language.key, wanted))
        options.add_options(rows)
        self.query_one("#parser-install", Button).disabled = self.busy

    def _repaint_memory(self) -> None:
        t = self.t
        status = repo_map_settings.memory_status(self.repo)
        if status.problem:
            text = t("Память недоступна: ") + status.problem
        else:
            index = (
                f"{status.indexed} " + t("документов")
                if status.indexed is not None
                else t("не построен")
            )
            state = (
                t("включена")
                if status.active
                else (
                    t("включена, но не читает источники: ") + t(status.inactive)
                    if status.inactive
                    else t("выключена")
                )
            )
            text = (
                state
                + (
                    " · " + t("источники") + ": " + ", ".join(status.source_types)
                    if status.source_types
                    else ""
                )
                + " · "
                + t("индекс")
                + ": "
                + index
            )
        self.query_one("#memory-status", Static).update(t("Память: ") + text)
        for identifier in ("memory-init", "memory-sync", "memory-rebuild"):
            self.query_one(f"#{identifier}", Button).disabled = self.busy

    # -- actions -------------------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        key = event.button.id or ""
        if not key.startswith(("tier-", "parser-", "memory-")):
            return
        event.stop()
        if self.busy:
            return
        if key == "tier-apply":
            self._apply_tier()
        elif key == "parser-settings":
            self._open_wizard("orchestration.json", ("repo_map_policy",))
        elif key == "parser-refresh":
            self.reload()
        elif key == "parser-install":
            self._install_parsers()
        elif key == "memory-settings":
            self._open_wizard("project.json", ("memory",))
        elif key == "memory-init":
            self._init_memory()
        elif key in ("memory-sync", "memory-rebuild"):
            self._run_command(_MEMORY[key], reload=True)

    def _open_wizard(self, name: str, path: tuple[str, ...]) -> None:
        from .configuration import ConfigurationScreen

        self.app.push_screen(
            ConfigurationScreen(self.repo, (name, path)), lambda _: self.reload()
        )

    def _selected_tier(self) -> str:
        pressed = self.query_one("#tier-choice", RadioSet).pressed_button
        return (pressed.id or "tier-full").removeprefix("tier-") if pressed else "full"

    def _apply_tier(self) -> None:
        from .configuration import ConfirmConfiguration

        tier = self._selected_tier()
        current = repo_map_settings.tier_setting(self.repo).tier
        if tier == current:
            note = self._effective_note(tier)
            self._show(
                self.t("В политике уже ")
                + tier
                + (
                    "\n" + note + "\n" + self.t("Установите парсеры в блоке ниже.")
                    if note
                    else ""
                )
            )
            return
        text = (
            self.t("Записать repo_map_policy.tier = ")
            + tier
            + "\n"
            + self.t("Цель: ")
            + str(self.repo)
            + "\n"
            + self.t("Прежние файлы будут сохранены отдельной версией.")
        )

        def confirmed(ok: bool | None) -> None:
            if ok:
                self._work(
                    lambda: self.t("Режим записан: ")
                    + tier
                    + "\n"
                    + self.t("Версия с прежними файлами: ")
                    + repo_map_settings.apply_tier(self.repo, tier)
                    + "\n"
                    + self.t("Построенная карта обновится при следующей сборке."),
                )

        self.app.push_screen(ConfirmConfiguration(text), confirmed)

    def _install_parsers(self) -> None:
        chosen = [
            key
            for key in self.query_one("#parser-languages", SelectionList).selected
            if key not in self._installed
        ]
        if not chosen:
            self._show(self.t("Отметьте язык, парсер которого нужно установить"))
            return
        self._confirm_and_run(parsers_install_entry(chosen), reload=True)

    def _init_memory(self) -> None:
        entry = _MEMORY["memory-build"]
        try:
            plan = repo_map_settings.plan_memory_enable(self.repo)
        except ValueError as exc:
            self._show(str(exc))
            return
        if not plan:
            self._confirm_and_run(entry, reload=True)
            return
        from .configuration import ConfirmConfiguration

        def enable() -> str:
            backup = repo_map_settings.enable_memory(self.repo)
            return (
                self.t("Память настроена.")
                + "\n"
                + self.t("Версия с прежними файлами: ")
                + backup
            )

        def confirmed(ok: bool | None) -> None:
            if ok:
                self._work(enable, then=lambda: self._run_command(entry, reload=True))

        changes = "\n".join(
            f"  {'.'.join(path)} = {json.dumps(value, ensure_ascii=False)}"
            for path, value in plan.items()
        )
        self.app.push_screen(
            ConfirmConfiguration(
                self.t("Чтобы память читала источники, будет записано в project.json:")
                + "\n"
                + changes
                + "\n"
                + self.t("затем выполнено:")
                + "\n$ "
                + entry.cli_line(self.repo)
                + "\n"
                + self.t("Прежние файлы будут сохранены отдельной версией.")
            ),
            confirmed,
        )

    def _confirm_and_run(self, entry: CatalogEntry, *, reload: bool) -> None:
        def confirmed(values: "dict[str, str] | None") -> None:
            if values is not None:
                self._run_command(entry, reload=reload)

        self.app.push_screen(CommandFormScreen(entry, self.repo), confirmed)

    # -- running -------------------------------------------------------------------------------

    def _set_busy(self, value: bool) -> None:
        self.busy = value
        self.repaint()

    def _show(self, text: str) -> None:
        self._output = text
        self.query_one("#settings-output", Static).update(colorize_log(text))

    def _work(
        self, run: Callable[[], str], then: Callable[[], None] | None = None
    ) -> None:
        """Run a configuration write in a worker and show its message."""
        self._set_busy(True)
        self._show(self.t("Выполняется…"))

        def work() -> None:
            try:
                message, ok = run(), True
            except (ValueError, OSError) as exc:
                message, ok = str(exc), False
            except Exception as exc:
                message, ok = f"{exc!r}", False
            call_from_worker(self, lambda: self._worked(message, ok, then))

        self.run_worker(work, thread=True, exclusive=True, group="settings-write")

    def _worked(self, message: str, ok: bool, then: Callable[[], None] | None) -> None:
        self._set_busy(False)
        self._show(message)
        self.reload()
        if ok and then is not None:
            then()
        elif not ok:
            self.notify(self.t("Не выполнено"), severity="error")

    def _run_command(self, entry: CatalogEntry, *, reload: bool) -> None:
        self._set_busy(True)
        line = entry.cli_line(self.repo)
        self._show(f"$ {line}\n" + self.t("Выполняется…"))
        argv = process_argv(entry.cli_argv(self.repo), self.repo)

        def work() -> None:
            try:
                result = self._runner(argv, cwd=self.repo)
                text, success = render_result(line, result), result.returncode == 0
            except OSError as exc:
                text, success = f"$ {line}\nне удалось запустить: {exc}", False
            except Exception as exc:
                text, success = f"$ {line}\nсбой запуска: {exc!r}", False
            call_from_worker(self, lambda: self._finished(text, success, reload))

        self.run_worker(work, thread=True, exclusive=True, group="settings-command")

    def _finished(self, text: str, success: bool, reload: bool) -> None:
        self._set_busy(False)
        if success and '"indexed": 0' in text:
            text += "\n" + self.t(
                "Проиндексировано 0 документов: в разрешённых путях нет подходящих файлов. "
                "Проверьте источники и пути в «Настроить память»."
            )
        self._show(text)
        self.notify(
            self.t("Готово") if success else self.t("Завершилось с ошибкой"),
            severity="information" if success else "error",
        )
        if reload:
            self.reload()
