"""Select, compare and explicitly restore a local configuration generation."""

from __future__ import annotations

import difflib
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Select, Static

from harness.configuration import Bundle, ConfigurationStore, read_config_bundle
from .commands import call_from_worker
from .configuration import ConfirmConfiguration


class ConfigurationVersionsScreen(Screen[None]):
    BINDINGS = [Binding("escape", "close", "Назад")]
    DEFAULT_CSS = """
    ConfigurationVersionsScreen .actions { height: auto; }
    ConfigurationVersionsScreen Static { height: auto; }
    ConfigurationVersionsScreen VerticalScroll { height: 1fr; margin: 0 1; }
    """

    def __init__(self, repo: Path) -> None:
        super().__init__()
        self.repo = repo
        self.store = ConfigurationStore(repo)
        self.selected: str | None = None
        self.expected: Bundle = {}
        self.candidate: Bundle | None = None
        self.busy = False

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll():
            yield Static(
                "Локальные версии конфигурации\nЦель: " + str(self.repo), markup=False
            )
            versions = self.store.versions()
            yield Select(
                [
                    (
                        f"{v.created_at} · {v.description or v.id} · Harness {v.package_version} · {', '.join(v.files) or 'без файлов'}",
                        v.id,
                    )
                    for v in versions
                ],
                value=versions[0].id if versions else Select.BLANK,
                id="configuration-version",
            )
            yield Static(
                "Выберите версию; удалённые в ней файлы будут удалены и при откате.",
                id="version-diff",
                markup=False,
            )
        with Horizontal(classes="actions"):
            yield Button("Откатиться к версии", id="restore-version", disabled=True)
            yield Button("Закрыть", id="close-versions")
        yield Static("", id="version-status", markup=False)
        yield Footer()

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
            lines: list[str] = []
            for name, raw in self.candidate.items():
                before = (self.expected[name] or b"").decode().splitlines(keepends=True)
                after = (raw or b"").decode().splitlines(keepends=True)
                lines.extend(
                    difflib.unified_diff(
                        before,
                        after,
                        fromfile="active/" + name,
                        tofile="version/" + name,
                    )
                )
                if raw is None and self.expected[name] is not None:
                    lines.append("Удалить " + name + "\n")
            self.query_one("#version-diff", Static).update(
                "".join(lines) or "Изменений нет"
            )
            self.query_one("#restore-version", Button).disabled = False
        except (OSError, ValueError) as exc:
            self.candidate = None
            self.query_one("#version-diff", Static).update(str(exc))
            self.query_one("#restore-version", Button).disabled = True

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close-versions":
            self.action_close()
        elif (
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
        await self.recompose()
        self.query_one("#version-status", Static).update(message)

    def action_close(self) -> None:
        if self.busy:
            self.notify("Дождитесь окончания отката", severity="warning")
        else:
            self.app.pop_screen()

    def on_unmount(self) -> None:
        self.workers.cancel_node(self)
