"""Shared visual shell of the console's workspace screens; navigation has no execution side effects."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Button, Collapsible, Footer, ListItem, ListView, Static
from typing_extensions import TypeVar

from . import brand, i18n
from .chrome import WorkspaceHeader
from .crumbs import ELLIPSIS, Crumb, collapse_crumbs, split_path
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


class ShellSidebar(Vertical):
    """The main menu column. Its look lives in `SHELL_CSS`, with the rest of the shell."""

    def __init__(self, menu: ListView, *, id: str, context: str = "") -> None:
        super().__init__(id=id)
        self.menu = menu
        self.context = context

    def compose(self) -> ComposeResult:
        yield Static("Главное меню", classes="menu-heading")
        yield self.menu
        if self.context:
            yield Static(self.context, classes="menu-context", markup=False)


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


class ContextPanel(VerticalScroll):
    """Facts beside the content; folded under it on a narrow terminal, never hidden."""

    DEFAULT_CLASSES = "context-panel"


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
.shell .breadcrumbs { height: auto; min-height: 2; padding: 0 2; border-bottom: solid $border-subtle; color: $foreground; }
.shell .breadcrumbs Static { width: auto; height: 1; }
.shell .crumb-link { color: $primary; text-style: underline; }
.shell .crumb-link:hover { background: $row-highlight; color: $selection-foreground; }
.shell .crumb-link:focus { background: $selection-background; color: $selection-foreground; text-style: bold underline; }
.shell .crumb-sep { color: $text-muted; }
.shell .crumb-current { width: 1fr; min-width: 1; text-style: bold; text-wrap: nowrap; text-overflow: ellipsis; }
.shell .context-strip { height: auto; min-height: 2; border-bottom: solid $border-subtle; padding: 0 2; color: $text-muted; }
.shell .context-strip Static { width: 1fr; height: auto; }
.shell Button { min-width: 8; width: auto; color: $foreground; border: round $component-border; text-style: none; }
.shell Button:hover, .shell Button:focus { border: round $primary; color: $primary; }
.shell .context-strip Button { border: none; height: 1; min-width: 3; color: $primary; }
.shell Button.-textual-compact, .shell Button.-textual-compact:hover, .shell Button.-textual-compact:focus { border: none; padding: 0 1; height: 1; min-height: 1; min-width: 0; }
.shell .context-panel { width: 30; height: 1fr; padding: 0 2; overflow-x: hidden; scrollbar-gutter: stable; border-left: solid $border-subtle; background: $surface; }
.shell .context-panel Static { height: auto; }
.shell .context-label { height: 2; color: $text-muted; margin-top: 1; }
.shell .context-value { height: auto; margin-bottom: 1; }
.shell .context-note { height: auto; border: round $component-border; padding: 0 1; margin-top: 1; color: $text-muted; }
.shell ShellSidebar { width: 24; height: 1fr; background: $surface; border-right: solid $border-subtle; padding: 1; }
.shell ShellSidebar .menu-heading { height: 2; padding-left: 1; color: $text-muted; }
.shell ShellSidebar .menu-context { height: auto; padding: 1; color: $text-muted; }
.shell ShellSidebar ListView, .shell ShellSidebar ListView:focus { height: 1fr; border: none; padding: 0; margin: 0; background: $surface; }
.shell ShellSidebar ListView > ListItem { height: 2; padding: 0 1; color: $text-muted; background: $surface; text-style: none; border-left: solid transparent; }
.shell ShellSidebar ListView > ListItem.-highlight, .shell ShellSidebar ListView:focus > ListItem.-highlight {
    background: $selection-background; color: $selection-foreground; text-style: none; border-left: solid $selection-edge;
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
.shell.-narrow .menu-toggle { display: block; }
.shell.-narrow ShellSidebar { display: none; }
.shell.-narrow.-menu-open ShellSidebar { display: block; }
.shell.-narrow .shell-body, .shell.-fold-context .shell-body { layout: vertical; overflow-y: auto; }
.shell.-narrow .shell-content, .shell.-fold-context .shell-content { min-height: 100%; }
.shell.-narrow ShellSidebar { width: 21; }
.shell.-narrow .context-panel, .shell.-fold-context .context-panel { width: 100%; height: auto; border-left: none; border-top: solid $border-subtle; }
.shell .context-fold { border: none; padding: 0; background: transparent; }
.shell .context-fold > Contents { padding: 0; height: auto; }
.shell .context-fold > CollapsibleTitle { display: none; padding: 0; color: $foreground; }
.shell.-fold-context .context-fold > CollapsibleTitle { display: block; }
.shell.-narrow .field-grid { layout: vertical; }
.shell.-narrow .field-column { width: 100%; padding-right: 0; }
.shell.-narrow ShellSidebar { padding: 0 1; }
.shell.-narrow ShellSidebar .menu-heading { height: 1; }
.shell.-narrow ShellSidebar .menu-context { display: none; }
.shell.-narrow WorkspaceHeader { height: 2; padding: 0 2; }
.shell.-narrow .breadcrumbs, .shell.-narrow .context-strip { min-height: 1; border: none; }
.shell * { scrollbar-color: $scrollbar-thumb; scrollbar-color-hover: $primary; scrollbar-color-active: $primary; scrollbar-background: $scrollbar-track; scrollbar-background-hover: $scrollbar-track; scrollbar-background-active: $scrollbar-track; }
.shell ListView, .shell .frame, .shell Input, .shell TextArea, .shell Select > SelectCurrent { border: round $component-border; }
.shell ListView:focus, .shell .frame:focus, .shell .frame:focus-within, .shell Input:focus, .shell TextArea:focus, .shell Select:focus > SelectCurrent { border: round $primary; }
""")


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
    # False for a view inside a section (it shares the section's menu entry but is not its screen).
    IS_SECTION_ROOT = True

    # Hooks a screen overrides.
    def compose_strip(self) -> ComposeResult:
        yield from ()

    def compose_content(self) -> ComposeResult:
        yield from ()

    def compose_context(self) -> ComposeResult:
        yield from ()

    def sidebar_context(self) -> str:
        return ""

    def menu_label(self, number: int, name: str) -> Text | Table:
        language = getattr(self.app, "language", "ru")
        return Text.assemble(
            (f"{number} ", brand.PALETTE["muted"]), i18n.section_label(language, name)
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
            yield ShellSidebar(
                self._menu(), id=ids.navigation, context=self.sidebar_context()
            )
            with Vertical(id=ids.content, classes="shell-main"):
                yield Breadcrumbs(self.crumbs(), id=ids.breadcrumbs)
                with ContextStrip():
                    yield Button(
                        "☰", id=ids.toggle, classes="menu-toggle", compact=True
                    )
                    yield from self.compose_strip()
                with Horizontal(id=ids.body, classes="shell-body"):
                    with Vertical(classes="shell-content"):
                        yield from self.compose_content()
                    if self.HAS_CONTEXT:
                        with ContextPanel(id=ids.context):
                            if self.CONTEXT_TITLE:
                                with Collapsible(
                                    title=self.CONTEXT_TITLE,
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
