"""Shared visual shell for the home and worker workspaces; navigation has no execution side effects."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import ListView, Static

from . import brand


def session_state_label(state: str) -> tuple[str, str]:
    labels = {
        "working": ("● Выполняется", "success"),
        "dispatched": ("◌ Запуск", "warning"),
        "approved": ("○ Готов к запуску", "muted"),
        "reported": ("✓ Отчёт получен", "success"),
        "blocked": ("✖ Работа заблокирована", "error"),
        "failed": ("✖ Ошибка", "error"),
        "cancelled": ("■ Отменён", "muted"),
        "abandoned": ("■ Завершён", "muted"),
    }
    label, color = labels.get(state, ("? Нет подтверждения", "muted"))
    return label, brand.PALETTE[color]


class WorkspaceSidebar(Vertical):
    DEFAULT_CSS = """
    WorkspaceSidebar { width: 24; height: 1fr; background: #262523; border-right: solid #38352f; padding: 1; }
    WorkspaceSidebar .menu-heading { height: 2; padding-left: 1; color: $text-muted; }
    WorkspaceSidebar ListView, WorkspaceSidebar ListView:focus { height: 1fr; border: none; padding: 0; margin: 0; background: #262523; }
    WorkspaceSidebar ListView > ListItem { height: 2; padding: 0 1; color: $text-muted; background: #262523; }
    WorkspaceSidebar ListView > ListItem.-highlight, WorkspaceSidebar ListView:focus > ListItem.-highlight {
        background: #45332c; color: #f6baa3; text-style: none;
    }
    WorkspaceSidebar .menu-context { height: auto; padding: 1; color: $text-muted; }
    """

    def __init__(
        self,
        menu: ListView,
        *,
        id: str,
        context: str = "Контекст исполнителя\nWorker: выбранная сессия\nQA: отдельный dispatch\nEnv: имена, без значений",
    ) -> None:
        super().__init__(id=id)
        self.menu = menu
        self.context = context

    def compose(self) -> ComposeResult:
        yield Static("Главное меню", classes="menu-heading")
        yield self.menu
        yield Static(self.context, classes="menu-context", markup=False)


WORKSPACE_CSS = """
Screen.workspace { background: #1F1E1C; }
.workspace .breadcrumbs { height: 2; padding: 0 2; content-align: left middle; border-bottom: solid #38352f; color: $text; }
.workspace .context-strip { height: 2; border-bottom: solid #38352f; padding: 0 2; color: $text-muted; }
.workspace .context-strip Static { width: 1fr; height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
.workspace Button { min-width: 8; width: auto; color: $text; border: round #51483d; }
.workspace Button:hover, .workspace Button:focus { border: round $primary; color: $primary; }
.workspace .context-strip Button { border: none; height: 1; min-width: 3; color: $primary; }
.workspace .context-panel { width: 30; height: 1fr; padding: 0 2; overflow-x: hidden; border-left: solid #38352f; background: #262523; }
.workspace .context-panel Static { height: auto; }
.workspace .context-label { height: 2; color: $text-muted; margin-top: 1; }
.workspace .context-value { height: auto; margin-bottom: 1; }
.workspace .context-note { height: auto; border: round #51483d; padding: 0 1; margin-top: 1; color: $text-muted; }
.workspace.-narrow WorkspaceSidebar { padding: 0 1; }
.workspace.-narrow WorkspaceSidebar .menu-heading { height: 1; }
.workspace.-narrow WorkspaceSidebar .menu-context { display: none; }
.workspace.-narrow WorkspaceHeader { height: 2; padding-top: 0; }
.workspace.-narrow WorkspaceHeader #topbar-right { display: none; }
.workspace.-narrow .breadcrumbs, .workspace.-narrow .context-strip { height: 1; border: none; }
.workspace Footer, .workspace WorkspaceHeader, .workspace WorkspaceSidebar, .workspace #worker-sessions, .workspace .context-panel { background: #262523; }
.workspace ListView, .workspace ListView:focus { background-tint: transparent; }
.workspace WorkspaceSidebar ListView, .workspace WorkspaceSidebar ListView:focus,
.workspace #worker-picker, .workspace #worker-picker:focus { border: none; background: #262523; }
.workspace WorkspaceSidebar ListView > ListItem { background: #262523; text-style: none; }
.workspace WorkspaceSidebar ListView > ListItem.-highlight, .workspace WorkspaceSidebar ListView:focus > ListItem.-highlight {
    background: #45332c; color: #f6baa3; text-style: none;
}
.workspace #worker-picker > ListItem.-highlight, .workspace #worker-picker:focus > ListItem.-highlight {
    background: #35302b; color: $text; text-style: none;
}
.workspace Button { text-style: none; }
.workspace #worker-actions Button { border: round #51483d; color: $text; }
.workspace #worker-actions Button:hover, .workspace #worker-actions Button:focus { border: round $primary; color: $primary; }
.workspace Button.-textual-compact, .workspace Button.-textual-compact:hover, .workspace Button.-textual-compact:focus { border: none; padding: 0 1; height: 1; min-height: 1; min-width: 0; }
.workspace #home-sessions, .workspace #home-sessions:focus { border: none; }
.workspace #home-sessions > ListItem.-highlight { background: #35302b; color: $text; text-style: none; }
.workspace #worker-actions #worker-stop { color: $error; }
.workspace * { scrollbar-color: #605746; scrollbar-color-hover: $primary; scrollbar-background: #262523; }

"""
