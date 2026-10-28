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
        left = f"[bold]☰ {brand.MARK} {brand.PRODUCT} {escape(version)}[/][{muted}] · {escape(crumbs)}[/]"
        counts: HealthCounts | None = getattr(self.app, "health_counts", None)
        counters = health_counts_markup(counts) if counts is not None else ""
        right = ""
        branch = self._branch
        if branch:
            # The whole branch name whenever it fits; the breadcrumb already ellipsizes first.
            left_cells = len(f"☰ {brand.MARK} {brand.PRODUCT} {version} · {crumbs}")
            counter_cells = len(" ".join(f"x{v}" for v in counts)) if counts else 0
            room = self.size.width - 2 - left_cells - counter_cells - 8
            limit = max(len(branch) if room >= len(branch) else room, 14)
            right += f"[{muted}]⎇ {escape(brand.shorten(branch, limit))}[/]  "
        right += counters
        self.query_one("#topbar-left", Static).update(left)
        self.query_one("#topbar-right", Static).update(right.rstrip())


def where_text(project: str, branch: str | None, width: int) -> str:
    """The right side of the top bar: `project · ⎇ branch` on a wide terminal and the branch alone,
    shortened to fit, on a narrow one; only the project when there is no branch."""
    if not branch:
        return project
    if width < brand.NARROW_WIDTH:
        return f"⎇ {brand.shorten(branch, max(12, (width - 4) // 2 - 4))}"
    limit = max(16, width // 2 - len(project) - 10)
    return f"{project} · ⎇ {brand.shorten(branch, limit)}"


class WorkspaceHeader(TopBar):
    """Product identity stays in the window bar; breadcrumbs belong to the content below it."""

    DEFAULT_CSS = brand.shell_css("""
    WorkspaceHeader { height: 2; margin: 0; padding: 0 2; background: transparent; color: $foreground; border-bottom: solid $border-subtle; }
    WorkspaceHeader #topbar-left { width: 1fr; }
    WorkspaceHeader #topbar-right { width: 1fr; color: $text-muted; }
    """)

    def refresh_status(self) -> None:
        if not self.is_mounted or not self.query("#topbar-left"):
            return
        primary, muted = brand.PALETTE["primary"], brand.PALETTE["muted"]
        self.query_one("#topbar-left", Static).update(
            f"[{primary}]{brand.MARK}[/]  {brand.PRODUCT}  [{muted}]{console_data.harness_version()}[/]"
        )
        repo = getattr(self.app, "repo", None)
        project = repo.name if repo else ""
        width = self.size.width or self.app.size.width
        self.query_one("#topbar-right", Static).update(
            escape(where_text(project, self._branch, width))
        )
