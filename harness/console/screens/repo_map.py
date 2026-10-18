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
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)
from textual.widgets.tree import TreeNode

from ..shell import SHELL_CSS, ConsoleShell, FieldGrid, Fold, SectionCard
from ..status import Status
from ..translate import Translated
from .. import repo_map as console_repo_map
from ..repo_map import RepoMapView
from ..runner import CommandRunner, capturing_runner
from .. import brand, i18n
from .. import repo_map_settings
from .commands import call_from_worker
from .repo_map_settings import RepoMapSettings
from harness.repo_map.bundle_fetch import ParserStatus, parser_status
from .export import (
    EXPORT_BINDING_KEY,
    EXPORT_JSON_BINDING_KEY,
    export_document,
    export_json,
)

_NO_MAP = "карты нет"
# Header buttons: the label on a wide terminal and on a narrow one.
_BUTTONS = {
    "build-map": ("b Собрать карту", "b Собрать"),
    "export-markdown": ("e Экспорт в Markdown", "e MD"),
    "export-json": ("j Экспорт в JSON", "j JSON"),
}
# Tab titles: on a wide terminal and on a narrow one.
_TABS = {
    "tab-summary": ("Сводка", "Свод"),
    "tab-files": ("Файлы", "Файлы"),
    "tab-hubs": ("Хабы", "Хабы"),
    "tab-diagnostics": ("Диагностики", "Диагн."),
    "tab-settings": ("Настройки", "Настр."),
}
_CACHE_NOTE = "Данные карты из кэша; сеть не используется"
_SETTINGS_NOTE = "Установка и сборка запускают команду из формы подтверждения"


