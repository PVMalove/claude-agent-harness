"""Breadcrumbs as data: the segments of a screen's path and their collapse on a narrow terminal.

Stdlib-only, like the rest of the textual-free seam; `shell.Breadcrumbs` draws what this decides."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

SEPARATOR = "  ›  "
ELLIPSIS = "…"


@dataclass(frozen=True)
class Crumb:
    """One segment. A link opens the menu `section`, or runs the screen's `action_<action>` (a
    view, never a command); a segment with neither is plain text."""

    label: str
    section: str | None = None
    action: str | None = None

    @property
    def is_link(self) -> bool:
        return self.section is not None or self.action is not None


def split_path(path: str) -> list[str]:
    """The labels of `Главная › Отчёты`."""
    return [part.strip() for part in path.split("›")]


def _width(labels: Sequence[str], shown: Sequence[int | None]) -> int:
    texts = [ELLIPSIS if index is None else labels[index] for index in shown]
    return sum(len(text) for text in texts) + len(SEPARATOR) * (len(texts) - 1)


def collapse_crumbs(labels: Sequence[str], width: int) -> list[int | None]:
    """The segments to show in `width` cells, as indices into `labels`; `None` is the ellipsis.

    The first and the last segment always stay. When everything does not fit, middle segments
    are hidden from the first one onward and one ellipsis stands for all of them."""
    count = len(labels)
    everything: list[int | None] = list(range(count))
    if count <= 2 or _width(labels, everything) <= width:
        return everything
    for hidden in range(1, count - 1):
        shown: list[int | None] = [0, None, *range(hidden + 1, count)]
        if _width(labels, shown) <= width:
            return shown
    return [0, None, count - 1]
