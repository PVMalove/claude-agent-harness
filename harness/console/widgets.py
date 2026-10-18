"""Console widgets that restyle Textual's: checkboxes and tick lists drawn as ☑ / ☐, so a ticked
and an unticked item differ by shape and not only by colour; and a page container a screen can
rebuild without recomposing its frame."""

from __future__ import annotations

from collections.abc import Callable

from rich.segment import Segment
from rich.style import Style
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.strip import Strip
from textual.widgets import Checkbox, ListView, RadioButton, SelectionList
from textual.widgets.option_list import OptionDoesNotExist

from . import brand

CHECKED = "☑"
UNCHECKED = "☐"


def _glyph(checked: bool) -> tuple[str, str]:
    """The glyph and its colour for a ticked or unticked item."""
    if checked:
        return CHECKED, brand.STATUS_COLORS["ok"]
    return UNCHECKED, brand.PALETTE["muted"]


class CheckBox(Checkbox):
    """A `Checkbox` drawn as ☑ / ☐ (three cells wide, like the original button)."""

    @property
    def _button(self) -> Content:
        glyph, color = _glyph(bool(self.value))
        return Content.styled(f" {glyph} ", f"bold {color}")


class RoundRadioButton(RadioButton):
    """A radio option with a filled or empty circle, without a button background."""

    @property
    def _button(self) -> Content:
        glyph = "●" if self.value else "○"
        color = brand.PALETTE["primary"] if self.value else brand.PALETTE["muted"]
        return Content.styled(glyph, color)


class CheckList(SelectionList[str]):
    """A `SelectionList` whose rows start with ☑ / ☐ instead of the coloured `X` button."""

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        segments = list(strip)
        if len(segments) < 4:
            return strip
        _, scroll_y = self.scroll_offset
        try:
            selection = self.get_option_at_index(scroll_y + y)
        except OptionDoesNotExist:
            return strip
        glyph, color = _glyph(selection.value in self._selected)
        if selection.disabled:
            # A locked row (e.g. an installed parser) keeps its glyph but loses the accent.
            color = brand.PALETTE["muted"]
        base = segments[3].style or Style()
        style = Style(
            color=color,
            bgcolor=base.bgcolor,
            bold=True,
            meta=dict(segments[1].style.meta) if segments[1].style else None,
        )
        return Strip([Segment(f" {glyph} ", style), *segments[3:]])


class OpenableList(ListView):
    """A `ListView` whose keys the footer names: `↑↓ Выбор` and `Enter Открыть`. The keys are the
    list's own; only their footer hints are new."""

    BINDINGS = [
        Binding("down", "cursor_down", "Выбор", key_display="↑↓"),
        Binding("enter", "select_cursor", "Открыть"),
    ]


class ScrollingView(VerticalScroll):
    """A `VerticalScroll` whose key the footer names: `↑↓ Прокрутка`. The keys are the scroll's own;
    only their footer hint is new."""

    BINDINGS = [Binding("down", "scroll_down", "Прокрутка", key_display="↑↓")]


class ComposedPage(Vertical):
    """The page of a screen on the shared frame: its children come from `build`, and `recompose()`
    rebuilds only them, so the frame (menu, breadcrumbs, footer) keeps its state and focus ring."""

    def __init__(
        self,
        build: Callable[[], ComposeResult],
        *,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self._build = build

    def compose(self) -> ComposeResult:
        yield from self._build()
