"""The console's home screen: an offline dashboard summary (added in the next commit) plus the
section menu - Diagnostics, Harness, Orchestration, Reports, Repo Map. Only Diagnostics gets a
real screen (also the next commit); the rest stay stubs."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Footer, Header, ListItem, ListView, Static

from .stub import StubScreen

SECTIONS = ("Diagnostics", "Harness", "Orchestration", "Reports", "Repo Map")


class DashboardScreen(Screen[None]):
    """Home screen: dashboard summary above a `ListView` menu of the five sections."""

    def __init__(self, repo: Path) -> None:
        super().__init__()
        self.repo = repo

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="dashboard-summary")
        yield ListView(
            *(ListItem(Static(name), name=name) for name in SECTIONS),
            id="section-menu",
        )
        yield Footer()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        name = event.item.name
        if name is not None:
            self.app.push_screen(StubScreen(name))
