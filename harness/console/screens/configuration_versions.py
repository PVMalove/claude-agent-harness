"""Select, compare and explicitly restore a local configuration generation."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Select, Static

from harness.configuration import Bundle, read_config_bundle
from harness.configuration_store import ConfigurationStore, ConfigurationVersion
from .commands import call_from_worker
from .. import brand, i18n
from ..crumbs import Crumb
from ..shell import SHELL_CSS, ConsoleShell, SectionCard
from ..logtext import colorize_diff
from ..configuration import bundle_diff
from ..widgets import ComposedPage
from .configuration import ConfirmConfiguration


class ConfigurationVersionsScreen(ConsoleShell):
    """Saved configuration versions on the shared frame: choose one, read its diff, restore it."""

    SECTION = "Configuration"
    CRUMBS = "Главная › Настройка › Версии"
    NARROW_FOOTER_KEYS = ("escape", "f2")
    # A view inside the Configuration section: a crumb returns to the wizard below, not here.
    IS_SECTION_ROOT = False
    CSS = SHELL_CSS + brand.shell_css("""
    ConfigurationVersionsScreen #versions-page { width: 100%; height: 1fr; padding: 0 2; }
    ConfigurationVersionsScreen #versions-scroll { width: 100%; height: 1fr; overflow-x: hidden; }
    ConfigurationVersionsScreen .actions { height: auto; }
    ConfigurationVersionsScreen Static { height: auto; }
    ConfigurationVersionsScreen #version-status { color: $text-muted; }
    ConfigurationVersionsScreen #version-diff-scroll { height: auto; max-height: 24; }
    """)

    def __init__(self, repo: Path) -> None:
        super().__init__()
        self.repo = repo
        self.store = ConfigurationStore(repo)
        self.selected: str | None = None
        self.expected: Bundle = {}
        self.candidate: Bundle | None = None
        self.busy = False
        self.versions: dict[str, ConfigurationVersion] = {}

    def compose_strip(self) -> ComposeResult:
        yield Static("Проект: " + self.repo.name, id="versions-project", markup=False)

    def compose_content(self) -> ComposeResult:
        yield ComposedPage(self._build_page, id="versions-page")

    def _build_page(self) -> ComposeResult:
        versions = self.store.versions()
        self.versions = {v.id: v for v in versions}
        with VerticalScroll(id="versions-scroll"):
            with SectionCard(title="Хранилище", id="versions-storage"):
                # Plain text: a path with spaces or square brackets must not be read as markup.
                yield Static(
                    f"Проект: {self.repo}\nВерсии: {self.store.root}",
                    id="versions-path",
                    markup=False,
                )
            with SectionCard(title="Версии", id="versions-list"):
                if not versions:
                    yield Static(
                        "Сохранённых версий нет. Сохраните текущую конфигурацию "
                        "в мастере настройки.",
                        id="versions-empty",
                        markup=False,
                    )
                yield Select(
                    [(_label(v), v.id) for v in versions],
                    value=versions[0].id if versions else Select.BLANK,
                    prompt="Выберите версию",
                    disabled=not versions,
                    id="configuration-version",
                )
            with SectionCard(title="Различия", id="versions-changes"):
                yield Static(
                    "Выберите версию; удалённые в ней файлы будут удалены и при откате.",
                    id="version-diff",
                    markup=False,
                )
        with SectionCard(title="Действия", id="versions-actions"):
            yield Static("", id="version-status", markup=False)
            with Horizontal(classes="actions"):
                yield Button("Восстановить версию", id="restore-version", disabled=True)

    async def _rebuild_page(self) -> None:
        """Rebuild the page below the frame; the menu, breadcrumbs and footer stay as they are."""
        await self.query_one("#versions-page", ComposedPage).recompose()
        self.translate_now()

    def on_select_changed(self, event: Select.Changed) -> None:
        if (
            event.select.id != "configuration-version"
            or not isinstance(event.value, str)
            or self.busy
        ):
            return
        self.selected = event.value
        try:
            self.expected = read_config_bundle(self.repo)
            self.candidate = self.store.load(self.selected)
            diff = bundle_diff(self.expected, self.candidate, "active", "version")
            self.query_one("#version-diff", Static).update(
                colorize_diff(
                    (diff or "Изменений нет") + self._compatibility_note(self.selected)
                )
            )
            self.query_one("#restore-version", Button).disabled = False
        except (OSError, ValueError) as exc:
            self.candidate = None
            self.query_one("#version-diff", Static).update(
                f"{exc}\nАктивная конфигурация не изменена. Версию нельзя применить как есть: "
                "примените другую версию или исправьте поля в мастере и сохраните новый набор."
            )
            self.query_one("#restore-version", Button).disabled = True

    def _compatibility_note(self, identifier: str) -> str:
        made = self.versions[identifier].package_version
        installed = self.store.installed_version()
        if "unknown" in (made, installed) or made == installed:
            return ""
        return (
            f"\nВерсия создана для Harness {made}, в проекте установлен {installed}: "
            "проверка контракта пройдена, но сверьте различия перед откатом."
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if (
            event.button.id == "restore-version"
            and self.candidate is not None
            and not self.busy
        ):
            self.app.push_screen(
                ConfirmConfiguration(
                    "Восстановить оба файла из версии "
                    + str(self.selected)
                    + "?\nЦель: "
                    + str(self.repo)
                    + "\nТекущий набор будет сохранён как новая версия. Отсутствующие в версии файлы будут удалены."
                ),
                self._confirmed,
            )

    def _confirmed(self, yes: bool | None) -> None:
        if not yes or self.candidate is None or self.selected is None:
            return
        self.busy = True
        self.query_one("#restore-version", Button).disabled = True
        selected, expected = self.selected, self.expected.copy()

        def work() -> None:
            try:
                # Recheck the immutable snapshot before writing; hashes/contract may have changed.
                backup = self.store.apply(self.store.load(selected), expected=expected)
                message = "Версия восстановлена. Обратный откат: " + backup.id
            except (ValueError, OSError) as exc:
                message = str(exc)
            call_from_worker(self, lambda: self._finished(message))

        self.run_worker(work, thread=True, group="restore", exclusive=True)

    async def _finished(self, message: str) -> None:
        self.busy = False
        await self._rebuild_page()
        self.query_one("#version-status", Static).update(message)

    # Esc, a number key, the menu and a breadcrumb all wait for a running restore.
    def _waiting(self) -> bool:
        if self.busy:
            language = getattr(self.app, "language", "ru")
            self.notify(
                i18n.tr(language, "Дождитесь окончания отката"), severity="warning"
            )
        return self.busy

    async def action_back(self) -> None:
        if not self._waiting():
            await super().action_back()

    async def open_section(self, index: int) -> None:
        if not self._waiting():
            await super().open_section(index)

    async def navigate_crumb(self, crumb: Crumb) -> None:
        if not self._waiting():
            await super().navigate_crumb(crumb)

    def on_unmount(self) -> None:
        self.workers.cancel_node(self)


def _label(version: ConfigurationVersion) -> str:
    if version.problem:
        return f"⚠ {version.id} · повреждена: {version.problem}"
    return (
        f"{version.created_at} · {version.description or version.id} · "
        f"Harness {version.package_version} · {', '.join(version.files) or 'без файлов'}"
    )
