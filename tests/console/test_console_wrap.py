"""wrap_value: long paths and IDs break after their separators, never in the middle of a name."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from harness.console import wrap
from harness.console.wrap import SEPARATORS, wrap_value

PATH = "/home/user/Projects/Code/some-project/.harness/.sandboxes/worktrees/issue-721-console-design-system"
IDENTIFIER = "dispatch-e98a4a72-50ac-4fd8-b421-1db55c17f287"


def test_a_value_that_fits_is_unchanged() -> None:
    assert wrap_value(IDENTIFIER, 80) == IDENTIFIER
    assert wrap_value("", 10) == ""
    assert wrap_value(PATH, 0) == PATH


@pytest.mark.parametrize("width", [12, 16, 20, 24, 33, 40])
@pytest.mark.parametrize("value", [PATH, IDENTIFIER])
def test_lines_fit_and_every_break_follows_a_separator(value: str, width: int) -> None:
    lines = wrap_value(value, width).split("\n")
    assert "".join(lines) == value
    for line in lines[:-1]:
        assert line[-1] in SEPARATORS
    for line in lines:
        longest_piece = max(len(piece) for piece in re.split(r"(?<=[/\-_.])", line))
        assert len(line) <= width or longest_piece == len(line)


def test_a_path_is_cut_at_slashes_and_an_id_at_hyphens() -> None:
    assert wrap_value("/aa/bb/cc/dd", 7).split("\n") == ["/aa/bb/", "cc/dd"]
    assert wrap_value("one-two-three-four", 9).split("\n") == [
        "one-two-",
        "three-",
        "four",
    ]


def test_spaces_are_break_points_and_existing_line_breaks_stay() -> None:
    assert wrap_value("alpha beta gamma", 10).split("\n") == ["alpha beta ", "gamma"]
    assert wrap_value("one\ntwo three", 20) == "one\ntwo three"
    assert wrap_value("first line\nsecond-line", 8).split("\n") == [
        "first ",
        "line",
        "second-",
        "line",
    ]


SPACED = [
    "a  b   c",
    "  indent x",
    "trailing  ",
    "  both  ",
    "name   value   /some/long-path/to_file.py",
    "   ",
    "a, b,  c",
]


@pytest.mark.parametrize("width", [3, 5, 8, 12, 80])
@pytest.mark.parametrize("value", SPACED)
def test_spaces_survive_so_that_joining_the_lines_returns_the_value(
    value: str, width: int
) -> None:
    lines = wrap_value(value, width).split("\n")
    assert "".join(lines) == value


def test_a_run_of_spaces_stays_when_the_value_fits() -> None:
    assert wrap_value("a  b   c", 20) == "a  b   c"
    assert wrap_value("  indent x", 20) == "  indent x"


def test_a_piece_wider_than_the_line_is_not_cut() -> None:
    assert wrap_value("prefix/abcdefghijklmnop", 8).split("\n") == [
        "prefix/",
        "abcdefghijklmnop",
    ]


def test_the_module_does_not_import_textual() -> None:
    source = Path(wrap.__file__).read_text(encoding="utf-8")
    assert not re.search(
        r"^\s*(?:from|import)\s+(?:textual|rich)\b", source, re.MULTILINE
    )
