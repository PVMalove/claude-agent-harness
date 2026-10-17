"""Wrapping a value that has no spaces - a path, an ID, a branch - at its own separators.

Stdlib-only, like the rest of the textual-free seam. A terminal widget wraps by words, so a long path
is cut at an arbitrary column in the middle of a name; `wrap_value` breaks it after a `/`, `-`, `_`,
`.` and the like, or at a space, and only when a single piece is wider than the line does it
stay on a line of its own. Spaces are kept, so the lines joined return the value."""

from __future__ import annotations

SEPARATORS = "/\\-_.,;=@"


def _pieces(line: str) -> list[str]:
    """`line` cut after every separator and before every word; the spaces after a piece stay on it,
    leading spaces on the first, so the pieces joined return `line` unchanged."""
    pieces: list[str] = []
    current = ""
    for char in line:
        if (
            not char.isspace()
            and current.strip()
            and (current[-1].isspace() or current[-1] in SEPARATORS)
        ):
            pieces.append(current)
            current = ""
        current += char
    if current:
        pieces.append(current)
    return pieces


def _wrap_line(line: str, width: int) -> list[str]:
    """The pieces of `line` packed into lines; a line may end in spaces, which do not count
    toward its width."""
    lines: list[str] = []
    current = ""
    for piece in _pieces(line):
        if current and len((current + piece).rstrip()) > width:
            lines.append(current)
            current = piece
        else:
            current += piece
    lines.append(current)
    return lines


def wrap_value(value: str, width: int) -> str:
    """`value` with line breaks that keep every line within `width` cells wherever a separator
    allows it; existing line breaks stay."""
    if width <= 0:
        return value
    return "\n".join(
        wrapped for line in value.split("\n") for wrapped in _wrap_line(line, width)
    )
