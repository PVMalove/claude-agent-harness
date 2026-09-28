"""The console's home screen: the dashboard summary (offline by default, with an "online checks"
action) plus the section menu - Diagnostics, Harness, Orchestration, Reports, Repo Map."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, ListItem, ListView, Static

from .. import data as console_data
from ..data import DashboardData
from ..runner import CommandRunner, default_runner

SECTIONS = ("Diagnostics", "Harness", "Orchestration", "Reports", "Repo Map")


class _CollectDashboard(Protocol):
    def __call__(self, repo: Path, /, *, online: bool = False) -> DashboardData: ...


def _render_summary(data: DashboardData, *, online: bool = False) -> str:
    active_batches = (
        str(data.active_batches) if data.active_batches is not None else "не подключено"
    )
    mode = "online" if online else "offline"
    return (
        f"health ({mode}): ok={data.ok} warn={data.warn} fail={data.fail} "
        f"skipped={data.skipped}\n"
        f"active batches: {active_batches}\n"
        f"repo map: {data.repo_map_tier}\n"
        f"harness: {data.harness_version} (drift: {data.drift_state})"
    )


class DashboardScreen(Screen[None]):
    """Home screen: dashboard summary above a `ListView` menu of the five sections."""

    # The section menu, not the "online checks" button, owns the keyboard on arrival.
    AUTO_FOCUS = "#section-menu"

    def __init__(
        self,
        repo: Path,
        *,
        collect_dashboard: _CollectDashboard = console_data.collect_dashboard,
        command_runner: CommandRunner = default_runner,
    ) -> None:
        super().__init__()
        self.repo = repo
        self._collect_dashboard = collect_dashboard
        self._command_runner = command_runner
        self._sections: dict[str, Callable[[], Screen[None]]] = {
            "Diagnostics": self._diagnostics,
            "Harness": self._harness,
            "Orchestration": self._orchestration,
            "Reports": self._reports,
            "Repo Map": self._repo_map,
        }

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(_render_summary(self._collect_dashboard(self.repo)), id="dashboard-summary")
        yield Button("Online checks", id="dashboard-online")
        yield Static(
            f"$ {console_data.health_cli_line(self.repo, '--online')}",
            id="dashboard-online-cli",
            markup=False,
        )
        yield ListView(
            *(ListItem(Static(name), name=name) for name in SECTIONS),
            id="section-menu",
        )
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "dashboard-online":
            return
        summary = self.query_one("#dashboard-summary", Static)
        summary.update("онлайн-проверки выполняются…")

        def work() -> None:
            text = _render_summary(self._collect_dashboard(self.repo, online=True), online=True)
            self.app.call_from_thread(summary.update, text)

        self.run_worker(work, thread=True, exclusive=True, group="dashboard-online")

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
