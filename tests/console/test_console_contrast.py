"""WCAG contrast of the console's colour tokens, measured against the real palette.

Component edges, controls, the selection edge and the scrollbar must stand out from the surface by
3:1; text on a highlighted row by 4.5:1; decorative dividers stay deliberately faint."""

from __future__ import annotations

# ruff: noqa: E402 — optional Textual imports must follow importorskip.

import asyncio
import re
from pathlib import Path

import pytest

pytest.importorskip("textual")

from harness.console import brand, shell

SURFACE = brand.PALETTE["surface"]
TOKENS = brand.SHELL_TOKENS
HIGHLIGHTED_ROWS = {
    "row-highlight": TOKENS["row-highlight"],
    "selection-background": TOKENS["selection-background"],
    "event-active-background": TOKENS["event-active-background"],
}


def _luminance(color: str) -> float:
    channels = [int(color.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    red, green, blue = (
        value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    )
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(foreground: str, background: str) -> float:
    """WCAG contrast ratio of two `#rrggbb` colours."""
    lighter, darker = sorted((_luminance(foreground), _luminance(background)))[::-1]
    return (lighter + 0.05) / (darker + 0.05)


@pytest.mark.parametrize(
    "token",
    ["component-border", "selection-edge", "scrollbar-thumb", "scrollbar-track"],
)
def test_component_tokens_reach_three_to_one_against_the_surface(token: str) -> None:
    assert contrast(TOKENS[token], SURFACE) >= 3.0


def test_decorative_dividers_stay_subtle() -> None:
    assert contrast(TOKENS["border-subtle"], SURFACE) < 2.0
    assert contrast(TOKENS["border-subtle"], SURFACE) < contrast(
        TOKENS["component-border"], SURFACE
    )


@pytest.mark.parametrize("row", sorted(HIGHLIGHTED_ROWS))
def test_text_on_a_highlighted_row_reaches_four_and_a_half_to_one(row: str) -> None:
    background = HIGHLIGHTED_ROWS[row]
    assert contrast(brand.PALETTE["muted"], background) >= 4.5
    assert contrast(brand.PALETTE["foreground"], background) >= 4.5
    for role, color in brand.ROLE_COLORS.items():
        assert contrast(color, background) >= 4.5, role


def test_muted_text_reaches_four_and_a_half_to_one_on_the_surfaces() -> None:
    for surface in ("surface", "background", "panel"):
        assert contrast(brand.PALETTE["muted"], brand.PALETTE[surface]) >= 4.5


def test_the_shell_draws_component_borders_with_the_component_token() -> None:
    css = brand.shell_css(shell.SHELL_CSS)
    for rule in re.findall(r"border: round (#[0-9a-fA-F]{6})", css):
        assert rule in {TOKENS["component-border"], brand.PALETTE["primary"]}
    assert TOKENS["component-border"] in css


def test_a_shell_button_is_drawn_with_the_component_border(tmp_path: Path) -> None:
    from textual.color import Color
    from textual.widgets import Button

    from harness.console.app import HarnessConsoleApp
    from harness.console.screens.worker_observation import WorkerObservationScreen

    async def scenario() -> Color:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(160, 52)) as pilot:
            await app.push_screen(
                WorkerObservationScreen(tmp_path, observer=lambda *_: {})
            )
            await pilot.pause()
            return app.screen.query_one("#worker-details", Button).styles.border_top[1]

    assert asyncio.run(scenario()) == Color.parse(TOKENS["component-border"])
