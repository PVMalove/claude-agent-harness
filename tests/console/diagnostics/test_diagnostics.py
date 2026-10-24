"""Diagnostics lifecycle and visible interactions at the injected health boundary."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from textual.app import App
from textual.widgets import Button, Static

from harness.console.screens.diagnostics import DiagnosticsScreen
from harness.health.model import CheckResult, Report
from tests.console._pilot_wait import wait_until


def report(message: str = "current", *, online: bool = False) -> Report:
    return Report(
        1, "/project", online, [CheckResult("files.lock", "files", "ok", message)]
    )


class Host(App[None]):
    def __init__(self, screen: DiagnosticsScreen) -> None:
        super().__init__()
        self.diagnostics = screen

    def on_mount(self) -> None:
        self.push_screen(self.diagnostics)


def activate(screen: DiagnosticsScreen, button: str) -> None:
    screen.on_button_pressed(Button.Pressed(screen.query_one(f"#{button}", Button)))


def visible(screen: DiagnosticsScreen) -> str:
    return "\n".join(str(widget.content) for widget in screen.query(Static))


def test_refresh_clears_exportable_report_and_failed_refresh_never_restores_it(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def collect(repo: Path, *, online: bool = False) -> Report:
        nonlocal calls
        calls += 1
        if calls == 1:
            return report("previous success")
        entered.set()
        assert release.wait(5), "test did not release refresh"
        raise ValueError("refresh failed")

    async def scenario() -> tuple[list[Path], str, list[Path], str]:
        screen = DiagnosticsScreen(tmp_path, collect_diagnostics=collect)
        app = Host(screen)
        try:
            async with app.run_test(size=(120, 40)) as pilot:
                await wait_until(pilot, lambda: "previous success" in visible(screen))
                try:
                    activate(screen, "offline-checks")
                    assert await asyncio.to_thread(entered.wait, 5)
                    screen.action_export()
                    during = list(tmp_path.rglob("*health-report.md"))
                    activate(screen, "apply-fixes")
                    fix_label = str(screen.query_one("#apply-fixes", Button).label)
                finally:
                    release.set()
                    await app.workers.wait_for_complete()
                await wait_until(pilot, lambda: "refresh failed" in visible(screen))
                screen.action_export()
                after = list(tmp_path.rglob("*health-report.md"))
                text = visible(screen)
            return during, fix_label, after, text
        finally:
            release.set()

    during, fix_label, after, text = asyncio.run(scenario())
    assert not during
    assert "Подтвердить" not in fix_label
    assert not after
    assert "previous success" not in text


def test_cancelled_checks_serialize_core_calls_and_cannot_publish_stale_results(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    release = threading.Event()
    second = threading.Event()
    mutex = threading.Lock()
    active = 0
    peak = 0
    calls = 0

    def collect(repo: Path, *, online: bool = False) -> Report:
        nonlocal active, peak, calls
        with mutex:
            active += 1
            peak = max(active, peak)
            calls += 1
            number = calls
        try:
            if number == 1:
                started.set()
                assert release.wait(5)
                return report("cancelled result")
            second.set()
            return report("latest result", online=online)
        finally:
            with mutex:
                active -= 1

    async def scenario() -> str:
        screen = DiagnosticsScreen(tmp_path, collect_diagnostics=collect)
        app = Host(screen)
        try:
            async with app.run_test(size=(120, 40)) as pilot:
                assert await asyncio.to_thread(started.wait, 5)
                activate(screen, "online-checks")
                # Give the accepted request a turn; the old core call is still held.
                await pilot.pause()
                await asyncio.to_thread(second.wait, 0.1)
                release.set()
                await wait_until(pilot, lambda: "latest result" in visible(screen))
                await app.workers.wait_for_complete()
                return visible(screen)
        finally:
            release.set()

    text = asyncio.run(scenario())
    assert peak == 1
    assert "latest result" in text
    assert "cancelled result" not in text


def test_export_resets_fix_confirmation(tmp_path: Path) -> None:
    calls: list[bool] = []

    def fix(repo: Path, *, online: bool = False) -> Report:
        calls.append(online)
        return report()

    async def scenario() -> None:
        screen = DiagnosticsScreen(
            tmp_path,
            collect_diagnostics=lambda repo, **kw: report(),
            apply_local_fixes=fix,
        )
        app = Host(screen)
        async with app.run_test(size=(160, 52)) as pilot:
            await wait_until(pilot, lambda: "current" in visible(screen))
            activate(screen, "apply-fixes")
            screen.action_export()
            activate(screen, "apply-fixes")
            assert not calls
            activate(screen, "apply-fixes")
            await wait_until(pilot, lambda: len(calls) == 1)

    asyncio.run(scenario())


def test_programmatic_navigation_resets_confirmation_even_for_current_section(
    tmp_path: Path,
) -> None:
    async def scenario() -> str:
        screen = DiagnosticsScreen(
            tmp_path, collect_diagnostics=lambda repo, **kw: report()
        )
        app = Host(screen)
        async with app.run_test(size=(160, 52)) as pilot:
            await wait_until(pilot, lambda: "current" in visible(screen))
            activate(screen, "apply-fixes")
            await screen.action_section(3)
            return str(screen.query_one("#apply-fixes", Button).label)

    assert asyncio.run(scenario()) == "Исправить"


@pytest.mark.parametrize(
    "old_fix,new_screen,old_error",
    [
        (False, False, True),
        (False, True, False),
        (True, False, False),
        (True, True, True),
    ],
)
def test_replaced_or_unmounted_runs_finish_before_the_next_core_call(
    tmp_path: Path, old_fix: bool, new_screen: bool, old_error: bool
) -> None:
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    active = 0
    peak = 0
    mutex = threading.Lock()
    ready = False

    def core(repo: Path, *, online: bool = False) -> Report:
        nonlocal active, peak, ready
        if old_fix and not ready:
            ready = True
            return report("ready for fix", online=True)
        with mutex:
            active += 1
            peak = max(peak, active)
        try:
            if not entered.is_set():
                entered.set()
                assert release.wait(5)
                if old_error:
                    raise ValueError("obsolete error")
                return report("obsolete success")
            assert finished.is_set()
            return report("replacement result", online=online)
        finally:
            with mutex:
                active -= 1
            finished.set()

    async def scenario() -> str:
        screen = DiagnosticsScreen(
            tmp_path, collect_diagnostics=core, apply_local_fixes=core
        )
        app = Host(screen)
        try:
            async with app.run_test(size=(160, 52)) as pilot:
                if old_fix:
                    await wait_until(pilot, lambda: "ready for fix" in visible(screen))
                    activate(screen, "apply-fixes")
                    activate(screen, "apply-fixes")
                assert await asyncio.to_thread(entered.wait, 5)
                if new_screen:
                    app.pop_screen()
                    screen = DiagnosticsScreen(tmp_path, collect_diagnostics=core)
                    await app.push_screen(screen)
                else:
                    activate(screen, "online-checks")
                # Fix cannot be confirmed while another request is building.
                activate(screen, "apply-fixes")
                assert "Подтвердить" not in str(
                    screen.query_one("#apply-fixes", Button).label
                )
                release.set()
                await wait_until(pilot, lambda: "replacement result" in visible(screen))
                return visible(screen)
        finally:
            release.set()

    text = asyncio.run(scenario())
    assert peak == 1
    assert "obsolete" not in text


@pytest.mark.parametrize("key", ["tab", "f2", "f1", "ctrl+p", "3", "down"])
def test_an_intervening_key_resets_confirmation(tmp_path: Path, key: str) -> None:
    async def scenario() -> str:
        screen = DiagnosticsScreen(
            tmp_path, collect_diagnostics=lambda repo, **kw: report()
        )
        app = Host(screen)
        async with app.run_test(size=(160, 52)) as pilot:
            await wait_until(pilot, lambda: "current" in visible(screen))
            activate(screen, "apply-fixes")
            await pilot.press(key)
            return str(screen.query_one("#apply-fixes", Button).label)

    assert asyncio.run(scenario()) == "Исправить"
