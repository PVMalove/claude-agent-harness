"""Pure stdlib parser for the canonical label tables in a target project's
`docs/agents/triage-labels.md` (ticket #346's tracker.labels check reads them at run time - this
module never ships fixed label data of its own).

Only a GFM table whose header row has both a `Label` and a `Color` column is read: the type
axis (`type::*`) and the purely informational `priority::*`/`severity::*`/`env::*` axes have no
Color column and are intentionally skipped, exactly as `/setup-labels`'s own first step does.
"""

from __future__ import annotations

import re

_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
_CODE_SPAN = re.compile(r"`([^`]+)`")
_SEPARATOR_CELL = re.compile(r":?-+:?")


def _cells(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(
        cell == "" or _SEPARATOR_CELL.fullmatch(cell) for cell in cells
    )


def _clean_label(cell: str) -> str | None:
    match = _CODE_SPAN.search(cell)
    name = (match.group(1) if match else cell).strip()
    return name or None


def _clean_color(cell: str) -> str | None:
    match = _HEX_COLOR.search(cell)
    return match.group(0).lower() if match else None


def parse_canonical_labels(text: str) -> list[tuple[str, str]]:
    """Parse every GFM table in `text` whose header has both a `Label` and a `Color` column.

    Returns `(name, "#rrggbb")` pairs in document order. A duplicate label name (the same label
    could in principle appear in two tables) keeps only its first occurrence. Markdown code spans
    around a label name (`` `hitl` ``) are unwrapped; a color cell's surrounding prose (e.g.
    "yellow `#fbca04`") is ignored and only the hex code is kept.
    """
    lines = text.splitlines()
    labels: list[tuple[str, str]] = []
    seen: set[str] = set()
    index = 0
    while index < len(lines) - 1:
        header_cells = _cells(lines[index])
        separator_cells = _cells(lines[index + 1])
        if (
            len(header_cells) >= 2
            and len(separator_cells) == len(header_cells)
            and _is_separator_row(separator_cells)
        ):
            lowered = [cell.lower() for cell in header_cells]
            if "label" in lowered and "color" in lowered:
                label_index = lowered.index("label")
                color_index = lowered.index("color")
                row = index + 2
                while row < len(lines) and lines[row].strip().startswith("|"):
                    cells = _cells(lines[row])
                    if len(cells) > max(label_index, color_index):
                        name = _clean_label(cells[label_index])
                        color = _clean_color(cells[color_index])
                        if name and color and name not in seen:
                            labels.append((name, color))
                            seen.add(name)
                    row += 1
                index = row
                continue
        index += 1
    return labels
