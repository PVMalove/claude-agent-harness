"""Shared visual shell of the console's workspace screens; navigation has no execution side effects."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import ListView, Static

from . import brand


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


# Every rule of the shell, once. Screens load it through their `CSS` (a screen's own rules are
# appended to it), so it outranks the app's generic widget rules in any host app. Rules for a
# single screen's widgets belong to that screen, never here.
SHELL_CSS = brand.shell_css("""
Screen.shell { background: $background; }
.shell .breadcrumbs { height: 2; padding: 0 2; content-align: left middle; border-bottom: solid $border-subtle; color: $text; }
.shell .context-strip { height: 2; border-bottom: solid $border-subtle; padding: 0 2; color: $text-muted; }
.shell .context-strip Static { width: 1fr; height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
.shell Button { min-width: 8; width: auto; color: $text; border: round $control-border; text-style: none; }
.shell Button:hover, .shell Button:focus { border: round $primary; color: $primary; }
.shell .context-strip Button { border: none; height: 1; min-width: 3; color: $primary; }
.shell Button.-textual-compact, .shell Button.-textual-compact:hover, .shell Button.-textual-compact:focus { border: none; padding: 0 1; height: 1; min-height: 1; min-width: 0; }
.shell .context-panel { width: 30; height: 1fr; padding: 0 2; overflow-x: hidden; border-left: solid $border-subtle; background: $surface; }
.shell .context-panel Static { height: auto; }
.shell .context-label { height: 2; color: $text-muted; margin-top: 1; }
.shell .context-value { height: auto; margin-bottom: 1; }
.shell .context-note { height: auto; border: round $control-border; padding: 0 1; margin-top: 1; color: $text-muted; }
.shell ShellSidebar { width: 24; height: 1fr; background: $surface; border-right: solid $border-subtle; padding: 1; }
.shell ShellSidebar .menu-heading { height: 2; padding-left: 1; color: $text-muted; }
.shell ShellSidebar .menu-context { height: auto; padding: 1; color: $text-muted; }
.shell ShellSidebar ListView, .shell ShellSidebar ListView:focus { height: 1fr; border: none; padding: 0; margin: 0; background: $surface; }
.shell ShellSidebar ListView > ListItem { height: 2; padding: 0 1; color: $text-muted; background: $surface; text-style: none; }
.shell ShellSidebar ListView > ListItem.-highlight, .shell ShellSidebar ListView:focus > ListItem.-highlight {
    background: $selection-background; color: $selection-foreground; text-style: none;
}
.shell Footer, .shell WorkspaceHeader { background: $surface; }
.shell ListView, .shell ListView:focus { background-tint: transparent; }
.shell.-narrow ShellSidebar { padding: 0 1; }
.shell.-narrow ShellSidebar .menu-heading { height: 1; }
.shell.-narrow ShellSidebar .menu-context { display: none; }
.shell.-narrow WorkspaceHeader { height: 2; padding-top: 0; }
.shell.-narrow WorkspaceHeader #topbar-right { display: none; }
.shell.-narrow .breadcrumbs, .shell.-narrow .context-strip { height: 1; border: none; }
.shell * { scrollbar-color: $scrollbar-thumb; scrollbar-color-hover: $primary; scrollbar-background: $surface; }
""")
