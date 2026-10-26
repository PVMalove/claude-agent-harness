"""Visible Harness acceptance, independent of SVG files stored outside Git."""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from pathlib import Path
from threading import Event
from typing import cast

import pytest
from textual.screen import Screen
from textual.widgets import Button, Input, ListView, Static

from harness.console.screens.harness import CommandFormScreen, HarnessScreen
from harness.console.shell import Fold
from tests.console._svg_snapshot import SIZES, mockup_states, until
from tests.console.harness.snapshots import (
    EXCLUDED,
    LANGUAGES,
    MOCKUP_STATES,
    STATES,
    EvidenceApp,
    Runner,
    capture,
)
from tests.console.test_console_harness import (
    _BlockingRunner,
    _HostApp,
    _RecordingRunner,
    _settle,
)


def test_matrix_accounts_for_every_mockup_state_without_fake_captures() -> None:
    assert set(MOCKUP_STATES.values()) | {entry["state"] for entry in EXCLUDED} >= set(
        mockup_states("harness.html")
    )
    assert len(set(STATES)) == len(STATES)
    assert all(entry["follow_up"] and entry["reason"] for entry in EXCLUDED)


@pytest.mark.parametrize("state", STATES)
@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("language", LANGUAGES)
def test_visible_state_and_svg(
    state: str,
    size: tuple[int, int],
    language: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    svg, terminal = asyncio.run(
        capture(state, size, language, tmp_path / "claude-agent-harness")
    )
    text = " ".join(ET.fromstring(svg).itertext()).replace("\u00a0", " ")
    assert "harness.html" in text and f"{size[0]}x{size[1]}" in text
    assert "Harness" in terminal
    if state == "baseline":
        assert "#d97757" in svg.lower()
        assert ("Команды" if language == "ru" else "Commands").upper() in terminal
        assert "20" in terminal and "INSTALL & UPDATE" not in terminal
        if language == "en" and size[0] == 160:
            assert "Select" in terminal and "Выбор" not in terminal
        assert (
            ("↑↓" in terminal and "f2" in terminal)
            if size[0] == 160
            else "f2" not in terminal
        )
    elif state == "running":
        assert ("Выполняется…" if language == "ru" else "Running…") in terminal
    elif state in ("exit-zero", "exit-nonzero"):
        assert ("exit 0" if state == "exit-zero" else "exit 7") in terminal
    elif state == "ledger-unavailable":
        assert "fixture ledger locked" in terminal
    elif state == "version-mismatch":
        assert "0.0.1" in terminal and "⚠" in terminal
    elif state == "filter-empty":
        assert (
            "Нет доступных команд" if language == "ru" else "No commands available"
        ) in terminal
    elif state == "unavailable-command":
        assert (
            "Команда недоступна" if language == "ru" else "Command is unavailable"
        ) in terminal
    elif state.startswith("worktree-"):
        assert "worktree" in terminal
    elif state.startswith("reset-"):
        assert "RESET" in terminal
    elif state.startswith("hard") or state.startswith("uninstall"):
        assert ("HARD" if state.startswith("hard") else "UNINSTALL") in terminal
    elif state == "drift-loading":
        assert ("проверяется" if language == "ru" else "checking") in terminal
    elif state == "drift-unknown":
        assert ("неизвестен" if language == "ru" else "unknown") in terminal


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("language", LANGUAGES)
def test_busy_enter_and_run_explain_rejection_and_never_replace_result(
    tmp_path: Path, size: tuple[int, int], language: str
) -> None:
    runner = _BlockingRunner()

    async def scenario() -> None:
        screen = HarnessScreen(
            tmp_path, command_runner=runner, load_drift=lambda _: "clean"
        )
        app = _HostApp(screen)
        app.language = language
        async with app.run_test(size=size) as pilot:
            try:
                screen.open_command("diff")
                await until(pilot, runner.started.is_set)
                screen.query_one("#command-menu", ListView).focus()
                await pilot.press("enter")
                await pilot.pause()
                message = "Дождитесь" if language == "ru" else "Wait for"
                assert message in str(screen.query_one("#run-feedback", Static).content)
                screen.query_one("#detail-fold", Fold).collapsed = False
                await pilot.pause()
                await pilot.click("#command-run")
                await pilot.pause()
                assert message in str(screen.query_one("#run-feedback", Static).content)
                assert len(runner.calls) == 1
                status = screen.query_one("#run-status", Static)
                assert status.region.bottom < size[1]
                runner.release.set()
                await _settle(pilot, app)
                await pilot.pause(0.2)
                assert "exit 7" in str(status.content)
                assert len(runner.calls) == 1
            finally:
                runner.release.set()

    asyncio.run(scenario())


@pytest.mark.parametrize("size", SIZES)
def test_retry_uses_result_command_and_requires_the_typed_word_again(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    runner = Runner("exit-nonzero")

    async def scenario() -> None:
        app = _HostApp(
            HarnessScreen(tmp_path, command_runner=runner, load_drift=lambda _: "clean")
        )
        async with app.run_test(size=size) as pilot:
            screen = app._harness_screen
            screen.open_command("cleanup-hard-apply")
            await pilot.pause()
            app.screen.query_one("#typed-confirmation", Input).value = "HARD"
            await pilot.pause()
            await pilot.click("#run")
            await _settle(pilot, app)
            await pilot.pause(0.2)
            screen.select_command("diff")
            await pilot.pause()
            retry = screen.query_one("#command-retry", Button)
            assert retry.region.bottom < size[1]
            await pilot.click(retry)
            await pilot.pause()
            assert isinstance(app.screen, CommandFormScreen)
            assert app.screen.entry.key == "cleanup-hard-apply"
            assert app.screen.query_one("#typed-confirmation", Input).value == ""
            assert app.screen.query_one("#run", Button).disabled
            assert len(runner.calls) == 1
            await pilot.click("#cancel")

    asyncio.run(scenario())


def test_unmount_never_cancels_another_application_worker(tmp_path: Path) -> None:
    started, release = Event(), Event()

    async def scenario() -> None:
        app = _HostApp(
            HarnessScreen(
                tmp_path,
                command_runner=_RecordingRunner(),
                load_drift=lambda _: "clean",
            )
        )
        async with app.run_test(size=(80, 24)) as pilot:

            def other_work() -> None:
                started.set()
                release.wait(5)

            worker = app.run_worker(other_work, thread=True, group="other-screen")
            try:
                await until(pilot, started.is_set)
                await app.pop_screen()
                await pilot.pause()
                assert not worker.is_cancelled
            finally:
                release.set()
                await app.workers.wait_for_complete()

    asyncio.run(scenario())


def test_full_output_survives_selection_filter_collapse_language_and_reopen(
    tmp_path: Path,
) -> None:
    runner = Runner("exit-zero")

    async def scenario() -> None:
        app = EvidenceApp(
            tmp_path,
            lambda repo: cast(
                Screen[object],
                HarnessScreen(
                    repo, command_runner=runner, load_drift=lambda _: "clean"
                ),
            ),
        )
        async with app.run_test(size=(80, 24)) as pilot:
            screen = app.screen
            assert isinstance(screen, HarnessScreen)
            screen.open_command("diff")
            await _settle(pilot, app)
            await pilot.pause(0.2)
            original = str(screen.query_one("#command-output", Static).content)
            assert all(
                marker in original
                for marker in (
                    "STDOUT-FIRST",
                    "STDOUT-LAST",
                    "STDERR-FIRST",
                    "STDERR-LAST",
                )
            )
            screen.query_one("#output-fold", Fold).collapsed = True
            screen.select_command("health")
            await pilot.click("#filter-confirmation")
            await pilot.press("f2")
            await _settle(pilot, app)
            assert str(screen.query_one("#command-output", Static).content) == original
            await app.pop_screen()
            replacement = HarnessScreen(
                tmp_path, command_runner=runner, load_drift=lambda _: "clean"
            )
            await app.push_screen(replacement)
            await _settle(pilot, app)
            assert (
                str(replacement.query_one("#command-output", Static).content)
                == original
            )

    asyncio.run(scenario())


def test_late_drift_result_ignores_unmounted_receiver(tmp_path: Path) -> None:
    started, release = Event(), Event()

    def load(_: Path) -> str:
        started.set()
        release.wait(5)
        return "drift"

    async def scenario() -> None:
        old = HarnessScreen(tmp_path, load_drift=load)
        app = _HostApp(old)
        async with app.run_test(size=(80, 24)) as pilot:
            try:
                await until(pilot, started.is_set)
                await app.pop_screen()
                current = HarnessScreen(tmp_path, load_drift=lambda _: "clean")
                await app.push_screen(current)
                release.set()
                await _settle(pilot, app)
                assert current._drift == "clean" and old._drift == "loading"
            finally:
                release.set()

    asyncio.run(scenario())


def test_confirmation_language_changes_keep_label_error_and_run_validity(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app = EvidenceApp(
            tmp_path,
            lambda repo: cast(
                Screen[object], HarnessScreen(repo, load_drift=lambda _: "clean")
            ),
        )
        async with app.run_test(size=(80, 24)) as pilot:
            screen = app.screen
            assert isinstance(screen, HarnessScreen)
            screen.open_command("cleanup-hard-apply")
            await pilot.pause()
            assert app.screen.query_one("#run", Button).disabled
            await pilot.press("f2")
            await pilot.pause()
            assert "To run, enter HARD" in str(
                app.screen.query_one("#form-error", Static).content
            )
            await pilot.press("f2")
            await pilot.pause()
            assert "Для запуска введите HARD" in str(
                app.screen.query_one("#form-error", Static).content
            )
            app.screen.query_one("#typed-confirmation", Input).value = "HARD"
            await pilot.pause()
            assert not app.screen.query_one("#run", Button).disabled
            await pilot.click("#cancel")

    asyncio.run(scenario())
