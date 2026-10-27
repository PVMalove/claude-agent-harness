"""Help screen: the console guide from harness.console.help_text, rendered as Markdown."""

from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import ListItem, ListView, Markdown, Static

from .. import brand, i18n
from ..help_text import help_markdown
from ..shell import ConsoleShell, Fold


class HelpScreen(ConsoleShell):
    SECTION = "Help"
    CRUMBS = "Главная › Справка"
    AUTO_FOCUS = "#help-scroll"
    DEFAULT_CSS = brand.shell_css("""
    HelpScreen #help-layout { height: 1fr; }
    HelpScreen #help-toc { width: 23; height: auto; border: none; margin: 1 1 0 2; padding: 0; }
    HelpScreen #help-toc > ListItem { height: 1; }
    HelpScreen #help-scroll { width: 1fr; height: 1fr; padding: 0 2; }
    HelpScreen Markdown { padding: 0; }
    HelpScreen MarkdownH1 { margin: 0 0 1 0; padding: 0; border: none; content-align: left middle; }
    HelpScreen .help-section { border: none; margin: 0; padding: 0; }
    HelpScreen .help-section > Contents { padding: 0; }
    HelpScreen .help-section > CollapsibleTitle { display: none; }
    HelpScreen.-narrow #help-toc { display: none; }
    HelpScreen.-narrow .help-section { border: round $border-subtle; padding: 0 1; }
    HelpScreen.-narrow .help-section > CollapsibleTitle { display: block; }
    HelpScreen.-narrow .help-section MarkdownH2 { display: none; }
    """)

    def __init__(self) -> None:
        super().__init__()
        self._help_narrow: bool | None = None
        self._navigation_blocked = False

    def _parts(self) -> tuple[str, list[tuple[str, str]]]:
        intro, *parts = help_markdown(getattr(self.app, "language", "ru")).split(
            "\n## "
        )
        return intro, [(part.split("\n", 1)[0], "## " + part) for part in parts]

    def compose_strip(self) -> ComposeResult:
        yield Static("", id="help-source", classes="no-i18n", markup=False)

    def compose_content(self) -> ComposeResult:
        intro, parts = self._parts()
        with Horizontal(id="help-layout"):
            yield ListView(
                *(ListItem(Static(title, classes="no-i18n")) for title, _ in parts),
                id="help-toc",
            )
            with VerticalScroll(id="help-scroll"):
                yield Markdown(intro, id="help-text")
                for index, (title, body) in enumerate(parts):
                    yield Fold(
                        Markdown(body, id=f"help-body-{index}"),
                        title=title,
                        id=f"help-section-{index}",
                        classes="help-section",
                        collapsed=self.app.size.width < brand.NARROW_WIDTH
                        and index != 0,
                    )

    def on_mount(self) -> None:
        self._adapt_sections(self.size.width)
        self._update_source()

    def _update_source(self) -> None:
        language = getattr(self.app, "language", "ru")
        source = next(
            (
                screen.SECTION
                for screen in reversed(self.app.screen_stack[:-1])
                if isinstance(screen, ConsoleShell)
            ),
            None,
        )
        text = i18n.tr(language, "Справка F1")
        if source:
            label = i18n.section_label(language, source)
            text += f" · {i18n.tr(language, 'Вы пришли с экрана')}: {label}"
        if self._navigation_blocked:
            text += " · ⚠ " + i18n.tr(language, "Сначала завершите или отмените диалог")
        self.query_one("#help-source", Static).update(text)

    def _adapt_sections(self, width: int) -> None:
        narrow = width < brand.NARROW_WIDTH
        if self._help_narrow == narrow:
            return
        self._help_narrow = narrow
        for index, fold in enumerate(self.query(".help-section").results(Fold)):
            fold.collapsed = narrow and index != 0 and not fold.has_focus_within

    def on_resize(self, event: events.Resize) -> None:
        self._adapt_sections(event.size.width)

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "help-toc":
            event.stop()
            index = event.list_view.index
            if index is not None:
                fold = self.query_one(f"#help-section-{index}", Fold)
                fold.collapsed = False
                fold.scroll_visible(top=True, animate=False)

    async def open_section(self, index: int) -> None:
        """Return through the opener's navigation guard instead of popping past a draft or dialog."""
        source = self.app.screen_stack[-2]
        if isinstance(source, ConsoleShell):
            self.dismiss(None)
            self.app.call_later(source.action_section, index)
        else:
            self._navigation_blocked = True
            self._update_source()

    def sync_language(self) -> None:
        super().sync_language()
        intro, parts = self._parts()
        self.query_one("#help-text", Markdown).update(intro)
        for index, (title, body) in enumerate(parts):
            self.query_one(f"#help-body-{index}", Markdown).update(body)
            self.query_one("#help-toc", ListView).children[index].query_one(
                Static
            ).update(title)
        self._update_source()
