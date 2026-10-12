"""The Update Harness screen: versions, drift, diff, mode and the confirmed run."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

from textual.app import App
from textual.widgets import Button, ListView, RadioButton, SelectionList, Static

from tests.console._pilot_wait import press, wait_until
from harness.console import stats as console_stats
from harness.console.catalog import seed_update_entry
from harness.console.screens.harness import CommandFormScreen
from harness.console.screens.update_harness import (
    SEED_MODE,
    UpdateHarnessScreen,
    render_drift,
    render_versions,
)

PAYLOAD: dict[str, object] = {
    "state": "drift",
    "package_changed": [".harness/a.py", ".harness/b.py"],
    "added": [".harness/new.py"],
    "missing": [],
    "retired": [],
    "local_changed": [".harness/local.py"],
    "conflict": [".harness/conflict.py"],
    "seed_differing": [".claude/rules/one.md", ".claude/rules/two.md"],
}


def _state(*, installed: bool = True) -> console_stats.HarnessState:
    if not installed:
        return console_stats.HarnessState(False, "", (), 0, 0, "", False)
    return console_stats.HarnessState(
        True, "1.3.2", ("backend-orchestration",), 32, 219, "2026-10-11 11:00 UTC", True
    )


class _Runner:
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
        return subprocess.CompletedProcess(list(argv), 0, "обновлено\n", "")


def _screen(
    tmp_path: Path,
    runner: _Runner,
    *,
    installed: bool = True,
    payload: dict[str, object] | None = None,
) -> UpdateHarnessScreen:
    return UpdateHarnessScreen(
        tmp_path,
        command_runner=runner,
        load_state=lambda _repo: _state(installed=installed),
        load_drift=lambda _repo: PAYLOAD if payload is None else payload,
        load_diff=lambda _repo,
        relative: f"--- проект/{relative}\n+++ после обновления/{relative}\n-old\n+new\n",
    )


def _host(screen: UpdateHarnessScreen) -> App[None]:
    class Host(App[None]):
        def on_mount(self) -> None:
            self.push_screen(screen)

    return Host()


def test_versions_and_drift_are_rendered_with_counters() -> None:
    versions = render_versions(_state(), "1.4.0")
    assert "1.3.2" in versions.plain and "1.4.0" in versions.plain
    assert "Доступно обновление" in versions.plain
    assert "Версия актуальна" in render_versions(_state(), "1.3.2").plain
    assert "не установлен" in render_versions(_state(installed=False), "1.4.0").plain
    drift = render_drift(PAYLOAD)
    assert "обновятся" in drift.plain and "в конфликте" in drift.plain
    assert "Расхождений нет" in render_drift({"state": "clean"}).plain
    assert "Not installed" not in render_drift(None, language="en").plain
    assert "Could not check drift." in render_drift(None, language="en").plain


def test_the_screen_shows_every_frame_and_the_drift_counters(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[str], str, int]:
        app = _host(_screen(tmp_path, _Runner()))
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            await pilot.pause()
            screen = app.screen
            frames = [
                str(frame.border_title).strip()
                for frame in screen.query(".frame")
                if frame.border_title
            ]
            drift = str(screen.query_one("#update-drift", Static).content)
            files = len(screen.query_one("#diff-files", ListView))
            return frames, drift, files

    frames, drift, files = asyncio.run(scenario())
    assert {"Версии", "Режим обновления", "План", "Результат"} <= set(frames)
    assert any(title.startswith("Diff") for title in frames)
    assert "обновятся" in drift
    # Every drift field plus the seed files that differ from the template, once each.
    assert files == 2 + 1 + 1 + 1 + 2


def test_update_asks_for_the_same_confirmation_and_runs_only_after_it(
    tmp_path: Path,
) -> None:
    runner = _Runner()

    async def scenario() -> tuple[bool, str, str]:
        app = _host(_screen(tmp_path, runner))
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            await press(pilot, "#update-run")
            await wait_until(pilot, lambda: isinstance(app.screen, CommandFormScreen))
            assert isinstance(app.screen, CommandFormScreen)
            assert app.screen.entry.key == "update"
            assert not runner.calls
            await pilot.press("escape")
            await wait_until(pilot, lambda: isinstance(app.screen, UpdateHarnessScreen))
            assert isinstance(app.screen, UpdateHarnessScreen)
            assert not runner.calls
            await press(pilot, "#update-run")
            await wait_until(pilot, lambda: isinstance(app.screen, CommandFormScreen))
            await press(pilot, "#run")
            await wait_until(pilot, lambda: isinstance(app.screen, UpdateHarnessScreen))
            await app.workers.wait_for_complete()
            await pilot.pause()
            output = str(app.screen.query_one("#update-output", Static).content)
            return bool(runner.calls), output, type(app.screen).__name__

    ran, output, screen_name = asyncio.run(scenario())
    assert ran and screen_name == "UpdateHarnessScreen"
    assert "код выхода: 0" in output and "обновлено" in output
    assert runner.calls[0][-1] == str(tmp_path) and "update" in runner.calls[0]


def test_nothing_runs_when_harness_is_not_installed(tmp_path: Path) -> None:
    runner = _Runner()

    async def scenario() -> tuple[bool, str]:
        app = _host(_screen(tmp_path, runner, installed=False))
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            return (
                app.screen.query_one("#update-run", Button).disabled,
                str(app.screen.query_one("#update-drift", Static).content),
            )

    disabled, drift = asyncio.run(scenario())
    assert disabled and "сравнивать нечего" in drift
    assert not runner.calls


def test_seed_mode_updates_only_the_ticked_files(tmp_path: Path) -> None:
    runner = _Runner()

    async def scenario() -> None:
        app = _host(_screen(tmp_path, runner))
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, UpdateHarnessScreen)
            assert not screen.query_one("#seed-frame").display
            screen.query_one("#update-mode").query(RadioButton).last(
                RadioButton
            ).value = True
            await pilot.pause()
            assert screen._mode() == SEED_MODE
            assert screen.query_one("#seed-frame").display
            files = screen.query_one("#seed-files", SelectionList)
            assert list(files.selected) == []
            assert screen.query_one("#update-run", Button).disabled
            await press(pilot, "#seed-all")
            await wait_until(
                pilot,
                lambda: sorted(files.selected) == PAYLOAD["seed_differing"],
            )
            assert sorted(files.selected) == PAYLOAD["seed_differing"]
            await press(pilot, "#seed-none")
            await wait_until(pilot, lambda: not files.selected)
            assert not files.selected
            files.select(".claude/rules/two.md")
            await wait_until(
                pilot,
                lambda: not screen.query_one("#update-run", Button).disabled,
            )
            assert not screen.query_one("#update-run", Button).disabled
            await press(pilot, "#update-run")
            await wait_until(pilot, lambda: isinstance(app.screen, CommandFormScreen))
            assert isinstance(app.screen, CommandFormScreen)
            assert app.screen.entry.cli_argv(tmp_path)[3:] == [
                "--force-seed-file",
                ".claude/rules/two.md",
            ]
            await press(pilot, "#run")
            await wait_until(pilot, lambda: isinstance(app.screen, UpdateHarnessScreen))
            await app.workers.wait_for_complete()

    asyncio.run(scenario())
    assert runner.calls
    assert runner.calls[0][-2:] == ["--force-seed-file", ".claude/rules/two.md"]


def test_seed_update_entry_builds_one_flag_per_file(tmp_path: Path) -> None:
    entry = seed_update_entry(["a.md", "b/c.md"])
    assert entry.cli_argv(tmp_path) == [
        "harness",
        "update",
        str(tmp_path),
        "--force-seed-file",
        "a.md",
        "--force-seed-file",
        "b/c.md",
    ]
    assert entry.needs_confirmation


def test_the_diff_block_shows_the_diff_of_the_highlighted_file(tmp_path: Path) -> None:
    async def scenario() -> tuple[str, str]:
        app = _host(_screen(tmp_path, _Runner()))
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            listing = app.screen.query_one("#diff-files", ListView)
            first = listing.highlighted_child
            assert first is not None and first.name
            return first.name, str(app.screen.query_one("#diff-view", Static).content)

    name, view = asyncio.run(scenario())
    assert name == "conflict.py" or name.endswith("conflict.py")
    assert "+new" in view and "-old" in view


def test_the_screen_follows_the_language(tmp_path: Path) -> None:
    async def scenario() -> tuple[str, str, str]:
        app = _host(_screen(tmp_path, _Runner()))
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            app.language = "en"  # type: ignore[attr-defined]
            from harness.console import translate

            translate.retranslate(app.screen, "en")
            await pilot.pause()
            return (
                str(app.screen.query_one("#update-versions", Static).content),
                str(app.screen.query_one("#update-run", Button).label),
                str(app.screen.query_one("#update-drift", Static).content),
            )

    versions, run, drift = asyncio.run(scenario())
    assert "Update available" in versions and "Installed" in versions
    assert run == "Update"
    assert "will be updated" in drift
