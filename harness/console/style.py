"""Shared terminal rendering for the console."""

from rich.color import Color
from rich.segment import Segment, Segments
from rich.style import Style
from textual.scrollbar import ScrollBarRender

from . import brand


class ThinScrollBarRender(ScrollBarRender):
    """Draw a roughly one-third-cell track, retaining Textual's mouse targets."""

    @classmethod
    def render_bar(
        cls,
        size: int = 25,
        virtual_size: float = 50,
        window_size: float = 20,
        position: float = 0,
        thickness: int = 1,
        vertical: bool = True,
        back_color: Color = Color.parse(brand.SHELL_TOKENS["scrollbar-track"]),
        bar_color: Color = Color.parse("bright_magenta"),
    ) -> Segments:
        bar = super().render_bar(
            size,
            virtual_size,
            window_size,
            position,
            thickness,
            vertical,
            back_color,
            bar_color,
        )
        glyph = "▎" if vertical else "▃"
        background = Color.parse(brand.PALETTE["background"])
        segments = []
        for segment in bar.segments:
            if segment.text == "\n" or segment.control:
                segments.append(segment)
                continue
            meta = segment.style.meta if segment.style else {}
            color = bar_color if meta.get("@mouse.down") == "grab" else back_color
            segments.append(
                Segment(
                    glyph * segment.cell_length,
                    Style(color=color, bgcolor=background, meta=meta),
                )
            )
        return Segments(segments, new_lines=bar.new_lines)
