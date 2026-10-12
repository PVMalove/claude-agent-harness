"""Help screen: the console guide from harness.console.help_text, rendered as Markdown."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Markdown

from ..chrome import TopBar
from ..translate import Translated
from ..help_text import help_markdown
from .. import brand


class HelpScreen(Translated, Screen[None]):
    BINDINGS = [Binding("escape", "app.pop_screen", "Назад")]
    DEFAULT_CSS = """
    HelpScreen #help-scroll { height: 1fr; }
    """

    def compose(self) -> ComposeResult:
        yield TopBar("Главная › Справка")
        with VerticalScroll(id="help-scroll", classes="frame") as scroll:
            scroll.border_title = brand.frame_title("Справка")
            yield Markdown(
                help_markdown(getattr(self.app, "language", "ru")), id="help-text"
            )
        yield Footer()

    def sync_language(self) -> None:
        self.query_one("#help-text", Markdown).update(
            help_markdown(getattr(self.app, "language", "ru"))
        )
