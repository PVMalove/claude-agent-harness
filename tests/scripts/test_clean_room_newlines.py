"""Clean-room comparisons of project files must not depend on the platform's line ending."""

from __future__ import annotations

import importlib

support = importlib.import_module("scripts.clean_room.support")

ORIGINAL = "# Local instructions\n\nKeep the project glossary in Russian.\n"


def test_a_crlf_file_still_starts_with_the_lf_original() -> None:
    """`write_text` writes CRLF on Windows, and an approved patch appends after it."""
    crlf = ORIGINAL.replace("\n", "\r\n").encode("utf-8") + b"+ added line\r\n"
    assert support.starts_with_text(crlf, ORIGINAL)


def test_an_lf_file_starts_with_the_original() -> None:
    assert support.starts_with_text(ORIGINAL.encode("utf-8") + b"added\n", ORIGINAL)


def test_a_file_that_lost_its_local_text_does_not_match() -> None:
    assert not support.starts_with_text(b"# Local instructions\r\n", ORIGINAL)
    assert not support.starts_with_text(
        b"added\r\n" + ORIGINAL.encode("utf-8"), ORIGINAL
    )
