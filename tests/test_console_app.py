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
from harness.console.data import DashboardData
from harness.console.screens.dashboard import SECTIONS, DashboardScreen
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


def _fake_dashboard_data() -> DashboardData:
    return DashboardData(
        ok=5,
        warn=1,
        fail=0,
        skipped=2,
        repo_map_tier="full",
        harness_version="9.9.9",
        drift_state="clean",
        active_batches=3,
    )


def test_dashboard_screen_renders_injected_data_without_touching_the_repo(
    tmp_path: Path,
) -> None:
    """A fake `collect_dashboard` proves the screen never needs a real health/drift/ledger
    environment to be tested - the seam harness.console.screens.dashboard.DashboardScreen
    accepts."""

    async def scenario() -> str:
        from textual.app import App
        from textual.widgets import Static

        screen = DashboardScreen(tmp_path, collect_dashboard=lambda _repo: _fake_dashboard_data())

        class _HostApp(App[None]):
            def on_mount(self) -> None:
                self.push_screen(screen)

        app = _HostApp()
        async with app.run_test():
            summary = app.screen.query_one("#dashboard-summary", Static)
            return str(summary.content)

    rendered = asyncio.run(scenario())
    assert "ok=5" in rendered
    assert "warn=1" in rendered
    assert "fail=0" in rendered
    assert "active batches: 3" in rendered
    assert "9.9.9" in rendered
    assert "clean" in rendered
