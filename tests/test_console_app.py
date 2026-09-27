"""harness.console.app: the textual TUI. Skipped entirely when textual is not installed - this
module is the seam DoD calls out: "Pilot tests use a recording runner and are skipped when
textual is unavailable". Tests drive the App with `asyncio.run` rather than pytest-asyncio, since
the harness dev dependency group carries no async pytest plugin."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("textual")

from harness.console.app import HarnessConsoleApp
from harness.console.screens.dashboard import SECTIONS
from harness.console.screens.stub import StubScreen


def test_dashboard_shows_the_section_menu_in_order(tmp_path: Path) -> None:
    async def scenario() -> list[str | None]:
        from textual.widgets import ListView

        app = HarnessConsoleApp(tmp_path)
        async with app.run_test():
            menu = app.screen.query_one("#section-menu", ListView)
            return [item.name for item in menu.children]

    assert asyncio.run(scenario()) == list(SECTIONS)


def test_selecting_a_stub_section_pushes_its_stub_screen(tmp_path: Path) -> None:
    async def scenario() -> tuple[bool, str]:
        from textual.widgets import ListView

        app = HarnessConsoleApp(tmp_path)
        async with app.run_test() as pilot:
            menu = app.screen.query_one("#section-menu", ListView)
            menu.index = SECTIONS.index("Harness")
            await pilot.press("enter")
            await pilot.pause()
            return isinstance(app.screen, StubScreen), getattr(
                app.screen, "section_name", ""
            )

    is_stub, section_name = asyncio.run(scenario())
    assert is_stub
    assert section_name == "Harness"
