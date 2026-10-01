"""harness.console.screens.harness: Pilot tests of the Harness section with a recording runner.
They assert the process argv the screen invoked, that every irreversible class asks first and a
cancelled confirmation runs nothing, and that ledger reset requires typing RESET. Skipped without
textual; driven with `asyncio.run` like tests/test_console_app.py."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

import pytest

pytest.importorskip("textual")

from textual.app import App
from textual.pilot import Pilot
from textual.widgets import Input, ListItem, ListView, Static

from harness.console.catalog import (
    HARNESS_COMMANDS,
    CatalogEntry,
    Reversibility,
    process_argv,
)
from harness.console.screens.harness import CommandFormScreen, HarnessScreen


class _RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> "subprocess.CompletedProcess[str]":
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "done\n", "")


class _HostApp(App[None]):
    def __init__(self, screen: HarnessScreen) -> None:
        super().__init__()
        self._harness_screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._harness_screen)


def _select(app: App[None], key: str, entries: Sequence[CatalogEntry]) -> None:
    """Highlight `key` by its menu item, since the menu lists only commands available here."""
    assert key in {entry.key for entry in entries}
    menu = app.screen.query_one("#command-menu", ListView)
    menu.index = [item.name for item in menu.query(ListItem)].index(key)


async def _choose(
    pilot: Pilot[None], app: App[None], key: str, entries: Sequence[CatalogEntry]
) -> None:
    _select(app, key, entries)
    await pilot.press("enter")
    await pilot.pause()


async def _settle(pilot: Pilot[None], app: App[None]) -> None:
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def _entry(key: str) -> CatalogEntry:
    return next(entry for entry in HARNESS_COMMANDS if entry.key == key)


def test_a_reversible_command_runs_immediately_and_shows_its_cli_equivalent(
    tmp_path: Path,
) -> None:
    runner = _RecordingRunner()

    async def scenario() -> str:
        app = _HostApp(HarnessScreen(tmp_path, command_runner=runner))
        async with app.run_test(size=(120, 40)) as pilot:
            await _choose(pilot, app, "diff", HARNESS_COMMANDS)
            await _settle(pilot, app)
            assert not isinstance(app.screen, CommandFormScreen)
            return str(app.screen.query_one("#command-output", Static).content)

    output = asyncio.run(scenario())
    assert runner.calls == [process_argv(["harness", "diff", str(tmp_path)])]
    assert output.startswith(f"$ {_entry('diff').cli_line(tmp_path)}")
    assert "done" in output


@pytest.mark.parametrize(
    "reversibility",
    [r for r in Reversibility if r is not Reversibility.REVERSIBLE],
    ids=lambda r: r.value,
)
def test_every_irreversible_class_asks_first_and_cancel_runs_nothing(
    tmp_path: Path, reversibility: Reversibility
) -> None:
    entry = CatalogEntry(
        "probe", "probe", ("harness", "list", "{repo}"), reversibility, "x:y"
    )
    runner = _RecordingRunner()

    async def scenario() -> tuple[bool, bool]:
        app = _HostApp(HarnessScreen(tmp_path, command_runner=runner, catalog=[entry]))
        async with app.run_test(size=(120, 40)) as pilot:
            await _choose(pilot, app, "probe", [entry])
            asked = isinstance(app.screen, CommandFormScreen) and bool(
                app.screen.query("#confirmation-reason")
            )
            await pilot.click("#cancel")
            await _settle(pilot, app)
            return asked, isinstance(app.screen, HarnessScreen)

    asked, back = asyncio.run(scenario())
    assert asked
    assert back
    assert runner.calls == []


def test_confirming_an_irreversible_command_runs_it(tmp_path: Path) -> None:
    runner = _RecordingRunner()

    async def scenario() -> None:
        app = _HostApp(HarnessScreen(tmp_path, command_runner=runner))
        async with app.run_test(size=(120, 40)) as pilot:
            await _choose(pilot, app, "init", HARNESS_COMMANDS)
            await pilot.click("#run")
            await _settle(pilot, app)

    asyncio.run(scenario())
    assert runner.calls == [process_argv(["harness", "init", str(tmp_path)])]


def _with_script(repo: Path, relative: str) -> Path:
    """The Harness screen offers a `python <script>` command only where its script exists."""
    script = repo / relative
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("", encoding="utf-8")
    return repo


def test_ledger_reset_requires_typing_reset(tmp_path: Path) -> None:
    runner = _RecordingRunner()
    _with_script(tmp_path, ".harness/orchestration/coordinator.py")

    async def scenario() -> list[int]:
        app = _HostApp(HarnessScreen(tmp_path, command_runner=runner))
        calls_after_each_try: list[int] = []
        async with app.run_test(size=(120, 40)) as pilot:
            await _choose(pilot, app, "ledger-reset", HARNESS_COMMANDS)
            await pilot.click("#run")
            await pilot.pause()
            calls_after_each_try.append(len(runner.calls))
            await pilot.click("#typed-confirmation")
            await pilot.press(*"reset")
            await pilot.click("#run")
            await pilot.pause()
            calls_after_each_try.append(len(runner.calls))
            app.screen.query_one("#typed-confirmation", Input).value = ""
            await pilot.click("#typed-confirmation")
            await pilot.press(*"RESET")
            await pilot.click("#run")
            await _settle(pilot, app)
            calls_after_each_try.append(len(runner.calls))
        return calls_after_each_try

    assert asyncio.run(scenario()) == [0, 0, 1]
    assert runner.calls[0][-4:] == ["ledger", "reset", "--confirm", "RESET"]


def test_a_command_with_inputs_asks_for_them_before_running(tmp_path: Path) -> None:
    runner = _RecordingRunner()
    _with_script(tmp_path, "scripts/build_parser_bundle.py")

    async def scenario() -> None:
        app = _HostApp(HarnessScreen(tmp_path, command_runner=runner))
        async with app.run_test(size=(120, 40)) as pilot:
            await _choose(pilot, app, "parser-bundle", HARNESS_COMMANDS)
            assert isinstance(app.screen, CommandFormScreen)
            assert not app.screen.query("#confirmation-reason")
            await pilot.click("#input-wheelhouse")
            await pilot.press(*"wheels")
            await pilot.click("#run")
            await _settle(pilot, app)

    asyncio.run(scenario())
    expected = _entry("parser-bundle").cli_argv(tmp_path, {"wheelhouse": "wheels"})
    assert runner.calls == [process_argv(expected)]


def test_commands_whose_script_is_absent_are_not_offered(tmp_path: Path) -> None:
    """verify and parser-bundle exist only in the canonical harness repository: in a target project
    they would always fail with "can't open file", so the menu hides them."""

    async def scenario() -> list[str]:
        app = _HostApp(HarnessScreen(tmp_path, command_runner=_RecordingRunner()))
        async with app.run_test(size=(120, 40)):
            return [item.name or "" for item in app.screen.query(ListItem)]

    offered = asyncio.run(scenario())
    assert "verify" not in offered and "parser-bundle" not in offered
    assert "diff" in offered and "worktree-remove" in offered


def test_harness_section_shows_installation_state_and_pipeline_usage(
    tmp_path: Path,
) -> None:
    from tests.console._console_ledger_fixture import build_reports_fixture

    build_reports_fixture(tmp_path)

    async def scenario() -> str:
        app = _HostApp(HarnessScreen(tmp_path, command_runner=_RecordingRunner()))
        async with app.run_test(size=(120, 40)):
            return str(app.screen.query_one("#harness-state", Static).content)

    text = asyncio.run(scenario())
    assert "Харнесс не установлен" in text
    assert "Использование пайплайна: запусков 2, тикетов 2" in text
