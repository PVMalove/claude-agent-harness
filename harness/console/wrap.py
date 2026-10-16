"""Wrapping a value that has no spaces - a path, an ID, a branch - at its own separators.

Stdlib-only, like the rest of the textual-free seam. A terminal widget wraps by words, so a long path
is cut at an arbitrary column in the middle of a name; `wrap_value` breaks it after a `/`, `-`, `_`,
`.` and the like, or at a space, and only when a single piece is wider than the line does it
stay on a line of its own."""

from __future__ import annotations

SEPARATORS = "/\\-_.,;=@"


def _pieces(line: str) -> list[str]:
    """`line` cut after every separator and at every space; a space is its own piece."""
    pieces: list[str] = []
    current = ""
    for char in line:
        if char.isspace():
            if current:
                pieces.append(current)
                current = ""
            pieces.append(" ")
            continue
        current += char
        if char in SEPARATORS:
            pieces.append(current)
            current = ""
    if current:
        pieces.append(current)
    return pieces


def _wrap_line(line: str, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    space = False
    for piece in _pieces(line):
        if piece == " ":
            space = True
            continue
        joined = current + (" " if space and current else "") + piece
        if current and len(joined) > width:
            lines.append(current)
            current = piece
        else:
            current = joined
        space = False
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
