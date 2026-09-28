"""Help screen: the console guide from harness.console.help_text, rendered as Markdown."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, Markdown

from ..help_text import HELP_MARKDOWN
from .. import brand


class HelpScreen(Screen[None]):
    BINDINGS = [Binding("escape", "app.pop_screen", "Назад")]
    DEFAULT_CSS = """
    HelpScreen #help-scroll { height: 1fr; }
    """

    def compose(self) -> ComposeResult:
        yield Header(icon=brand.MENU_ICON)
        with VerticalScroll(id="help-scroll", classes="frame") as scroll:
            scroll.border_title = "Справка"
            yield Markdown(HELP_MARKDOWN, id="help-text")
        yield Footer()
