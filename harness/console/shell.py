"""Shared visual shell of the console's workspace screens; navigation has no execution side effects."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from rich.cells import cell_len
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import ActiveBinding, Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.markup import escape
from textual.message import Message
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Button, Collapsible, Footer, ListItem, ListView, Static
from typing_extensions import TypeVar

from . import brand, i18n
from .chrome import WorkspaceHeader
from .crumbs import ELLIPSIS, Crumb, collapse_crumbs, split_path
from .data import DashboardData
from .wrap import wrap_value
from .translate import Translated

CONFIGURATION_SECTION = "Configuration"
HOME_SECTION = "Home"
# The numbered main menu, in the order of the number keys 0-9.
SECTIONS = (
    HOME_SECTION,
    "Open project",
    "Update Harness",
    "Diagnostics",
    "Harness",
    "Orchestration",
    "Reports",
    "Repo Map",
    CONFIGURATION_SECTION,
    "Help",
)


@dataclass(frozen=True)
class CheckRoute:
    """Where a failed health check is dealt with: its readable title and the menu section."""

    title: str
    section: str


# Every health check -> its readable title and the menu section that deals with it. Home's
# "Requires attention", Diagnostics and Open project use it for jumps like `Update Harness 2 →`.
CHECK_ROUTES: dict[str, CheckRoute] = {
    "files.skill_snapshot": CheckRoute(
        "Скиллы отличаются от установленной версии Harness", "Update Harness"
    ),
    "files.skill_registry": CheckRoute(
        "Реестр скиллов не совпадает со скиллами", "Update Harness"
    ),
    "files.agents_md": CheckRoute(
        "AGENTS.md отсутствует или устарел", "Update Harness"
    ),
    "files.discovery_links": CheckRoute(
        "Ссылки обнаружения скиллов нарушены", "Update Harness"
    ),
    "files.overlay_locks": CheckRoute(
        "Блокировки оверлеев не совпадают", "Update Harness"
    ),
    "files.lock": CheckRoute("Harness не установлен в проект", "Open project"),
    "files.project_json": CheckRoute("Ошибка в .harness/project.json", "Configuration"),
    "files.integrations": CheckRoute(
        "Нужно проверить настройки интеграций", "Configuration"
    ),
    "files.verification_routing": CheckRoute(
        "Маршрутизация проверок настроена неверно", "Configuration"
    ),
    "tracker.project": CheckRoute("Трекер задач не определён", "Configuration"),
    "tracker.auth": CheckRoute("Нет доступа к трекеру задач", "Configuration"),
    "tracker.reachability": CheckRoute("Трекер задач недоступен", "Configuration"),
    "tracker.permissions": CheckRoute(
        "Недостаточно прав в трекере задач", "Configuration"
    ),
    "tracker.labels": CheckRoute("В трекере не хватает меток", "Configuration"),
    "tracker.git_base": CheckRoute(
        "В тикете не указана ветка интеграции", "Configuration"
    ),
    "files.orchestration_config": CheckRoute(
        "Ошибка в настройках оркестрации", "Orchestration"
    ),
    "directories.orchestration_state": CheckRoute(
        "Каталог состояния оркестрации недоступен", "Orchestration"
    ),
    "orchestration.ledger_summary": CheckRoute(
        "Ledger оркестрации требует внимания", "Orchestration"
    ),
    "orchestration.unfinished_batches": CheckRoute(
        "Есть незавершённые batch", "Orchestration"
    ),
    "orchestration.blocked_batches": CheckRoute(
        "Есть заблокированные batch", "Orchestration"
    ),
    "orchestration.stale_dispatches": CheckRoute(
        "Есть зависшие dispatch", "Orchestration"
    ),
    "orchestration.orphaned_worktrees": CheckRoute(
        "Есть worktree без batch", "Orchestration"
    ),
    "orchestration.disposable_data": CheckRoute(
        "Накопились временные данные оркестрации", "Orchestration"
    ),
    "repo_map.tier": CheckRoute("Repo Map работает в ограниченном режиме", "Repo Map"),
    "memory.index": CheckRoute("Индекс памяти проекта требует внимания", "Repo Map"),
    "memory.model": CheckRoute("Модель памяти недоступна", "Repo Map"),
    "environment.os": CheckRoute(
        "Операционная система не поддерживается", "Diagnostics"
    ),
    "environment.git": CheckRoute("Git недоступен", "Diagnostics"),
    "environment.git_identity": CheckRoute(
        "Не настроен автор коммитов Git", "Diagnostics"
    ),
    "environment.gitattributes": CheckRoute("Нет файла .gitattributes", "Diagnostics"),
    "environment.line_endings": CheckRoute(
        "Переводы строк не совпадают с .gitattributes", "Diagnostics"
    ),
    "environment.python": CheckRoute("Неподходящая версия Python", "Diagnostics"),
    "environment.uv": CheckRoute("uv недоступен", "Diagnostics"),
    "environment.glab": CheckRoute("Версия glab устарела", "Diagnostics"),
    "environment.dev_env": CheckRoute(
        "Окружение .harness/.venv не совпадает с uv.lock", "Diagnostics"
    ),
    "environment.output_encoding": CheckRoute(
        "Кодировка вывода не UTF-8", "Diagnostics"
    ),
    "environment.codex_sandbox": CheckRoute(
        "Песочница Codex настроена неверно", "Diagnostics"
    ),
    "environment.claude_sandbox": CheckRoute(
        "Песочница Claude настроена неверно", "Diagnostics"
    ),
    "environment.codex_uv_cache": CheckRoute(
        "Кэш uv недоступен в песочнице Codex", "Diagnostics"
    ),
    "environment.claude_uv_cache": CheckRoute(
        "Кэш uv недоступен в песочнице Claude", "Diagnostics"
    ),
    "environment.long_paths": CheckRoute(
        "Длинные пути Windows выключены", "Diagnostics"
    ),
    "environment.path_length": CheckRoute(
        "Путь к проекту слишком длинный", "Diagnostics"
    ),
    "environment.pytest_temp": CheckRoute(
        "Временный каталог pytest недоступен", "Diagnostics"
    ),
    "environment.symlinks": CheckRoute(
        "Символические ссылки недоступны", "Diagnostics"
    ),
    "environment.hook_bash": CheckRoute("Bash для hooks не найден", "Diagnostics"),
    "hooks.policy": CheckRoute("Hooks проекта подключены неверно", "Update Harness"),
    "files.sandboxes": CheckRoute("Каталог .sandboxes настроен неверно", "Diagnostics"),
    "directories.harness": CheckRoute("Каталог Harness недоступен", "Diagnostics"),
    "directories.sandboxes": CheckRoute("Каталог Harness недоступен", "Diagnostics"),
    "directories.cache": CheckRoute("Каталог Harness недоступен", "Diagnostics"),
    "directories.logs": CheckRoute("Каталог Harness недоступен", "Diagnostics"),
    "directories.scratch": CheckRoute("Каталог Harness недоступен", "Diagnostics"),
    "directories.pr_body": CheckRoute("Каталог Harness недоступен", "Diagnostics"),
    "directories.runs": CheckRoute("Каталог Harness недоступен", "Diagnostics"),
    "directories.reports": CheckRoute("Каталог Harness недоступен", "Diagnostics"),
    "directories.worktrees": CheckRoute("Каталог Harness недоступен", "Diagnostics"),
}


def check_route(check_id: str) -> CheckRoute:
    """The route of `check_id`; a check outside the table keeps its id and opens Diagnostics."""
    return CHECK_ROUTES.get(check_id) or CheckRoute(check_id, "Diagnostics")


class ShellSidebar(Vertical):
    """The main menu column. Its look lives in `SHELL_CSS`, with the rest of the shell."""

    def __init__(self, menu: ListView, *, id: str) -> None:
        super().__init__(id=id)
        self.menu = menu

    def compose(self) -> ComposeResult:
        yield Static("ГЛАВНОЕ МЕНЮ", classes="menu-heading")
        yield self.menu


class CrumbLink(Static, can_focus=True):
    """A breadcrumb segment that opens its screen on a click or on Enter."""

    BINDINGS = [Binding("enter", "select", show=False)]

    class Selected(Message):
        def __init__(self, crumb: Crumb) -> None:
            super().__init__()
            self.crumb = crumb

    def __init__(self, crumb: Crumb, text: str) -> None:
        super().__init__(text, classes="crumb-link no-i18n", markup=False)
        self.crumb = crumb

    def on_click(self) -> None:
        self.post_message(self.Selected(self.crumb))

    def action_select(self) -> None:
        self.post_message(self.Selected(self.crumb))


class JumpLink(Static, can_focus=True):
    """A jump to a menu section, `Обновить Harness  2 →` (only `2 →` on a narrow terminal): a
    click or Enter opens the section as its number key does. It runs no command."""

    BINDINGS = [Binding("enter", "select", show=False)]

    class Selected(Message):
        def __init__(self, section: str) -> None:
            super().__init__()
            self.section = section

    def __init__(self, section: str, *, id: str | None = None) -> None:
        super().__init__("", id=id, classes="jump-link no-i18n", markup=False)
        self.section = section

    def on_mount(self) -> None:
        self.relabel()

    def relabel(self) -> None:
        number = SECTIONS.index(self.section)
        if self.app.size.width < brand.NARROW_WIDTH:
            text = f"{number} →"
        else:
            language = getattr(self.app, "language", "ru")
            text = f"{i18n.jump_label(language, self.section)}  {number} →"
        if text != str(self.content):
            self.update(text)

    def on_click(self) -> None:
        self.post_message(self.Selected(self.section))

    def action_select(self) -> None:
        self.post_message(self.Selected(self.section))


class Breadcrumbs(Horizontal):
    """The path to the current screen: every segment but the last is a link, and a narrow terminal
    folds the middle segments into an ellipsis."""

    DEFAULT_CLASSES = "breadcrumbs"

    def __init__(self, crumbs: Sequence[Crumb], *, id: str) -> None:
        super().__init__(id=id)
        self._crumbs = list(crumbs)
        self._key: tuple[tuple[str, ...], tuple[int | None, ...]] = ((), ())

    def set_crumbs(self, crumbs: Sequence[Crumb]) -> None:
        self._crumbs = list(crumbs)
        self.relabel()

    def _labels(self) -> list[str]:
        language = getattr(self.app, "language", "ru")
        return [i18n.tr_text(language, crumb.label) for crumb in self._crumbs]

    def _shown(self, labels: Sequence[str]) -> list[int | None]:
        width = self.content_size.width or self.app.size.width
        return collapse_crumbs(labels, width)

    def relabel(self) -> None:
        """Redraw the segments after a change of the path, the language or the width."""
        if not self.is_attached:
            return
        labels = self._labels()
        shown = self._shown(labels)
        key = (tuple(labels), tuple(shown))
        if key == self._key:
            return
        self._key = key
        last = len(self._crumbs) - 1
        parts: list[Widget] = []
        for position, index in enumerate(shown):
            if position:
                parts.append(Static(" › ", classes="crumb-sep no-i18n", markup=False))
            if index is None:
                parts.append(Static(ELLIPSIS, classes="crumb-sep no-i18n"))
                continue
            crumb = self._crumbs[index]
            if index == last:
                parts.append(
                    Static(labels[index], classes="crumb-current no-i18n", markup=False)
                )
            elif crumb.is_link:
                parts.append(CrumbLink(crumb, labels[index]))
            else:
                parts.append(
                    Static(labels[index], classes="crumb-text no-i18n", markup=False)
                )
        self.remove_children()
        self.mount_all(parts)

    def on_mount(self) -> None:
        self.relabel()

    def on_resize(self) -> None:
        self.relabel()


class ContextStrip(Horizontal):
    """One line (wrapping when it must) under the breadcrumbs: what the screen is about."""

    DEFAULT_CLASSES = "context-strip"


class WrappedValue(Static):
    """A value without spaces - a path, an ID, a branch - that wraps at its separators."""

    DEFAULT_CLASSES = "no-i18n"

    def __init__(self, value: str = "", **kwargs: object) -> None:
        super().__init__(value, markup=False, **kwargs)  # type: ignore[arg-type]
        self._value = value

    def set_value(self, value: str) -> None:
        self._value = value
        self._wrap()

    def _wrap(self) -> None:
        text = wrap_value(self._value, self.content_size.width)
        if text != self.content:
            self.update(text)

    def on_resize(self) -> None:
        self._wrap()


class ElidedPath(Static):
    """A path on one line, cut from the left to the room it has (`…/Code/project`); the whole
    path is its tooltip."""

    DEFAULT_CLASSES = "elided-path no-i18n"

    def __init__(self, path: str, *, id: str | None = None) -> None:
        super().__init__(path, id=id, markup=False)
        self.path = path
        self.tooltip = path

    def on_resize(self) -> None:
        width = self.content_size.width
        text = brand.elide_path(self.path, width) if width else self.path
        if text != str(self.content):
            self.update(text)


class ContextPanel(VerticalScroll):
    """Facts beside the content; folded under it on a narrow terminal, never hidden."""

    DEFAULT_CLASSES = "context-panel"


class SectionCard(Vertical):
    """A block of a page: a bordered card whose top border carries the section title in capitals
    and, after it, a counter or a note. The title follows the console language by itself."""

    def __init__(
        self,
        *children: Widget,
        title: str,
        note: str = "",
        id: str | None = None,
        classes: str = "",
    ) -> None:
        super().__init__(
            *children, id=id, classes=f"section-card no-i18n {classes}".strip()
        )
        self._title = title
        self._note = note

    def on_mount(self) -> None:
        self.relabel()

    def set_note(self, note: str) -> None:
        self._note = note
        self.relabel()

    def relabel(self) -> None:
        language = getattr(self.app, "language", "ru")
        self.border_title = brand.section_title(
            i18n.tr(language, self._title), i18n.tr_text(language, self._note)
        )


class Fold(Collapsible):
    """A folded section card, `▸ Title  summary`: Enter or a click opens it. The summary says what
    is inside without opening it."""

    def __init__(
        self,
        *children: Widget,
        title: str,
        summary: str = "",
        collapsed: bool = True,
        id: str | None = None,
        classes: str = "",
    ) -> None:
        super().__init__(
            *children,
            title=title,
            collapsed=collapsed,
            collapsed_symbol="▸",
            expanded_symbol="▾",
            id=id,
            classes=f"section-card fold no-i18n {classes}".strip(),
        )
        self._source = title
        self._summary = summary

    def on_mount(self) -> None:
        self.relabel()

    def set_summary(self, summary: str) -> None:
        self._summary = summary
        self.relabel()

    def on_resize(self) -> None:
        self.relabel()

    def relabel(self) -> None:
        language = getattr(self.app, "language", "ru")
        title = i18n.tr(language, self._source)
        summary = i18n.tr_text(language, self._summary)
        if not summary:
            self.title = escape(title)
            return
        # The summary sits at the right end of the title row; the symbol and a space come first.
        room = self.content_size.width - 2
        gap = max(2, room - cell_len(title) - cell_len(summary))
        self.title = (
            f"{escape(title)}{' ' * gap}[{brand.PALETTE['muted']}]{escape(summary)}[/]"
        )


class FieldGrid(Widget):
    """Label/value fields in two columns on a wide terminal and one on a narrow one (the first half
    of the fields in the first column, the rest in the second). A value wraps, so a long ID or path
    stays whole on screen."""

    DEFAULT_CLASSES = "field-grid"

    def __init__(
        self,
        fields: Sequence[tuple[str, str]] = (),
        *,
        translate_values: bool = False,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._fields = tuple(fields)
        # Values the screen writes in Russian (statistics) follow the language; data (IDs, paths) does not.
        self._translate_values = translate_values

    def _columns(self) -> ComposeResult:
        half = (len(self._fields) + 1) // 2
        for chunk in (self._fields[:half], self._fields[half:]):
            if chunk:
                yield Vertical(
                    *(
                        Static(
                            self._cell(label, value),
                            classes="field no-i18n",
                            markup=False,
                        )
                        for label, value in chunk
                    ),
                    classes="field-column",
                )

    def compose(self) -> ComposeResult:
        yield from self._columns()

    def _cell(self, label: str, value: str, width: int = 0) -> Text:
        language = getattr(self.app, "language", "ru")
        if self._translate_values:
            value = i18n.tr_text(language, value)
        return Text.assemble(
            (i18n.tr(language, label) + "\n", brand.PALETTE["muted"]),
            wrap_value(value or "—", width),
        )

    def set_fields(self, fields: Sequence[tuple[str, str]]) -> None:
        """Show `fields` in place of the current ones."""
        self._fields = tuple(fields)
        self.remove_children()
        self.mount_all(self._columns())

    def relabel(self) -> None:
        """Redraw the cells for the current language and width: a value breaks at its separators."""
        for cell, (label, value) in zip(
            self.query(".field").results(Static), self._fields
        ):
            text = self._cell(label, value, cell.content_size.width)
            if text != cell.content:
                cell.update(text)

    def on_resize(self) -> None:
        self.relabel()


@dataclass(frozen=True)
class ShellIds:
    """DOM ids of a shell screen's parts; a screen overrides them to keep its established ids."""

    navigation: str = "shell-navigation"
    menu: str = "shell-sections"
    content: str = "shell-main"
    breadcrumbs: str = "shell-breadcrumbs"
    toggle: str = "shell-menu"
    body: str = "shell-body"
    context: str = "shell-context"


