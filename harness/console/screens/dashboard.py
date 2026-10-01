"""The console's home screen: the dashboard summary (offline by default, with an "online checks"
action) plus the section menu - Diagnostics, Harness, Orchestration, Reports, Repo Map, Help."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

from rich.markup import escape
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, ListItem, ListView, Static

from .. import brand
from .. import data as console_data
from ..data import DashboardData
from ..runner import CommandRunner, default_runner

SECTIONS = ("Diagnostics", "Harness", "Orchestration", "Reports", "Repo Map", "Help")


class _CollectDashboard(Protocol):
    def __call__(self, repo: Path, /, *, online: bool = False) -> DashboardData: ...


def _counter(name: str, value: int, color: str) -> str:
    """A health counter, coloured only when it is non-zero so a clean report stays calm."""
    text = f"{name}={value}"
    return f"[{color}]{text}[/]" if value else f"[{brand.PALETTE['muted']}]{text}[/]"


def _render_summary(data: DashboardData, *, online: bool = False) -> str:
    active_batches = (
        str(data.active_batches) if data.active_batches is not None else "не подключено"
    )
    mode = "online" if online else "offline"
    # The Repo Map message joins its facts with "; ": one fact per line keeps it readable.
    first, *rest = escape(data.repo_map_tier).split("; ")
    repo_map = "\n".join([f"repo map: {first}", *(f"  {part}" for part in rest)])
    lines = [
        f"health ({mode}): "
        f"{_counter('ok', data.ok, brand.PALETTE['success'])} "
        f"{_counter('warn', data.warn, brand.PALETTE['warning'])} "
        f"{_counter('fail', data.fail, brand.PALETTE['error'])} "
        f"{_counter('skipped', data.skipped, brand.PALETTE['muted'])}",
        f"active batches: {active_batches}",
        repo_map,
        f"harness: {data.harness_version} (drift: {escape(data.drift_state)})",
    ]
    if data.problems:
        lines.append("")
        lines.append("ошибки и предупреждения:")
        colors = {"fail": brand.PALETTE["error"], "warn": brand.PALETTE["warning"]}
        lines.extend(
            f"  [{colors.get(status, brand.PALETTE['muted'])}]{status:<4}[/] "
            f"{escape(check_id)} — {escape(message)}"
            for status, check_id, message in data.problems
        )
    return "\n".join(lines)


def _render_mark() -> Text:
    return Text("\n".join(brand.LOGO), style=f"bold {brand.PALETTE['primary']}")


def _render_banner(info: brand.BannerInfo) -> Text:
    """The description beside the mark: product and version, capabilities, path and branch."""
    blank, title, capabilities, repo, branch = brand.banner_lines(info)
    banner = Text(blank + "\n")
    banner.append(title + "\n", style=f"bold {brand.PALETTE['primary']}")
    banner.append(capabilities + "\n", style=brand.PALETTE["secondary"])
    banner.append(repo + "\n", style=brand.PALETTE["foreground"])
    banner.append(branch, style=brand.PALETTE["muted"])
    return banner


def _default_banner(repo: Path) -> brand.BannerInfo:
    return brand.collect_banner(repo, console_data.harness_version())


class DashboardScreen(Screen[None]):
    """Home screen: the mark and banner, the dashboard summary and a `ListView` section menu."""

    # The section menu, not the "online checks" button, owns the keyboard on arrival.
    AUTO_FOCUS = "#section-menu"
    DEFAULT_CSS = """
    DashboardScreen #brand { height: auto; padding: 1 2 0 2; }
    DashboardScreen #brand-mark { width: auto; }
    DashboardScreen #brand-info { width: 1fr; padding-left: 2; }
    DashboardScreen #dashboard-summary-scroll {
        height: auto; max-height: 40%; margin: 1 2 0 2; border: round $primary 50%;
    }
    DashboardScreen #dashboard-actions { height: auto; margin: 1 2 0 2; }
    DashboardScreen #dashboard-actions Button { width: 21; margin-right: 1; }
    DashboardScreen #dashboard-cli { height: auto; width: 1fr; padding: 0 0 0 1; }
    DashboardScreen .action-cli { color: $text-muted; height: auto; }
    DashboardScreen #section-menu { height: auto; margin: 1 2; border-title-color: $primary; }
    """

    def __init__(
        self,
        repo: Path,
        *,
        collect_dashboard: _CollectDashboard = console_data.collect_dashboard,
        collect_banner: Callable[[Path], brand.BannerInfo] = _default_banner,
        command_runner: CommandRunner = default_runner,
    ) -> None:
        super().__init__()
        self.repo = repo
        self._collect_dashboard = collect_dashboard
        self._collect_banner = collect_banner
        self._command_runner = command_runner
        self._sections: dict[str, Callable[[], Screen[None]]] = {
            "Diagnostics": self._diagnostics,
            "Harness": self._harness,
            "Orchestration": self._orchestration,
            "Reports": self._reports,
            "Repo Map": self._repo_map,
            "Help": self._help,
        }

    def compose(self) -> ComposeResult:
        yield Header(icon=brand.MENU_ICON)
        with Horizontal(id="brand"):
            yield Static(_render_mark(), id="brand-mark")
            yield Static(
                _render_banner(self._collect_banner(self.repo)), id="brand-info"
            )
        with VerticalScroll(id="dashboard-summary-scroll", classes="frame") as scroll:
            scroll.border_title = "Состояние"
            yield Static(
                _render_summary(self._collect_dashboard(self.repo)),
                id="dashboard-summary",
            )
        with Horizontal(id="dashboard-actions", classes="frame") as actions:
            actions.border_title = "Проверки"
            yield Button("Offline checks", id="dashboard-offline")
            yield Button("Online checks", id="dashboard-online")
            with Vertical(id="dashboard-cli"):
                yield Static(
                    f"$ {console_data.health_cli_line(self.repo)}",
                    id="dashboard-offline-cli",
                    classes="action-cli",
                    markup=False,
                )
                yield Static(
                    f"$ {console_data.health_cli_line(self.repo, '--online')}",
                    id="dashboard-online-cli",
                    classes="action-cli",
                    markup=False,
                )
        menu = ListView(
            *(ListItem(Static(name), name=name) for name in SECTIONS),
            id="section-menu",
        )
        menu.border_title = "Разделы"
        yield menu
        yield Footer()

    def on_unmount(self) -> None:
        """Отменяет фоновые воркеры при размонтировании экрана."""
        self.workers.cancel_all()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id not in ("dashboard-offline", "dashboard-online"):
            return
        online = event.button.id == "dashboard-online"
        summary = self.query_one("#dashboard-summary", Static)
        summary.update(
            "онлайн-проверки выполняются…" if online else "офлайн-проверки выполняются…"
        )

        def work() -> None:
            try:
                text = _render_summary(
                    self._collect_dashboard(self.repo, online=online), online=online
                )
            except Exception:
                return
            if self.is_mounted and self.app.is_running:
                try:
                    self.app.call_from_thread(summary.update, text)
                except Exception:
                    pass

        self.run_worker(work, thread=True, exclusive=True, group="dashboard-health")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        open_screen = self._sections.get(event.item.name or "")
        if open_screen is not None:
            self.app.push_screen(open_screen())

    def _diagnostics(self) -> Screen[None]:
        from .diagnostics import DiagnosticsScreen

        return DiagnosticsScreen(self.repo)

    def _harness(self) -> Screen[None]:
        from .harness import HarnessScreen

        return HarnessScreen(self.repo, command_runner=self._command_runner)

    def _orchestration(self) -> Screen[None]:
        from .orchestration import OrchestrationScreen

        return OrchestrationScreen(self.repo, command_runner=self._command_runner)

    def _reports(self) -> Screen[None]:
        from .reports import ReportsScreen

        return ReportsScreen(self.repo)

    def _repo_map(self) -> Screen[None]:
        from .repo_map import RepoMapScreen

        return RepoMapScreen(self.repo, command_runner=self._command_runner)

    def _help(self) -> Screen[None]:
        from .help import HelpScreen

        return HelpScreen()
