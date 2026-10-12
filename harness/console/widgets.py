"""Console widgets that restyle Textual's: checkboxes and tick lists drawn as ☑ / ☐, so a ticked
and an unticked item differ by shape and not only by colour."""

from __future__ import annotations

from rich.segment import Segment
from rich.style import Style
from textual.content import Content
from textual.strip import Strip
from textual.widgets import Checkbox, RadioButton, SelectionList
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