# Every rule of the shell, once. Screens load it through their `CSS` (a screen's own rules are
# appended to it), so it outranks the app's generic widget rules in any host app. Rules for a
# single screen's widgets belong to that screen, never here.
SHELL_CSS = brand.shell_css("""
Screen.shell { background: $background; }
.shell .crumb-row { height: auto; min-height: 2; padding: 0 2; border-bottom: solid $border-subtle; color: $foreground; }
.shell .crumb-row .menu-toggle { padding: 0; margin-right: 1; color: $primary; text-style: bold; }
.shell .breadcrumbs { width: 1fr; height: auto; }
.shell .breadcrumbs Static { width: auto; height: 1; }
.shell .crumb-link { color: $primary; text-style: underline; }
.shell .crumb-link:hover { background: $row-highlight; color: $selection-foreground; }
.shell .crumb-link:focus { background: $selection-background; color: $selection-foreground; text-style: bold underline; }
.shell .crumb-sep { color: $text-muted; }
.shell .jump-link { width: auto; height: 1; color: $primary; }
.shell .segment { width: auto; height: 1; }
.shell .segment Button { color: $foreground; }
.shell .segment Button.-on { background: $selection-background; color: $selection-foreground; }
.shell .segment .segment-sep { width: 1; color: $component-border; }
.shell .jump-link:hover { background: $row-highlight; color: $selection-foreground; }
.shell .jump-link:focus { background: $selection-background; color: $selection-foreground; text-style: bold; }
.shell .crumb-current { width: 1fr; min-width: 1; text-style: bold; text-wrap: nowrap; text-overflow: ellipsis; }
.shell .context-strip { height: auto; min-height: 2; border-bottom: solid $border-subtle; padding: 0 2; color: $text-muted; }
.shell .context-strip Static { width: 1fr; height: auto; }
.shell .context-strip .elided-path { height: 1; text-wrap: nowrap; }
.shell .narrow-only { display: none; }
.shell.-narrow .narrow-only { display: block; }
.shell.-narrow .wide-only { display: none; }
.shell Button { min-width: 8; width: auto; color: $foreground; border: round $component-border; text-style: none; }
.shell Button:hover, .shell Button:focus { border: round $primary; color: $primary; }
.shell .context-strip Button { border: none; height: 1; min-width: 3; color: $primary; }
.shell Button.-textual-compact, .shell Button.-textual-compact:hover, .shell Button.-textual-compact:focus { border: none; padding: 0 1; height: 1; min-height: 1; min-width: 0; }
.shell .context-panel { width: 30; height: 1fr; padding: 0 2; overflow-x: hidden; scrollbar-gutter: stable; border-left: solid $border-subtle; background: $surface; }
.shell .context-panel Static { height: auto; }
.shell .context-label { height: 2; color: $text-muted; margin-top: 1; }
.shell .context-value { height: auto; margin-bottom: 1; }
.shell .context-note { height: auto; border: round $component-border; padding: 0 1; margin-top: 1; color: $text-muted; }
.shell ShellSidebar { width: 30; height: 1fr; background: $surface; border-right: solid $border-subtle; padding: 1; }
.shell ShellSidebar .menu-heading { height: 2; padding-left: 2; color: $text-muted; }
.shell ShellSidebar ListView, .shell ShellSidebar ListView:focus { height: 1fr; border: none; padding: 0; margin: 0; background: $surface; }
.shell ShellSidebar ListView > ListItem { height: 3; padding: 1 2; color: $text-muted; background: $surface; text-style: none; }
.shell ShellSidebar ListView > ListItem.-highlight, .shell ShellSidebar ListView:focus > ListItem.-highlight {
    background: $selection-background; color: $selection-foreground; text-style: none; outline: round $selection-outline;
}
.shell Footer, .shell WorkspaceHeader { background: $surface; }
.shell ListView, .shell ListView:focus { background-tint: transparent; }
.shell ShellSidebar ListView > ListItem.-active { color: $selection-foreground; }
.shell .shell-frame { height: 1fr; }
.shell .shell-main { width: 1fr; min-width: 0; height: 1fr; }
.shell .shell-body { height: 1fr; }
.shell .shell-content { width: 1fr; min-width: 0; height: 1fr; }
.shell .menu-toggle { display: none; }
.shell .field-grid { layout: horizontal; height: auto; }
.shell .field-column { width: 1fr; height: auto; padding-right: 2; }
.shell .field { height: auto; margin-bottom: 1; }
.shell.-spacious .context-panel { width: 38; }
.shell.-wide ShellSidebar { width: 27; }
.shell.-narrow .menu-toggle { display: block; }
.shell.-narrow ShellSidebar { display: none; }
.shell.-narrow.-menu-open ShellSidebar { display: block; }
.shell.-narrow .shell-body, .shell.-fold-context .shell-body { layout: vertical; overflow-y: auto; }
.shell.-narrow .shell-content, .shell.-fold-context .shell-content { min-height: 100%; }
.shell.-narrow .context-panel, .shell.-fold-context .context-panel { width: 100%; height: auto; border-left: none; border-top: solid $border-subtle; }
.shell .section-card { height: auto; padding: 0 1; background: $surface; border: round $border-subtle; }
.shell .section-card.-alert { border-left: outer $error; }
.shell .fold { padding: 0 1; }
.shell .fold > CollapsibleTitle { width: 1fr; padding: 0; color: $foreground; background: transparent; }
.shell .fold > Contents { padding: 1 0 0 0; height: auto; }
.shell .context-fold { border: none; padding: 0; background: transparent; }
.shell .context-fold > Contents { padding: 0; }
.shell .context-fold > CollapsibleTitle { display: none; }
.shell.-fold-context .context-fold { border: round $border-subtle; padding: 0 1; background: $surface; }
.shell.-fold-context .context-fold > CollapsibleTitle { display: block; }
.shell * { border-title-color: $text-muted !important; border-title-style: none !important; }
.shell.-narrow .field-grid { layout: vertical; }
.shell.-narrow .field-column { width: 100%; padding-right: 0; }
.shell.-narrow ShellSidebar { padding: 0 1; }
.shell.-narrow ShellSidebar .menu-heading { height: 1; padding-left: 1; }
.shell.-short ShellSidebar ListView > ListItem { height: 1; padding: 0 2; }
.shell.-narrow ShellSidebar ListView > ListItem { height: 1; padding: 0 1; }
.shell.-narrow ShellSidebar ListView > ListItem.-highlight, .shell.-narrow ShellSidebar ListView:focus > ListItem.-highlight,
.shell.-short ShellSidebar ListView > ListItem.-highlight, .shell.-short ShellSidebar ListView:focus > ListItem.-highlight { outline: none; }
.shell.-narrow WorkspaceHeader { height: 2; padding: 0 2; }
.shell.-narrow .crumb-row, .shell.-narrow .context-strip { min-height: 1; border: none; }
.shell * { scrollbar-color: $scrollbar-thumb; scrollbar-color-hover: $primary; scrollbar-color-active: $primary; scrollbar-background: $scrollbar-track; scrollbar-background-hover: $scrollbar-track; scrollbar-background-active: $scrollbar-track; }
.shell ListView, .shell .frame, .shell Input, .shell TextArea, .shell Select > SelectCurrent { border: round $component-border; }
.shell ListView:focus, .shell .frame:focus, .shell .frame:focus-within, .shell Input:focus, .shell TextArea:focus, .shell Select:focus > SelectCurrent { border: round $primary; }
""")


