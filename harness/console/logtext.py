"""Colour for log-like text: health reports, command output and exit codes.

The text itself is never changed - only styled - so a shown log still copies, exports and compares
as the plain string it was. Stdlib plus rich, no textual."""

from __future__ import annotations

import re

from rich.text import Text

from . import brand

_COLORS = brand.STATUS_COLORS
_MARKERS = {"[OK]": "ok", "[WARN]": "warn", "[FAIL]": "fail"}
_COUNTER = re.compile(r"\b(ok|warn|fail|skipped)=(\d+)")
_COUNT_LINE = re.compile(r"^(Требует исправления|Не проверено): (\d+)")
_EXIT_CODE = re.compile(r"^(код выхода: )(\d+)$")
_ERROR_WORDS = re.compile(r"(?i)\b(traceback|error|ошибка|не удалось|сбой|failed)\b")
_WARNING_WORDS = re.compile(r"(?i)\b(warning|предупреждение)\b")


def _line(out: Text, line: str) -> None:
    palette = brand.PALETTE
    for marker, status in _MARKERS.items():
        if line.startswith(marker + " "):
            out.append(marker, style=f"bold {_COLORS[status]}")
            out.append(line[len(marker) :])
            return
    exit_code = _EXIT_CODE.match(line)
    if exit_code:
        status = "ok" if exit_code.group(2) == "0" else "fail"
        out.append(exit_code.group(1), style=palette["muted"])
        out.append(exit_code.group(2), style=f"bold {_COLORS[status]}")
        return
    count_line = _COUNT_LINE.match(line)
    if count_line:
        status = "fail" if count_line.group(1).startswith("Требует") else "warn"
        number_color = (
            _COLORS[status] if count_line.group(2) != "0" else palette["muted"]
        )
        out.append(f"{count_line.group(1)}: ", style="bold")
        out.append(count_line.group(2), style=f"bold {number_color}")
        out.append(line[count_line.end() :])
        return
    if line.startswith("Итого:"):
        position = 0
        for counter in _COUNTER.finditer(line):
            out.append(line[position : counter.start()], style="bold")
            color = (
                _COLORS[counter.group(1)]
                if counter.group(2) != "0"
                else palette["muted"]
            )
            out.append(counter.group(0), style=f"bold {color}")
            position = counter.end()
        out.append(line[position:], style="bold")
        return
    if line.startswith("$ "):
        out.append(line, style=f"bold {palette['secondary']}")
    elif line.startswith("  -> "):
        out.append(line, style=palette["secondary"])
    elif line.startswith(("     ", "  - ", "- ")) or line.endswith("…"):
        out.append(line, style=palette["muted"])
    elif _ERROR_WORDS.search(line):
        out.append(line, style=_COLORS["fail"])
    elif _WARNING_WORDS.search(line):
        out.append(line, style=_COLORS["warn"])
    else:
        out.append(line)


def colorize_log(text: str) -> Text:
    """`text` as a `Text` with statuses, counters, exit codes, commands and errors coloured."""
    styled = Text()
    for index, line in enumerate(text.split("\n")):
        if index:
            styled.append("\n")
        _line(styled, line)
    return styled


def colorize_diff(text: str) -> Text:
    """A unified diff with additions green, removals red and hunk headers highlighted."""
    palette = brand.PALETTE
    styled = Text()
    for index, line in enumerate(text.split("\n")):
        if index:
            styled.append("\n")
        if line.startswith(("+++", "---")):
            styled.append(line, style=f"bold {palette['muted']}")
        elif line.startswith("@@"):
            styled.append(line, style=palette["secondary"])
        elif line.startswith("+"):
            styled.append(line, style=_COLORS["ok"])
        elif line.startswith("-"):
            styled.append(line, style=_COLORS["fail"])
        elif line.startswith("…"):
            styled.append(line, style=palette["muted"])
        else:
            styled.append(line)
    return styled


def colorize_overview(text: str) -> Text:
    """`Key: value` lines with the key in the accent colour, as on the dashboard's details."""
    palette = brand.PALETTE
    styled = Text()
    for index, line in enumerate(text.split("\n")):
        if index:
            styled.append("\n")
        key, separator, value = line.partition(": ")
        if separator and len(key) <= 40:
            styled.append(f"{key}:", style=f"bold {palette['primary']}")
            styled.append(f" {value}")
        else:
            styled.append(line)
    return styled
