"""Раздел Repo Map: карта для HEAD, отображаемая сразу из кэша результатов Repo Map либо строящаяся
по запросу. Модальное окно `BuildConfirmScreen` предупреждает, что CLI может установить parser bundle,
а отмена ничего не запускает. Вкладки: сводка с информацией о происхождении парсера (provenance);
дерево файлов с сигнатурами и статусом parser_status, поиск символов и связи выбранного файла
по типам и достоверности; хабы по входящей степени; диагностики. Экспорт карты в Markdown (`e`)
и JSON (`j`) выполняется через screens/export.py. Все данные берутся из harness.console.repo_map.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)
from textual.widgets.tree import TreeNode

from .. import repo_map as console_repo_map
from ..repo_map import RepoMapView
from ..runner import CommandRunner, capturing_runner
from .. import brand
from .export import (
    EXPORT_BINDING_KEY,
    EXPORT_JSON_BINDING_KEY,
    export_document,
    export_json,
)

_NO_MAP = "карты нет"


def _panel(title: str, content: Static, *, id: str | None = None) -> VerticalScroll:
    """A scrolling panel in a titled frame, like the frames of the other sections."""
    panel = VerticalScroll(content, id=id, classes="map-panel")
    panel.border_title = title
    return panel


class BuildConfirmScreen(ModalScreen[bool]):
    """Модальный экран подтверждения запуска построения карты Repo Map для коммита HEAD."""

    BINDINGS = [Binding("escape", "cancel", "Отмена")]

    def __init__(self, commit: str) -> None:
        """Инициализирует экран подтверждения для указанного хэша коммита."""
        super().__init__()
        self.commit = commit

    def compose(self) -> ComposeResult:
        """Формирует структуру виджетов окна подтверждения построения карты."""
        with Vertical(id="build-confirm"):
            yield Static(
                f"Построить Repo Map для HEAD {self.commit[:12]}?", markup=False
            )
            yield Static(
                console_repo_map.BUILD_WARNING, id="build-warning", markup=False
            )
            with Horizontal():
                yield Button("Построить", id="confirm-build", variant="warning")
                yield Button("Отмена", id="cancel-build")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Обрабатывает нажатие кнопок подтверждения или отмены."""
        self.dismiss(event.button.id == "confirm-build")

    def action_cancel(self) -> None:
        """Обрабатывает отмену диалога по клавише Escape."""
        self.dismiss(False)