# A menu entry's short status: (text, colour).
Badge = tuple[str, str]


def section_badges(data: DashboardData) -> dict[str, Badge]:
    """A short status per menu section from the home screen's health facts; a section without a
    status has no badge. Every shell screen shows them, so they are computed here once."""
    colors = brand.STATUS_COLORS
    icons = brand.STATUS_ICONS
    badges: dict[str, Badge] = {}
    if data.fail:
        badges["Diagnostics"] = (f"{icons['fail']} {data.fail}", colors["fail"])
    elif data.warn:
        badges["Diagnostics"] = (f"{icons['warn']} {data.warn}", colors["warn"])
    else:
        badges["Diagnostics"] = (icons["ok"], colors["ok"])
    if data.drift_state == "clean":
        badges["Harness"] = (icons["ok"], colors["ok"])
    elif data.drift_state == "drift":
        # The installed files drifted from the package: the update brings them back.
        badges["Harness"] = (icons["warn"], colors["warn"])
        badges["Update Harness"] = (icons["warn"], colors["warn"])
    if data.active_batches is not None:
        badges["Orchestration"] = (str(data.active_batches), brand.PALETTE["muted"])
    tier = repo_map_tier_name(data.repo_map_tier)
    if tier is not None:
        badges["Repo Map"] = (tier, colors["ok"] if tier == "full" else colors["warn"])
    return badges


