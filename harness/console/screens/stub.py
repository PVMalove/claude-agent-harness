"""One shared "not yet implemented" screen for every console section that has no real screen of
its own yet (Orchestration - see harness/console/screens/dashboard.py)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Header, Static


class StubScreen(Screen[None]):
    """Placeholder for a section name the console does not implement yet."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Назад")]

    def __init__(self, section_name: str) -> None:
        super().__init__()
        self.section_name = section_name

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(f"{self.section_name}: раздел ещё не реализован", id="stub-message")
        yield Footer()
