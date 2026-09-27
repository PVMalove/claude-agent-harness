"""The console's home screen: an offline dashboard summary plus the section menu - Diagnostics,
Harness, Orchestration, Reports, Repo Map. Only Diagnostics gets a real screen (a later commit);
the rest stay stubs."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Footer, Header, ListItem, ListView, Static

from .. import data as console_data
from ..data import DashboardData
from ..runner import CommandRunner, default_runner
from .stub import StubScreen

SECTIONS = ("Diagnostics", "Harness", "Orchestration", "Reports", "Repo Map")


def _render_summary(data: DashboardData) -> str:
    active_batches = (
        str(data.active_batches) if data.active_batches is not None else "не подключено"
    )
    return (
        f"health: ok={data.ok} warn={data.warn} fail={data.fail} skipped={data.skipped}\n"
        f"active batches: {active_batches}\n"
        f"repo map: {data.repo_map_tier}\n"
        f"harness: {data.harness_version} (drift: {data.drift_state})"
    )


class DashboardScreen(Screen[None]):
    """Home screen: dashboard summary above a `ListView` menu of the five sections."""

    def __init__(
        self,
        repo: Path,
        *,
        collect_dashboard: Callable[[Path], DashboardData] = console_data.collect_dashboard,
        command_runner: CommandRunner = default_runner,
    ) -> None:
        super().__init__()
        self.repo = repo
        self._collect_dashboard = collect_dashboard
        self._command_runner = command_runner

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(_render_summary(self._collect_dashboard(self.repo)), id="dashboard-summary")
        yield ListView(
            *(ListItem(Static(name), name=name) for name in SECTIONS),
            id="section-menu",
        )
        yield Footer()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        name = event.item.name
        if name == "Diagnostics":
            from .diagnostics import DiagnosticsScreen

            self.app.push_screen(
                DiagnosticsScreen(self.repo, command_runner=self._command_runner)
            )
        elif name is not None:
            self.app.push_screen(StubScreen(name))
