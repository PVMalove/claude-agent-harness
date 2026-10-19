"""Console widgets that restyle Textual's: checkboxes and tick lists drawn as ☑ / ☐, so a ticked
and an unticked item differ by shape and not only by colour; and a page container a screen can
rebuild without recomposing its frame."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.strip import Strip
from textual.widgets import (
    Button,
    Checkbox,
    ListView,
    RadioButton,
    SelectionList,
    Static,
)
from textual.widgets.option_list import OptionDoesNotExist

from . import brand, i18n
from .status import Status

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


class ActionButton(Button):
    """A `Button` whose Enter key the footer names, `Enter Выбрать` (Textual's own binding is hidden)."""

    BINDINGS = [Binding("enter", "press", "Выбрать")]


class ReasonNote(Static):
    """Why actions are unavailable: one muted line `«label»: reason` per disabled button, and the same
    reason as that button's tooltip (a disabled button cannot take focus, so a tooltip alone is not
    enough). `reasons` returns the (button, reason) pairs that apply now; `relabel` asks again, so a
    screen calls it after a state change or a language switch. With no pair the note is hidden."""

    DEFAULT_CLASSES = "reason-note no-i18n"

    def __init__(
        self,
        reasons: Callable[[], Iterable[tuple[Button, str]]],
        *,
        id: str | None = None,
    ) -> None:
        super().__init__("", id=id, markup=False)
        self._reasons = reasons

    def on_mount(self) -> None:
        self.relabel()

    def relabel(self) -> None:
        language = getattr(self.app, "language", "ru")
        lines = []
        for button, reason in self._reasons():
            text = i18n.tr_text(language, reason)
            button.tooltip = text
            lines.append(f"«{button.label}»: {text}")
        shown = "\n".join(lines)
        if shown != str(self.content):
            self.update(shown)
        self.display = bool(lines)


class StatusLine(Static):
    """A status as a symbol and a text in the console language (`✔ актуален`), coloured; `relabel`
    redraws it after a language switch."""

    DEFAULT_CLASSES = "status-line no-i18n"

    def __init__(
        self, status: Status, *, id: str | None = None, classes: str = ""
    ) -> None:
        super().__init__("", id=id, markup=False)
        self.add_class(*classes.split())
        self.status = status

    def on_mount(self) -> None:
        self.relabel()

    def set_status(self, status: Status) -> None:
        self.status = status
        self.relabel()

    def relabel(self) -> None:
        language = getattr(self.app, "language", "ru")
        self.update(Text(self.status.render(language), style=self.status.color))
