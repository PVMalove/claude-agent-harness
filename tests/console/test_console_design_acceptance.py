"""The #720 design contract at the application seam, including Help (#725)."""

from __future__ import annotations

import asyncio
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from textual.widgets import ListView

from harness.console.app import HarnessConsoleApp
from harness.console.screens.help import HelpScreen
from harness.console.shell import (
    Breadcrumbs,
    ConsoleShell,
    Fold,
    SECTIONS,
    ShellSidebar,
)
from tests.console._pilot_wait import crumb_text, wait_until


def _visible_text(app: HarnessConsoleApp) -> str:
    root = ET.fromstring(app.export_screenshot())
    text = " ".join(
        "".join(node.itertext()).replace("\u00a0", " ")
        for node in root.iter("{http://www.w3.org/2000/svg}text")
    )
    return " ".join(text.split())


@pytest.mark.parametrize("size", [(120, 40), (80, 24)])
def test_home_context_follows_the_report_without_an_empty_screen(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    from tests.console.test_console_home import _home

    async def scenario() -> None:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=size) as pilot:
            home = _home(tmp_path, collect_titles=lambda *_: {})
            await app.switch_screen(home)
            await pilot.pause()

            report = home.query_one("#dashboard-details")
            context = home.query_one(".context-fold")
            assert context.region.y - report.region.bottom <= 2
            assert home.region.contains_region(context.region)

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(160, 52), (120, 40), (80, 24)])
def test_f1_keeps_the_shell_and_returns_to_the_same_screen_and_focus(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    async def scenario() -> None:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=size) as pilot:
            await pilot.press("6")
            await wait_until(
                pilot,
                lambda: isinstance(app.screen, ConsoleShell)
                and app.screen.SECTION == "Reports",
            )
            source, focused = app.screen, app.focused

            await pilot.press("f1")
            await wait_until(pilot, lambda: isinstance(app.screen, HelpScreen))
            help_screen = app.screen
            assert isinstance(help_screen, ConsoleShell)
            assert (
                crumb_text(help_screen.query_one(Breadcrumbs)) == "Главная  ›  Справка"
            )
            assert [
                item.name
                for item in help_screen.query_one("#shell-sections", ListView).children
            ] == list(SECTIONS)
            assert help_screen.query_one(ShellSidebar).display == (size[0] >= 100)

            await pilot.press("f2")
            await wait_until(pilot, lambda: "Console guide" in _visible_text(app))
            assert crumb_text(help_screen.query_one(Breadcrumbs)) == "Home  ›  Help"
            await pilot.press("escape")
            await wait_until(pilot, lambda: app.screen is source)
            assert app.focused is focused
            assert app.language == "en"

    asyncio.run(scenario())


def test_help_contents_reach_statuses_and_narrow_sections_remain_accessible(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(160, 52)) as pilot:
            await pilot.press("f1")
            await wait_until(pilot, lambda: isinstance(app.screen, HelpScreen))
            toc = app.screen.query_one("#help-toc", ListView)
            toc.focus()
            await pilot.press("down", "down", "down", "enter")
            await wait_until(
                pilot, lambda: "✔" in _visible_text(app) and "Ⅱ" in _visible_text(app)
            )
            assert "СТАТУСЫ" in _visible_text(app) or "Статусы" in _visible_text(app)

            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            folds = list(app.screen.query(".help-section").results(Fold))
            assert len(folds) == 6
            assert not toc.display
            assert any(fold.collapsed for fold in folds)
            statuses = app.screen.query_one("#help-section-3", Fold)
            statuses.collapsed = False
            statuses.scroll_visible(top=True, animate=False)
            await wait_until(pilot, lambda: "Ⅱ" in _visible_text(app))
            assert "Ⅱ" in _visible_text(app)

    asyncio.run(scenario())