def repo_map_tier_name(message: str) -> str | None:
    """`full` or `minimal` from the `repo_map.tier` check message; None when it names no tier."""
    found = re.search(r"tier=(\w+)", message)
    return found.group(1) if found else None


def fact_table(rows: Sequence[tuple[str, Text | str]], language: str) -> Table:
    """Facts of a context card: the label on the left (muted), the value on the right."""
    table = Table.grid(expand=True, padding=(0, 1))
    # One label width for every card, so the values of neighbouring cards line up.
    table.add_column(min_width=12, no_wrap=True, style=brand.PALETTE["muted"])
    table.add_column(ratio=1, overflow="fold")
    for label, value in rows:
        table.add_row(i18n.fact_label(language, label), value)
    return table


def menu_label(
    number: int, name: str, badge: Badge | None, language: str = "ru"
) -> Table:
    """A menu entry: its number and name on the left, its badge on the right. The number takes
    the entry's own colour, so it turns into the selection colour with the entry."""
    row = Table.grid(expand=True)
    row.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    row.add_column(justify="right", no_wrap=True)
    row.add_row(
        Text(f"{number} {i18n.section_label(language, name)}"),
        Text(f" {badge[0]}", style=badge[1]) if badge else Text(""),
    )
    return row


def _section_named(label: str) -> str | None:
    """The menu section whose name, in either language, is `label`."""
    for name in SECTIONS:
        if label in (i18n.section_label(language, name) for language in i18n.LANGUAGES):
            return name
    return None