class BuildConfirmScreen(Translated, ModalScreen[bool]):
    """Модальный экран подтверждения запуска построения карты Repo Map для коммита HEAD."""

    BINDINGS = [Binding("escape", "cancel", "Отмена")]
    DEFAULT_CSS = brand.shell_css("""
    BuildConfirmScreen { align: center middle; }
    BuildConfirmScreen #build-confirm {
        width: 80%; max-width: 100; height: auto; padding: 0 1;
        background: $surface; border: round $primary; border-title-color: $text-muted;
    }
    BuildConfirmScreen Static { height: auto; }
    BuildConfirmScreen #build-warning { color: $warning; margin: 1 0; }
    BuildConfirmScreen Horizontal { height: auto; }
    """)

    def __init__(self, commit: str) -> None:
        """Инициализирует экран подтверждения для указанного хэша коммита."""
        super().__init__()
        self.commit = commit

    def compose(self) -> ComposeResult:
        """Формирует структуру виджетов окна подтверждения построения карты."""
        with Vertical(id="build-confirm") as dialog:
            dialog.border_title = brand.frame_title("Подтверждение")
            yield Static(
                f"Построить Repo Map для HEAD {self.commit[:12]}?", markup=False
            )
            yield Static(
                console_repo_map.BUILD_WARNING, id="build-warning", markup=False
            )
            with Horizontal():
                yield Button("Собрать", id="confirm-build", variant="warning")
                yield Button("Отмена", id="cancel-build")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Обрабатывает нажатие кнопок подтверждения или отмены."""
        self.dismiss(event.button.id == "confirm-build")

    def action_cancel(self) -> None:
        """Обрабатывает отмену диалога по клавише Escape."""
        self.dismiss(False)


class RepoMapScreen(ConsoleShell):
    """Экран раздела Repo Map: визуализация дерева файлов, сигнатур, связей, хабов и диагностик."""

    CRUMBS = "Главная › Repo Map"
    SECTION = "Repo Map"
    NARROW_FOOTER_KEYS = ("escape", "b", "f2")
    AUTO_FOCUS = "Tabs"
    BINDINGS = [
        Binding("b", "build", "Собрать"),
        Binding(EXPORT_BINDING_KEY, "export", "Markdown"),
        Binding(EXPORT_JSON_BINDING_KEY, "export_json", "JSON"),
    ]
    CSS = SHELL_CSS + brand.shell_css("""
    RepoMapScreen #repo-map-head { height: auto; padding: 0 2; margin-bottom: 1; }
    RepoMapScreen #repo-map-title { height: 1; text-style: bold; }
    RepoMapScreen #repo-map-status { height: auto; }
    RepoMapScreen #repo-map-actions { height: 1; margin-top: 1; }
    RepoMapScreen #repo-map-actions Button { margin-right: 1; }
    RepoMapScreen .strip-note { text-align: right; color: $text-muted; }
    RepoMapScreen #repo-map-tabs { height: 1fr; padding: 0 2; }
    RepoMapScreen TabPane { padding: 1 0 0 0; }
    RepoMapScreen #tab-settings { padding: 0; }
    RepoMapScreen .section-card { margin-bottom: 1; }
    RepoMapScreen #file-search { height: auto; }
    RepoMapScreen #symbol-search { width: 1fr; }
    RepoMapScreen #search-status { width: auto; height: 3; content-align: left middle; padding-left: 2; color: $text-muted; }
    RepoMapScreen #file-browser { height: 1fr; }
    RepoMapScreen #files-card { width: 1fr; height: 1fr; margin-right: 1; margin-bottom: 0; }
    RepoMapScreen #file-tree { height: 1fr; background: transparent; }
    RepoMapScreen #relations-fold { width: 1fr; height: 1fr; margin-bottom: 0; }
    RepoMapScreen #relations-fold > Contents { height: 1fr; padding: 0; }
    RepoMapScreen #file-relations-scroll { height: 1fr; }
    RepoMapScreen #file-relations { height: auto; }
    RepoMapScreen.-wide #relations-fold > CollapsibleTitle, RepoMapScreen.-spacious #relations-fold > CollapsibleTitle { display: none; }
    RepoMapScreen #map-summary { margin-bottom: 1; }
    RepoMapScreen #map-summary-empty { height: auto; color: $text-muted; }
    RepoMapScreen #map-hubs, RepoMapScreen #map-diagnostics, RepoMapScreen #map-checks, RepoMapScreen #map-provenance { height: auto; }
    RepoMapScreen.-narrow #file-browser { layout: vertical; height: auto; }
    RepoMapScreen.-narrow #files-card { width: 100%; height: 14; margin-right: 0; }
    RepoMapScreen.-narrow #relations-fold { width: 100%; height: auto; }
    RepoMapScreen.-narrow #relations-fold > Contents { height: auto; padding: 1 0 0 0; }
    RepoMapScreen.-narrow #file-relations-scroll { height: auto; max-height: 10; }
    RepoMapScreen.-narrow #tab-files { overflow-y: auto; }
    RepoMapScreen.-narrow #search-status { height: 3; }
    RepoMapScreen.-narrow .strip-note { display: none; }
    """)

    def __init__(
        self,
        repo: Path,
        *,
        command_runner: CommandRunner = capturing_runner,
        find_head: Callable[[Path], str | None] = console_repo_map.head_commit,
        load_cached: Callable[
            [Path, str], RepoMapView | None
        ] = console_repo_map.cached_map,
        load_parsers: Callable[[Path], ParserStatus] = parser_status,
    ) -> None:
        """Инициализирует экран Repo Map для указанного репозитория."""
        super().__init__()
        self.repo = repo
        self._command_runner = command_runner
        self._load_parsers = load_parsers
        self.head = find_head(repo)
        self.view: RepoMapView | None = (
            load_cached(repo, self.head) if self.head is not None else None
        )
        self._status_text, self._status_kind = self._initial_status()
        self._check_serial = 0
        self._check_inputs: tuple[ParserStatus | None, dict[str, int]] | None = None
        self._narrow = False
        self._tab = "tab-summary"
        self._selected: str | None = None

    def _initial_status(self) -> tuple[str, str]:
        """The status line at opening, with the kind that gives it its symbol."""
        if self.head is None:
            return (
                "HEAD не найден: это не Git-репозиторий или в нём нет коммитов",
                "error",
            )
        if self.view is not None:
            return f"карта HEAD {self.head[:12]} из кэша · {self.view.tier}", "accepted"
        return (
            f"кэша карты для HEAD {self.head[:12]} нет — «Построить карту» (b)",
            "not_started",
        )

    @property
    def _language(self) -> str:
        return str(getattr(self.app, "language", "ru"))

    def _status_line(self) -> Text:
        """The status as a symbol and its text, coloured by kind."""
        text = i18n.tr_text(self._language, self._status_text)
        status = Status(self._status_kind, text, text)
        return Text(status.render(self._language), style=status.color)

    def _context_text(self) -> str:
        line = f"Проект: {self.repo.name}"
        if self._tab == "tab-settings":
            return line + " · политика: orchestration.json → repo_map_policy"
        return line + (f" · HEAD {self.head[:12]}" if self.head else "")

    def _context_note(self) -> str:
        return _SETTINGS_NOTE if self._tab == "tab-settings" else _CACHE_NOTE

    def compose_strip(self) -> ComposeResult:
        yield Static(
            i18n.tr_text("ru", self._context_text()),
            id="repo-map-context",
            classes="no-i18n",
            markup=False,
        )
        yield Static(
            self._context_note(),
            id="repo-map-note",
            classes="strip-note no-i18n",
            markup=False,
        )

    def compose_content(self) -> ComposeResult:
        """Формирует структуру виджетов и вкладок экрана Repo Map."""
        narrow = self.app.size.width < brand.NARROW_WIDTH
        with Vertical(id="repo-map-head"):
            yield Static("Repo Map", id="repo-map-title")
            yield Static(
                self._status_line(),
                id="repo-map-status",
                classes="no-i18n",
                markup=False,
            )
            with Horizontal(id="repo-map-actions", classes="actions"):
                for identifier, labels in _BUTTONS.items():
                    yield Button(
                        labels[narrow],
                        id=identifier,
                        disabled=identifier == "build-map" and self.head is None,
                        compact=True,
                    )
        with TabbedContent(id="repo-map-tabs"):
            with TabPane(_TABS["tab-summary"][narrow], id="tab-summary"):
                yield FieldGrid(id="map-summary")
                yield Static(_NO_MAP, id="map-summary-empty")
                with SectionCard(title="Provenance парсера", id="provenance-card"):
                    yield Static(
                        "", id="map-provenance", classes="no-i18n", markup=False
                    )
            with TabPane(_TABS["tab-files"][narrow], id="tab-files"):
                with Horizontal(id="file-search"):
                    yield Input(
                        placeholder="поиск символа в сигнатурах", id="symbol-search"
                    )
                    yield Static(
                        "", id="search-status", classes="no-i18n", markup=False
                    )
                with Horizontal(id="file-browser"):
                    with SectionCard(
                        title="Файлы", note="дерево · ←→ раскрыть", id="files-card"
                    ):
                        tree: Tree[str] = Tree("карта", id="file-tree")
                        yield tree
                    with Fold(
                        title="Связи",
                        summary="выберите файл",
                        collapsed=narrow,
                        id="relations-fold",
                    ):
                        with VerticalScroll(id="file-relations-scroll"):
                            yield Static(
                                "выберите файл",
                                id="file-relations",
                                classes="no-i18n",
                                markup=False,
                            )
            with TabPane(_TABS["tab-hubs"][narrow], id="tab-hubs"):
                with SectionCard(
                    title="Хабы", note="по входящей степени", id="hubs-card"
                ):
                    yield Static("", id="map-hubs", classes="no-i18n", markup=False)
            with TabPane(_TABS["tab-diagnostics"][narrow], id="tab-diagnostics"):
                with SectionCard(title="Проверка Repo Map", id="checks-frame"):
                    yield Static("", id="map-checks", classes="no-i18n", markup=False)
                    with Horizontal(classes="actions"):
                        yield Button("Проверить", id="run-checks", compact=True)
                with SectionCard(title="Диагностики карты", id="diagnostics-card"):
                    yield Static(
                        "", id="map-diagnostics", classes="no-i18n", markup=False
                    )
            with TabPane(_TABS["tab-settings"][narrow], id="tab-settings"):
                yield RepoMapSettings(
                    self.repo,
                    command_runner=self._command_runner,
                    load_parsers=self._load_parsers,
                )

    # -- language and width --------------------------------------------------------------------

    def _relabel(self) -> None:
        """Button and tab labels for the current width and language, the frame title of the
        relations card, and the texts built in code."""
        language = self._language
        narrow = int(self._narrow)
        for identifier, labels in _BUTTONS.items():
            self.query_one(f"#{identifier}", Button).label = i18n.tr(
                language, labels[narrow]
            )
        tabs = self.query_one("#repo-map-tabs", TabbedContent)
        for identifier, labels in _TABS.items():
            tabs.get_tab(identifier).label = i18n.tr(language, labels[narrow])
        fold = self.query_one("#relations-fold", Fold)
        fold.border_title = (
            "" if self._narrow else brand.section_title(i18n.tr(language, "Связи"))
        )
        self.query_one("#repo-map-status", Static).update(self._status_line())
        self.query_one("#repo-map-context", Static).update(
            i18n.tr_text(language, self._context_text())
        )
        self.query_one("#repo-map-note", Static).update(
            i18n.tr_text(language, self._context_note())
        )

    def _apply_layout(self) -> None:
        self.query_one("#relations-fold", Fold).collapsed = self._narrow
        self._relabel()

    def on_resize(self, event: events.Resize) -> None:
        narrow = event.size.width < brand.NARROW_WIDTH
        if narrow != self._narrow:
            self._narrow = narrow
            self._apply_layout()

    def sync_language(self) -> None:
        """Repaints the texts built in the current language: the checks and the settings pane."""
        ConsoleShell.sync_language(self)
        self._relabel()
        self._render_map()
        self._show_relations()
        self._paint_checks()
        for pane in self.query(RepoMapSettings):
            pane.repaint()

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        """The checks are read fresh each time their tab opens; the strip follows the tab."""
        self._tab = event.pane.id or self._tab
        self.query_one("#repo-map-context", Static).update(
            i18n.tr_text(self._language, self._context_text())
        )
        self.query_one("#repo-map-note", Static).update(
            i18n.tr_text(self._language, self._context_note())
        )
        if event.pane.id == "tab-diagnostics":
            self._run_checks()

    def _run_checks(self) -> None:
        """Read the parsers, languages and memory in a worker, then show the checks."""
        self.query_one("#map-checks", Static).update(
            i18n.tr_text(self._language, "Проверяю…")
        )

        self._check_serial += 1
        serial = self._check_serial

        def work() -> None:
            try:
                parsers: ParserStatus | None = self._load_parsers(self.repo)
            except Exception:
                parsers = None
            files = repo_map_settings.language_files(self.repo)
            call_from_worker(self, lambda: self._checked(serial, parsers, files))

        self.run_worker(work, thread=True, group="repo-map-checks")

    def _checked(
        self, serial: int, parsers: ParserStatus | None, files: dict[str, int]
    ) -> None:
        if serial != self._check_serial:
            return
        self._check_inputs = (parsers, files)
        self._paint_checks()

    def _paint_checks(self) -> None:
        if self._check_inputs is None:
            return
        parsers, files = self._check_inputs
        checks = repo_map_settings.repo_map_checks(
            head=self.head,
            tier=repo_map_settings.tier_setting(self.repo),
            parsers=parsers,
            files=files,
            memory=repo_map_settings.memory_status(self.repo),
            view=self.view,
            t=lambda text: i18n.tr_text(self._language, text),
        )
        text = Text()
        for index, check in enumerate(checks):
            color = brand.STATUS_COLORS.get(check.level, brand.PALETTE["muted"])
            text.append(
                ("\n" if index else "")
                + brand.STATUS_ICONS.get(check.level, "·")
                + " ",
                style=color,
            )
            text.append(check.text)
        self.query_one("#map-checks", Static).update(text)

    def on_mount(self) -> None:
        """Отрисовывает карту репозитория при монтировании экрана."""
        self._narrow = self.app.size.width < brand.NARROW_WIDTH
        self._apply_layout()
        self._render_map()
        self._run_checks()

    # -- the map -------------------------------------------------------------------------------

    def _render_map(self) -> None:
        view = self.view
        language = self._language
        summary = self.query_one("#map-summary", FieldGrid)
        empty = self.query_one("#map-summary-empty", Static)
        self.query_one("#provenance-card").display = view is not None
        if view is None:
            summary.set_fields([])
            empty.display = True
            self.query_one("#map-hubs", Static).update(_NO_MAP)
            self.query_one("#map-diagnostics", Static).update(_NO_MAP)
        else:
            empty.display = False
            summary.set_fields(console_repo_map.summary_fields(view))
            self.query_one("#map-provenance", Static).update(
                "\n".join(console_repo_map.provenance_lines(view))
            )
            self.query_one("#map-hubs", Static).update(self._hubs(view, language))
            self.query_one("#map-diagnostics", Static).update(
                self._diagnostics(view, language)
            )
        self._render_tree(self.query_one("#symbol-search", Input).value)

    @staticmethod
    def _hubs(view: RepoMapView, language: str) -> Text:
        text = console_repo_map.hubs_text(view)
        if not console_repo_map.hubs(view):
            return Text(i18n.tr_text(language, text))
        shown = Text()
        for index, line in enumerate(text.split("\n")):
            if index:
                shown.append("\n")
            count, _, path = line.partition("  ")
            shown.append(count, style=f"bold {brand.PALETTE['primary']}")
            shown.append("  " + path)
        return shown

    @staticmethod
    def _diagnostics(view: RepoMapView, language: str) -> Text:
        rows = console_repo_map.diagnostic_rows(view)
        if not rows:
            ok = Status("accepted", "диагностик нет", "no diagnostics")
            return Text(ok.render(language), style=ok.color)
        shown = Text()
        for index, (status, _, path) in enumerate(rows):
            if index:
                shown.append("\n")
            shown.append(status.render(language), style=status.color)
            shown.append(f": {path}")
        return shown

    def _render_tree(self, query: str) -> None:
        tree: Tree[str] = self.query_one("#file-tree", Tree)
        tree.clear()
        counter = self.query_one("#search-status", Static)
        view = self.view
        language = self._language
        if view is None:
            tree.root.set_label(i18n.tr(language, _NO_MAP))
            counter.update("")
            return
        tree.root.set_label(Text(f"{view.commit[:12]} · {view.tier}"))
        files = console_repo_map.search_files(view, query)
        searching = bool(query.strip())
        if language == "en":
            counter.update(
                f"found files: {len(files)} of {len(view.files)}"
                if searching
                else f"files: {len(view.files)}"
            )
        else:
            counter.update(
                f"найдено файлов: {len(files)} из {len(view.files)}"
                if searching
                else f"файлов: {len(view.files)}"
            )
        directories: dict[str, TreeNode[str]] = {"": tree.root}
        for item in files:
            parent_path = item.path.rpartition("/")[0]
            parent = self._directory_node(directories, parent_path)
            node = parent.add(self._file_label(item), data=item.path)
            for signature in item.signatures:
                node.add_leaf(Text(signature), data=item.path)
            if query.strip():
                node.expand()
        tree.root.expand()
        if query.strip():
            for node in directories.values():
                node.expand()

    @staticmethod
    def _file_label(item: console_repo_map.MapFile) -> Text:
        """A file in the tree: its name and parser status. `ok` is the green word in brackets; any
        other status also carries its symbol, so a colour is never the only cue."""
        status = console_repo_map.file_status(item)
        name = item.path.rsplit("/", 1)[-1]
        if status is None or item.parser_status == "ok":
            label = Text(name)
            if status is not None:
                label.append(f" [{item.parser_status}]", style=status.color)
            return label
        label = Text(f"{name} ")
        label.append(f"{status.symbol} [{item.parser_status}]", style=status.color)
        return label

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
        self._selected = path
        self._show_relations()

    def _show_relations(self) -> None:
        """The links of the selected file: a group per direction, kind and confidence, with the
        count, and the paths of the group whole, wrapping at the commas."""
        relations = self.query_one("#file-relations", Static)
        fold = self.query_one("#relations-fold", Fold)
        view, path, language = self.view, self._selected, self._language
        if view is None or path is None:
            relations.update(i18n.tr(language, "выберите файл"))
            fold.set_summary("выберите файл")
            return
        groups = console_repo_map.relations(view, path)
        shown = Text(path, style="bold")
        if not groups:
            shown.append("\n" + i18n.tr(language, "связей нет"))
        for group in groups:
            shown.append(
                "\n"
                + i18n.tr(language, group.direction)
                + f" · {group.kind} · {group.confidence} · {len(group.paths)}\n",
                style=brand.PALETTE["primary"],
            )
            shown.append("  " + ", ".join(group.paths))
        relations.update(shown)
        incoming, outgoing = console_repo_map.relation_counts(view, path)
        fold.set_summary(
            f"{path.rsplit('/', 1)[-1]} · {incoming} входящих · {outgoing} исходящих"
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        super().on_button_pressed(event)
        if event.button.id == "build-map":
            self.action_build()
        elif event.button.id == "export-markdown":
            self.action_export()
        elif event.button.id == "export-json":
            self.action_export_json()
        elif event.button.id == "run-checks":
            self._run_checks()

    def action_build(self) -> None:
        head = self.head
        if head is None:
            self.notify("HEAD не найден", severity="warning")
            return

        def on_confirm(confirmed: bool | None) -> None:
            if confirmed:
                self._build(head)

        self.app.push_screen(BuildConfirmScreen(head), on_confirm)

    def on_unmount(self) -> None:
        """Отменяет фоновые воркеры при размонтировании экрана."""
        self.workers.cancel_all()

    def _build(self, head: str) -> None:
        self._set_status(f"строится карта HEAD {head[:12]}…", "running")

        def work() -> None:
            outcome: RepoMapView | str
            try:
                outcome = console_repo_map.build_map(
                    self.repo, head, self._command_runner
                )
            except Exception as exc:
                # An unhandled worker error would exit the whole TUI; show it as the build outcome.
                outcome = f"сбой построения: {exc!r}"
            call_from_worker(self, lambda: self._apply_build(outcome))

        self.run_worker(work, thread=True, exclusive=True, group="repo-map-build")

    def _apply_build(self, outcome: RepoMapView | str) -> None:
        if isinstance(outcome, str):
            self._set_status(f"карта не построена: {outcome}", "error")
            return
        self.view = outcome
        self._selected = None
        self._set_status(f"карта HEAD {outcome.commit[:12]} построена", "accepted")
        self._show_relations()
        self._render_map()
        self._paint_checks()

    def _set_status(self, text: str, kind: str) -> None:
        self._status_text, self._status_kind = text, kind
        self.query_one("#repo-map-status", Static).update(self._status_line())

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