def test_help_navigation_preserves_the_unsaved_configuration_guard(
    tmp_path: Path,
) -> None:
    from harness.console.screens.configuration import (
        ConfigurationScreen,
        ConfirmConfiguration,
    )
    from tests.console.test_console_configuration_app import (
        installed_project,
        _change_draft,
        _open_wizard_from_home,
    )

    installed_project(tmp_path)

    async def scenario() -> None:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            wizard = await _open_wizard_from_home(app, pilot)
            _change_draft(wizard)
            await pilot.press("f1")
            await wait_until(pilot, lambda: isinstance(app.screen, HelpScreen))

            await pilot.press("0")
            await wait_until(
                pilot, lambda: isinstance(app.screen, ConfirmConfiguration)
            )
            await pilot.click("#cancel")
            await wait_until(pilot, lambda: isinstance(app.screen, ConfigurationScreen))
            assert app.screen is wizard
            assert wizard.draft is not None and wizard.draft.changed()

    asyncio.run(scenario())


@pytest.mark.parametrize("language", ["ru", "en"])
def test_help_never_navigates_past_an_open_confirmation(
    tmp_path: Path, language: str
) -> None:
    from harness.console.screens.configuration import ConfirmConfiguration

    async def scenario() -> None:
        app = HarnessConsoleApp(tmp_path)
        app.language = language
        async with app.run_test(size=(120, 40), notifications=True) as pilot:
            confirmation = ConfirmConfiguration("Confirm a pending change")
            await app.push_screen(confirmation)
            app.action_open_help()
            await wait_until(pilot, lambda: isinstance(app.screen, HelpScreen))

            await pilot.press("0")
            assert isinstance(app.screen, HelpScreen)
            message = {
                "ru": "Сначала завершите или отмените диалог",
                "en": "Complete or cancel the dialog first",
            }[language]
            await wait_until(pilot, lambda: message in _visible_text(app))
            assert confirmation in app.screen_stack
            await pilot.press("escape")
            await wait_until(pilot, lambda: app.screen is confirmation)

    asyncio.run(scenario())


@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize("size", [(160, 52), (120, 40), (80, 24)])
def test_every_menu_section_keeps_navigation_language_help_and_focus_without_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    size: tuple[int, int],
    language: str,
) -> None:
    monkeypatch.setenv("HARNESS_CONSOLE_LANG", language)
    expected = (
        "Home",
        "Open project",
        "Update Harness",
        "Diagnostics",
        "Harness",
        "Orchestration",
        "Reports",
        "Repo Map",
        "Configuration",
        "Help",
    )
    calls: list[list[str]] = []

    def never_run(
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        raise AssertionError("Navigation must not run a command")

    async def scenario() -> None:
        app = HarnessConsoleApp(tmp_path, command_runner=never_run)
        async with app.run_test(size=size) as pilot:
            for number, name in enumerate(expected):
                if number:
                    app.screen.set_focus(None)
                await pilot.press(str(number))
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, ConsoleShell)
                    and app.screen.SECTION == name,
                )
                source = app.screen
                assert isinstance(source, ConsoleShell)
                assert source.query_one(ShellSidebar).display == (size[0] >= 100)
                assert source.query_one(Breadcrumbs).region.height > 0
                assert app.focused is not None, name
                assert all(
                    node.display for node in app.focused.ancestors_with_self
                ), name
                focused = app.focused

                await pilot.press("f2")
                await wait_until(pilot, lambda: app.language != language)
                await pilot.press("f2")
                await wait_until(pilot, lambda: app.language == language)
                assert app.focused is focused

                if number != 9:
                    await pilot.press("f1")
                    await wait_until(pilot, lambda: isinstance(app.screen, HelpScreen))
                    await pilot.press("escape")
                    await wait_until(pilot, lambda: app.screen is source)
                    assert app.focused is focused

            await pilot.press("0")
            await wait_until(pilot, lambda: len(app.screen_stack) == 2)
            assert isinstance(app.screen, ConsoleShell) and app.screen.SECTION == "Home"
            assert calls == []

    asyncio.run(scenario())
