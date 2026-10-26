"""The matrix and SVG viewport are acceptance evidence, independent of generated files."""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET

import pytest

from tests.console._svg_snapshot import mockup_states
from tests.console.diagnostics.snapshots import (
    CAPTIONS,
    LANGUAGES,
    SIZES,
    STATES,
    capture,
)


def test_matrix_covers_every_mockup_state_in_both_languages_and_sizes() -> None:
    assert set(CAPTIONS.values()) >= set(mockup_states("diagnostics.html"))
    assert (
        len(
            {
                (state, size, language)
                for state in STATES
                for size in SIZES
                for language in LANGUAGES
            }
        )
        == 24
    )


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize(
    "language,heading,all_label",
    [("ru", "Диагностика", "Все"), ("en", "Diagnostics", "All")],
)
def test_svg_shows_the_requested_state_and_language(
    state: str, size: tuple[int, int], language: str, heading: str, all_label: str
) -> None:
    svg = asyncio.run(capture(state, size, language))
    root = ET.fromstring(svg)
    text = " ".join(root.itertext()).replace("\u00a0", " ")
    assert heading in text
    assert all_label in text
    assert "diagnostics.html" in text and f"{size[0]}x{size[1]}" in text
    if state == "baseline":
        assert "files.skill_snapshot" in text
        assert "5" in text and "2" in text
    elif state == "building":
        assert "health is running" in text or "health выполняется" in text
        assert "files.skill_snapshot" not in text
    elif state == "failure":
        assert "diagnostic fixture failure" in text
        assert "files.skill_snapshot" not in text
    elif state == "fix-first":
        assert "Confirm fixes" in text or "Подтвердить фиксы" in text, [
            "".join(node.itertext())
            for node in root.iter()
            if node.tag.endswith("text")
            and (
                "Под" in "".join(node.itertext())
                or "Confirm" in "".join(node.itertext())
            )
        ]
    elif state == "fix-applied":
        assert "Создан каталог .harness/logs" in text
    else:
        assert "online" in text or "онлайн" in text
        assert "✖ 3" in text