async def _pop_to(app: App[None], target: Screen[None]) -> None:
    """Close the screens above `target`."""
    while app.screen is not target and target in app.screen_stack:
        await app.pop_screen()


# What a shell screen hands back when it is dismissed; most return nothing.
ShellResult = TypeVar("ShellResult", default=None)


class ConsoleShell(Translated, Screen[ShellResult]):
    """The console's common frame: top bar, numbered main menu with the active section marked,
    breadcrumbs, a context strip, the screen's content, an optional context panel and the footer.

    A screen sets `SECTION` (its menu entry, if any), `CRUMBS` and `HAS_CONTEXT`, and fills
    `compose_strip`, `compose_content` and `compose_context`. Terminals narrower than 100 columns
    collapse the menu behind a toggle, fold the context panel under the content and put the fields
    in one column; 140 columns and wider give the context panel more room. Moving through the menu
    or selecting an entry never runs a command: a section only opens its own screen."""

    DEFAULT_CLASSES = "shell"
    SCOPED_CSS = False
    CSS = SHELL_CSS
    HORIZONTAL_BREAKPOINTS = [
        (0, "-narrow"),
        (brand.NARROW_WIDTH, "-wide"),
        (140, "-spacious"),
    ]
    # Shorter than this, the main menu's entries take one line each so all ten fit unscrolled.
    VERTICAL_BREAKPOINTS = [(0, "-short"), (38, "-tall")]
    BINDINGS = [
        Binding("escape", "back", "Назад"),
        *(
            Binding(str(number), f"section({number})", show=False)
            for number in range(len(SECTIONS))
        ),
    ]
    IDS = ShellIds()
    SECTION: str | None = None
    CRUMBS = "Главная"
    HAS_CONTEXT = False
    # False for the bottom of the screen stack (home): there is nothing to go back to, so Esc is
    # neither shown in the footer nor run.
    CAN_GO_BACK = True
    # Narrower than this, the context panel folds into a collapsible block under the content
    # (`-fold-context`), titled `CONTEXT_TITLE`; 0 leaves the choice to the narrow layout alone.
    CONTEXT_SIDE_MIN = 0
    CONTEXT_TITLE = ""
    # What the folded context panel holds, shown beside its title.
    CONTEXT_SUMMARY = ""
    # The keys a narrow terminal's footer shows (the screen mockup's narrow footer); None shows them
    # all. Hidden keys still work.
    NARROW_FOOTER_KEYS: tuple[str, ...] | None = None
    # False for a view inside a section (it shares the section's menu entry but is not its screen).
    IS_SECTION_ROOT = True

    # Hooks a screen overrides.
    def compose_strip(self) -> ComposeResult:
        yield from ()

    def compose_content(self) -> ComposeResult:
        yield from ()

    def compose_context(self) -> ComposeResult:
        yield from ()

    def menu_label(self, number: int, name: str) -> Text | Table:
        badges: dict[str, Badge] = getattr(self.app, "section_badges", {})
        return menu_label(
            number, name, badges.get(name), getattr(self.app, "language", "ru")
        )

    def menu_item_id(self, name: str) -> str | None:
        return None

    # The frame.
    def crumbs(self) -> list[Crumb]:
        """The path as segments; a label that names a menu section links to it."""
        labels = split_path(self.CRUMBS)
        return [
            Crumb(
                label,
                section=_section_named(label) if index < len(labels) - 1 else None,
            )
            for index, label in enumerate(labels)
        ]

    def _menu(self) -> ListView:
        return ListView(
            *(
                ListItem(
                    Static(self.menu_label(number, name), classes="no-i18n"),
                    name=name,
                    id=self.menu_item_id(name),
                    classes="-active" if name == self.SECTION else "",
                )
                for number, name in enumerate(SECTIONS)
            ),
            id=self.IDS.menu,
        )

    def compose(self) -> ComposeResult:
        ids = self.IDS
        yield WorkspaceHeader(self.CRUMBS)
        with Horizontal(classes="shell-frame"):
            yield ShellSidebar(self._menu(), id=ids.navigation)
            with Vertical(id=ids.content, classes="shell-main"):
                # The menu toggle opens the crumb row on a narrow terminal; `Breadcrumbs` remounts
                # its own children, so the toggle sits beside it rather than inside.
                with Horizontal(classes="crumb-row"):
                    yield Button(
                        "≡", id=ids.toggle, classes="menu-toggle", compact=True
                    )
                    yield Breadcrumbs(self.crumbs(), id=ids.breadcrumbs)
                with ContextStrip():
                    yield from self.compose_strip()
                with Horizontal(id=ids.body, classes="shell-body"):
                    with Vertical(classes="shell-content"):
                        yield from self.compose_content()
                    if self.HAS_CONTEXT:
                        with ContextPanel(id=ids.context):
                            if self.CONTEXT_TITLE:
                                with Fold(
                                    title=self.CONTEXT_TITLE,
                                    summary=self.CONTEXT_SUMMARY,
                                    collapsed=self.app.size.width
                                    < self.CONTEXT_SIDE_MIN,
                                    classes="context-fold",
                                ):
                                    yield from self.compose_context()
                            else:
                                yield from self.compose_context()
        yield Footer()

    def _fold_context(self, width: int) -> None:
        """Fold the context panel under the content below `CONTEXT_SIDE_MIN` columns, unfolded beside it."""
        fold = width < self.CONTEXT_SIDE_MIN
        if fold == self.has_class("-fold-context"):
            return
        self.set_class(fold, "-fold-context")
        for block in self.query(Collapsible).filter(".context-fold"):
            block.collapsed = fold

    def on_resize(self, event: events.Resize) -> None:
        self._fold_context(event.size.width)
        for link in self.query(JumpLink):
            link.relabel()
        narrow = event.size.width < brand.NARROW_WIDTH
        if self.NARROW_FOOTER_KEYS is not None and narrow != self._narrow_footer:
            self._narrow_footer = narrow
            self.refresh_bindings()

    _narrow_footer = False

    @property
    def active_bindings(self) -> dict[str, ActiveBinding]:
        """The footer's keys: on a narrow terminal only `NARROW_FOOTER_KEYS` and the palette are
        shown. The others are left out of the footer only; the keys themselves still work."""
        bindings = super().active_bindings
        keys = self.NARROW_FOOTER_KEYS
        if keys is None or self.app.size.width >= brand.NARROW_WIDTH:
            return bindings
        palette = self.app.COMMAND_PALETTE_BINDING
        return {
            key: active
            for key, active in bindings.items()
            if not active.binding.show or key in keys or key == palette
        }

    def on_mount(self) -> None:
        self._fold_context(self.size.width)
        # Textual sets the breakpoint class on the first resize event, which can come after the
        # first content is laid out; set it now so a log written at once gets its real width.
        for threshold, name in sorted(self.HORIZONTAL_BREAKPOINTS or [], reverse=True):
            if self.size.width >= threshold:
                self.add_class(name)
                break
        if self.SECTION in SECTIONS:
            self.query_one(f"#{self.IDS.menu}", ListView).index = SECTIONS.index(
                self.SECTION
            )

    def sync_language(self) -> None:
        for number, item in enumerate(self.query(f"#{self.IDS.menu} > ListItem")):
            item.query_one(Static).update(self.menu_label(number, item.name or ""))
        for grid in self.query(FieldGrid):
            grid.relabel()
        for card in self.query(SectionCard):
            card.relabel()
        for fold in self.query(Fold):
            fold.relabel()
        for link in self.query(JumpLink):
            link.relabel()
        self.query_one(Breadcrumbs).relabel()

    # Navigation: opening a section is the only effect.
    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "back" and not self.CAN_GO_BACK:
            return False
        return super().check_action(action, parameters)

    async def action_back(self) -> None:
        """Return to the previous screen."""
        await self.app.pop_screen()

    async def open_section(self, index: int) -> None:
        """Open section `index` through the home screen's opener; Home returns to it."""
        from .screens.dashboard import DashboardScreen

        if SECTIONS[index] == HOME_SECTION:
            if any(isinstance(item, DashboardScreen) for item in self.app.screen_stack):
                # From the app's loop: popping this screen from inside its own handler would wait on itself.
                home = next(
                    item
                    for item in self.app.screen_stack
                    if isinstance(item, DashboardScreen)
                )
                self.app.call_later(_pop_to, self.app, home)
            return
        for screen in self.app.screen_stack:
            if isinstance(screen, DashboardScreen):
                await screen.action_open_section(index)
                return

    async def action_section(self, index: int) -> None:
        if SECTIONS[index] != self.SECTION:
            await self.open_section(index)

    async def navigate_crumb(self, crumb: Crumb) -> None:
        """Go to the screen a breadcrumb names; no command runs."""
        if crumb.section is not None:
            if crumb.section != HOME_SECTION:
                for screen in reversed(self.app.screen_stack[:-1]):
                    if (
                        isinstance(screen, ConsoleShell)
                        and screen.IS_SECTION_ROOT
                        and screen.SECTION == crumb.section
                    ):
                        self.app.call_later(_pop_to, self.app, screen)
                        return
            await self.open_section(SECTIONS.index(crumb.section))
        elif crumb.action is not None:
            result = getattr(self, f"action_{crumb.action}")()
            if result is not None:
                await result

    async def on_crumb_link_selected(self, event: CrumbLink.Selected) -> None:
        event.stop()
        await self.navigate_crumb(event.crumb)

    async def on_jump_link_selected(self, event: JumpLink.Selected) -> None:
        """A jump opens its section, as the section's number key does; no command runs."""
        event.stop()
        await self.action_section(SECTIONS.index(event.section))

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == self.IDS.menu and event.item.name in SECTIONS:
            event.stop()
            await self.action_section(SECTIONS.index(event.item.name))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == self.IDS.toggle:
            event.stop()
            self.toggle_class("-menu-open")
            if self.has_class("-menu-open"):
                self.call_after_refresh(
                    self.query_one(f"#{self.IDS.menu}", ListView).focus
                )