class RepoMapScreen(Screen[None]):
    """Экран раздела Repo Map: визуализация дерева файлов, сигнатур, связей, хабов и диагностик."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Назад"),
        Binding("b", "build", "Построить карту"),
        Binding(EXPORT_BINDING_KEY, "export", "Экспорт в Markdown"),
        Binding(EXPORT_JSON_BINDING_KEY, "export_json", "Экспорт в JSON"),
    ]
    DEFAULT_CSS = """
    RepoMapScreen #repo-map-status {
        height: auto; margin: 0 1; padding: 0 1;
        border: round $primary 50%; border-title-color: $primary;
    }
    RepoMapScreen #repo-map-actions { height: auto; margin: 0 1; }
    RepoMapScreen #repo-map-tabs { height: 1fr; margin: 0 1; }
    RepoMapScreen .map-panel {
        height: 1fr; padding: 0 1; background: $background;
        border: round $panel-lighten-2; border-title-color: $primary;
    }
    RepoMapScreen .map-panel:focus, RepoMapScreen .map-panel:focus-within { border: round $primary; }
    RepoMapScreen #file-browser { height: 1fr; }
    RepoMapScreen #file-tree { width: 1fr; }
    RepoMapScreen #file-relations-scroll { width: 1fr; }
    RepoMapScreen #search-status { height: auto; padding: 0 1; color: $text-muted; }
    RepoMapScreen #build-confirm { height: auto; }
    """

    def __init__(
        self,
        repo: Path,
        *,
        command_runner: CommandRunner = capturing_runner,
        find_head: Callable[[Path], str | None] = console_repo_map.head_commit,
        load_cached: Callable[
            [Path, str], RepoMapView | None
        ] = console_repo_map.cached_map,
    ) -> None:
        """Инициализирует экран Repo Map для указанного репозитория."""
        super().__init__()
        self.repo = repo
        self._command_runner = command_runner
        self.head = find_head(repo)
        self.view: RepoMapView | None = (
            load_cached(repo, self.head) if self.head is not None else None
        )
        self._status_text = self._initial_status()

    def _initial_status(self) -> str:
        """Формирует начальный текст строки статуса при открытии экрана."""
        if self.head is None:
            return "HEAD не найден: это не Git-репозиторий или в нём нет коммитов"
        if self.view is not None:
            return f"карта HEAD {self.head[:12]} из кэша"
        return f"кэша карты для HEAD {self.head[:12]} нет — «Построить карту» (b)"

    def compose(self) -> ComposeResult:
        """Формирует структуру виджетов и вкладок экрана Repo Map."""
        yield Header(icon=brand.MENU_ICON)
        status = Static(self._status_text, id="repo-map-status", markup=False)
        status.border_title = "Состояние"
        yield status
        with Horizontal(id="repo-map-actions"):
            yield Button("Построить карту", id="build-map", disabled=self.head is None)
            yield Button("Экспорт в Markdown", id="export-markdown")
            yield Button("Экспорт в JSON", id="export-json")
        with TabbedContent(id="repo-map-tabs"):
            with TabPane("Сводка", id="tab-summary"):
                yield _panel("Сводка", Static("", id="map-summary", markup=False))
            with TabPane("Файлы", id="tab-files"):
                yield Input(
                    placeholder="поиск символа в сигнатурах", id="symbol-search"
                )
                yield Static("", id="search-status", markup=False)
                with Horizontal(id="file-browser"):
                    tree: Tree[str] = Tree("карта", id="file-tree", classes="map-panel")
                    tree.border_title = "Файлы"
                    yield tree
                    yield _panel(
                        "Связи",
                        Static("выберите файл", id="file-relations", markup=False),
                        id="file-relations-scroll",
                    )
            with TabPane("Хабы", id="tab-hubs"):
                yield _panel("Хабы", Static("", id="map-hubs", markup=False))
            with TabPane("Диагностики", id="tab-diagnostics"):
                yield _panel("Диагностики", Static("", id="map-diagnostics", markup=False))
        yield Footer()

    def on_mount(self) -> None:
        """Отрисовывает карту репозитория при монтировании экрана."""
        self._render_map()

    def _render_map(self) -> None:
        view = self.view
        summary = self.query_one("#map-summary", Static)
        if view is None:
            summary.update(_NO_MAP)
            self.query_one("#map-hubs", Static).update(_NO_MAP)
            self.query_one("#map-diagnostics", Static).update(_NO_MAP)
        else:
            summary.update(
                "\n".join(
                    [
                        *console_repo_map.summary_lines(view),
                        "",
                        "provenance парсера:",
                        *(
                            f"  {line}"
                            for line in console_repo_map.provenance_lines(view)
                        ),
                    ]
                )
            )
            self.query_one("#map-hubs", Static).update(console_repo_map.hubs_text(view))
            self.query_one("#map-diagnostics", Static).update(
                console_repo_map.diagnostics_text(view)
            )
        self._render_tree(self.query_one("#symbol-search", Input).value)

    def _render_tree(self, query: str) -> None:
        tree: Tree[str] = self.query_one("#file-tree", Tree)
        tree.clear()
        status = self.query_one("#search-status", Static)
        view = self.view
        if view is None:
            tree.root.set_label(_NO_MAP)
            status.update("")
            return
        tree.root.set_label(Text(f"{view.commit[:12]} · {view.tier}"))
        files = console_repo_map.search_files(view, query)
        status.update(
            f"найдено файлов: {len(files)} из {len(view.files)}"
            if query.strip()
            else f"файлов: {len(view.files)}"
        )
        directories: dict[str, TreeNode[str]] = {"": tree.root}
        for item in files:
            parent_path = item.path.rpartition("/")[0]
            parent = self._directory_node(directories, parent_path)
            node = parent.add(Text(console_repo_map.file_label(item)), data=item.path)
            for signature in item.signatures:
                node.add_leaf(Text(signature), data=item.path)
            if query.strip():
                node.expand()
        tree.root.expand()
        if query.strip():
            for node in directories.values():
                node.expand()

    def _directory_node(
        self, directories: dict[str, TreeNode[str]], path: str
    ) -> TreeNode[str]:
        node = directories.get(path)
        if node is not None:
            return node
        head, _, name = path.rpartition("/")
        node = self._directory_node(directories, head).add(Text(f"{name}/"))
        directories[path] = node
        return node

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "symbol-search":
            self._render_tree(event.value)

    def on_tree_node_selected(self, event: Tree.NodeSelected[str]) -> None:
        path = event.node.data
        if path is None or self.view is None:
            return
        self.query_one("#file-relations", Static).update(
            console_repo_map.relations_text(self.view, path)
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "build-map":
            self.action_build()
        elif event.button.id == "export-markdown":
            self.action_export()
        elif event.button.id == "export-json":
            self.action_export_json()

    def action_build(self) -> None:
        head = self.head
        if head is None:
            self.notify("HEAD не найден", severity="warning")
            return

        def on_confirm(confirmed: bool | None) -> None:
            if confirmed:
                self._build(head)

        self.app.push_screen(BuildConfirmScreen(head), on_confirm)

    def _build(self, head: str) -> None:
        self._set_status(f"строится карта HEAD {head[:12]}…")

        def work() -> None:
            outcome = console_repo_map.build_map(self.repo, head, self._command_runner)
            self.app.call_from_thread(self._apply_build, outcome)

        self.run_worker(work, thread=True, exclusive=True, group="repo-map-build")

    def _apply_build(self, outcome: RepoMapView | str) -> None:
        if isinstance(outcome, str):
            self._set_status(f"карта не построена: {outcome}")
            return
        self.view = outcome
        self._set_status(f"карта HEAD {outcome.commit[:12]} построена")
        self.query_one("#file-relations", Static).update("выберите файл")
        self._render_map()

    def _set_status(self, text: str) -> None:
        self._status_text = text
        self.query_one("#repo-map-status", Static).update(text)

    def action_export(self) -> None:
        if self.view is None:
            self.notify("нечего экспортировать: карты нет", severity="warning")
            return
        export_document(self, self.repo, console_repo_map.map_document(self.view))

    def action_export_json(self) -> None:
        if self.view is None:
            self.notify("нечего экспортировать: карты нет", severity="warning")
            return
        export_json(
            self,
            self.repo,
            console_repo_map.map_json(self.view),
            slug=console_repo_map.export_slug(self.view),
        )
