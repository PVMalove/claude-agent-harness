"""The console's persistent top bar: product, version, where the user is, the branch and the
health counters - the same one-line context on every screen."""

from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static

from . import brand, i18n
from . import data as console_data

# Health counters shared through the app: (ok, warn, fail, skipped); None until a check has run.
HealthCounts = tuple[int, int, int, int]


def health_counts_markup(counts: HealthCounts, *, gaps: str = " ") -> str:
    """`✔42 ⚠0 ✖2 ○11`: a counter is coloured only when non-zero so a clean report stays calm."""
    parts = []
    for status, value in zip(("ok", "warn", "fail", "skipped"), counts):
        color = brand.STATUS_COLORS[status] if value else brand.PALETTE["muted"]
        parts.append(f"[{color}]{brand.STATUS_ICONS[status]}{value}[/]")
    return gaps.join(parts)


class TopBar(Horizontal):
    """One text row plus a rule. `crumbs` is where the user is, e.g. `Главная › Harness`."""

    DEFAULT_CSS = """
    TopBar {
        height: 2; margin: 0 1; padding: 0 1; color: $primary;
        border-bottom: solid $primary 40%;
    }
    TopBar #topbar-left { width: 1fr; text-wrap: nowrap; text-overflow: ellipsis; }
    TopBar #topbar-right { width: auto; padding-left: 2; text-align: right; }
    """

    def __init__(self, crumbs: str) -> None:
        super().__init__()
        self.crumbs = crumbs
        self._branch: str | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="topbar-left")
        yield Static(id="topbar-right")

    def on_mount(self) -> None:
        repo = getattr(self.app, "repo", None)
        self._branch = brand.current_branch(repo) if repo is not None else None
        self.refresh_status()

    def on_click(self) -> None:
        """A click on the bar opens the command palette (what the header's menu icon used to do)."""
        self.app.action_command_palette()

    def on_resize(self) -> None:
        self.refresh_status()

    def refresh_status(self) -> None:
        if not self.is_mounted or not self.query("#topbar-left"):
            return
        muted = brand.PALETTE["muted"]
        version = console_data.harness_version()
        crumbs = i18n.tr(getattr(self.app, "language", "ru"), self.crumbs)
        left = f"[bold]☰ ◆ Harness {escape(version)}[/][{muted}] · {escape(crumbs)}[/]"
        counts: HealthCounts | None = getattr(self.app, "health_counts", None)
        counters = health_counts_markup(counts) if counts is not None else ""
        right = ""
        branch = self._branch
        if branch:
            # The whole branch name whenever it fits; the breadcrumb already ellipsizes first.
            left_cells = len(f"☰ ◆ Harness {version} · {crumbs}")
            counter_cells = len(" ".join(f"x{v}" for v in counts)) if counts else 0
            room = self.size.width - 2 - left_cells - counter_cells - 8
            limit = max(len(branch) if room >= len(branch) else room, 14)
            right += f"[{muted}]⎇ {escape(brand.shorten(branch, limit))}[/]  "
        right += counters
        self.query_one("#topbar-left", Static).update(left)
        self.query_one("#topbar-right", Static).update(right.rstrip())


class WorkspaceHeader(TopBar):
    """Product identity stays in the window bar; breadcrumbs belong to the content below it."""

    DEFAULT_CSS = """
    WorkspaceHeader { height: 3; margin: 0; padding: 1 2 0 2; background: #262523; color: $text; border-bottom: solid #38352f; }
    WorkspaceHeader #topbar-left { width: 1fr; }
    WorkspaceHeader #topbar-right { width: 1fr; color: $text-muted; }
    """

    def refresh_status(self) -> None:
        if not self.is_mounted or not self.query("#topbar-left"):
            return
        primary, muted = brand.PALETTE["primary"], brand.PALETTE["muted"]
        self.query_one("#topbar-left", Static).update(
            f"[{primary}]▏H▕[/]  Agent Harness console  [{muted}]{console_data.harness_version()}[/]"
        )
        repo = getattr(self.app, "repo", None)
        project = repo.name if repo else ""
        branch = self._branch or ""
        limit = max(16, self.size.width // 2 - len(project) - 8)
        self.query_one("#topbar-right", Static).update(
            escape(f"{project} · {brand.shorten(branch, limit)}")
        )
