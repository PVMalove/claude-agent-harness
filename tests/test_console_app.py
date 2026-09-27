"""harness.console.app: the textual TUI. Skipped entirely when textual is not installed - this
module is the seam DoD calls out: "Pilot tests use a recording runner and are skipped when
textual is unavailable". Tests drive the App with `asyncio.run` rather than pytest-asyncio, since
the harness dev dependency group carries no async pytest plugin."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

import pytest

pytest.importorskip("textual")

from harness.console.app import HarnessConsoleApp
from harness.console.data import DashboardData
from harness.console.screens.dashboard import SECTIONS, DashboardScreen
from harness.console.screens.diagnostics import DiagnosticsScreen
from harness.console.screens.harness import HarnessScreen
from harness.console.screens.stub import StubScreen
from harness.health.model import CheckResult, Fix, Report


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
            menu.index = SECTIONS.index("Orchestration")
            await pilot.press("enter")
            await pilot.pause()
            return isinstance(app.screen, StubScreen), getattr(
                app.screen, "section_name", ""
            )

    is_stub, section_name = asyncio.run(scenario())
    assert is_stub
    assert section_name == "Orchestration"


def test_selecting_harness_pushes_the_harness_screen(tmp_path: Path) -> None:
    async def scenario() -> bool:
        from textual.widgets import ListView

        app = HarnessConsoleApp(tmp_path)
        async with app.run_test() as pilot:
            menu = app.screen.query_one("#section-menu", ListView)
            menu.index = SECTIONS.index("Harness")
            await pilot.press("enter")
            await pilot.pause()
            return isinstance(app.screen, HarnessScreen)

    assert asyncio.run(scenario())


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


class _RecordingRunner:
    """Test double for harness.console.runner.CommandRunner: records every call instead of
    spawning a real process, so a Pilot test can drive "apply fixes" safely."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self,
        argv: "Sequence[str]",
        *,
        env: "Mapping[str, str] | None" = None,
        cwd: Path | None = None,
    ) -> "subprocess.CompletedProcess[str]":
        recorded = list(argv)
        self.calls.append(recorded)
        return subprocess.CompletedProcess(recorded, 0, "", "")


def _fake_report(*, online: bool = False, fixes_applied: list[str] | None = None) -> Report:
    return Report(
        schema_version=1,
        repo="/tmp/fake-repo",
        online=online,
        checks=[
            CheckResult(id="files.lock", group="files", status="ok", message="lock ok"),
            CheckResult(
                id="environment.uv",
                group="environment",
                status="fail",
                message="uv недоступен",
                fix=Fix(text="поставить uv", command="echo installing-uv"),
            ),
        ],
        fixes_applied=fixes_applied or [],
    )


def test_diagnostics_screen_renders_the_full_report(tmp_path: Path) -> None:
    async def scenario() -> str:
        from textual.app import App
        from textual.widgets import Static

        screen = DiagnosticsScreen(
            tmp_path, collect_diagnostics=lambda repo, *, online=False: _fake_report()
        )

        class _HostApp(App[None]):
            def on_mount(self) -> None:
                self.push_screen(screen)

        app = _HostApp()
        async with app.run_test():
            report_widget = app.screen.query_one("#diagnostics-report", Static)
            return str(report_widget.content)

    rendered = asyncio.run(scenario())
    assert "environment.uv" in rendered
    assert "uv недоступен" in rendered
    assert "поставить uv" in rendered
    assert "echo installing-uv" in rendered


def test_diagnostics_apply_fixes_asks_before_running_a_destructive_fix(
    tmp_path: Path,
) -> None:
    """DoD/architect seed: apply-fixes must not run anything on the first press - only a second,
    confirming press invokes the injected CommandRunner."""

    async def scenario() -> tuple[int, int]:
        from textual.app import App
        from textual.widgets import Button

        runner = _RecordingRunner()
        screen = DiagnosticsScreen(
            tmp_path,
            collect_diagnostics=lambda repo, *, online=False: _fake_report(),
            apply_local_fixes=lambda repo, *, online=False: _fake_report(),
            command_runner=runner,
        )

        class _HostApp(App[None]):
            def on_mount(self) -> None:
                self.push_screen(screen)

        app = _HostApp()
        async with app.run_test() as pilot:
            await pilot.click("#apply-fixes")
            # Button's own brief "-active" press animation ignores a second click while it runs.
            await pilot.pause(0.4)
            calls_after_first_press = len(runner.calls)

            await pilot.click("#apply-fixes")
            await pilot.pause(0.4)
            calls_after_second_press = len(runner.calls)

        return calls_after_first_press, calls_after_second_press

    after_first, after_second = asyncio.run(scenario())
    assert after_first == 0
    assert after_second == 1


def test_diagnostics_apply_fixes_merges_shell_and_local_fixers(tmp_path: Path) -> None:
    """Apply fixes both runs each check's shell `command` (via the injected CommandRunner) and
    calls `apply_local_fixes` - the in-process `FIXERS` half of `harness health --fix` (#399), e.g.
    creating a missing `.harness` directory - and shows both in `fixes_applied`."""

    async def scenario() -> str:
        from textual.app import App
        from textual.widgets import Static

        runner = _RecordingRunner()
        screen = DiagnosticsScreen(
            tmp_path,
            collect_diagnostics=lambda repo, *, online=False: _fake_report(),
            apply_local_fixes=lambda repo, *, online=False: _fake_report(
                fixes_applied=["создан каталог .harness"]
            ),
            command_runner=runner,
        )

        class _HostApp(App[None]):
            def on_mount(self) -> None:
                self.push_screen(screen)

        app = _HostApp()
        async with app.run_test() as pilot:
            await pilot.click("#apply-fixes")
            await pilot.pause(0.4)
            await pilot.click("#apply-fixes")
            await pilot.pause(0.4)
            report_widget = app.screen.query_one("#diagnostics-report", Static)
            return str(report_widget.content)

    rendered = asyncio.run(scenario())
    assert "echo installing-uv" in rendered
    assert "создан каталог .harness" in rendered


class _FailingRunner:
    """Test double for CommandRunner: every call fails, so a Pilot test can prove a failed shell
    remedy is neither recorded as applied nor silently dropped."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self,
        argv: "Sequence[str]",
        *,
        env: "Mapping[str, str] | None" = None,
        cwd: Path | None = None,
    ) -> "subprocess.CompletedProcess[str]":
        recorded = list(argv)
        self.calls.append(recorded)
        return subprocess.CompletedProcess(recorded, 1, "", "boom: uv install failed\n")


def test_diagnostics_apply_fixes_reports_a_failed_shell_fix_instead_of_hiding_it(
    tmp_path: Path,
) -> None:
    """Standards fix: a shell remedy that exits non-zero must not land in `fixes_applied`, and
    must be surfaced to the operator with its command and a short stderr tail."""

    async def scenario() -> str:
        from textual.app import App
        from textual.widgets import Static

        runner = _FailingRunner()
        screen = DiagnosticsScreen(
            tmp_path,
            collect_diagnostics=lambda repo, *, online=False: _fake_report(),
            apply_local_fixes=lambda repo, *, online=False: _fake_report(),
            command_runner=runner,
        )

        class _HostApp(App[None]):
            def on_mount(self) -> None:
                self.push_screen(screen)

        app = _HostApp()
        async with app.run_test() as pilot:
            await pilot.click("#apply-fixes")
            await pilot.pause(0.4)
            await pilot.click("#apply-fixes")
            await pilot.pause(0.4)
            report_widget = app.screen.query_one("#diagnostics-report", Static)
            return str(report_widget.content)

    rendered = asyncio.run(scenario())
    assert "Применённые фиксы" not in rendered
    assert "Не удалось применить" in rendered
    assert "echo installing-uv" in rendered
    assert "boom: uv install failed" in rendered


def test_diagnostics_online_checks_refetches_with_online_true(tmp_path: Path) -> None:
    async def scenario() -> list[bool]:
        from textual.app import App

        seen_online: list[bool] = []

        def fake_collect(repo: Path, *, online: bool = False) -> Report:
            seen_online.append(online)
            return _fake_report(online=online)

        screen = DiagnosticsScreen(tmp_path, collect_diagnostics=fake_collect)

        class _HostApp(App[None]):
            def on_mount(self) -> None:
                self.push_screen(screen)

        app = _HostApp()
        async with app.run_test() as pilot:
            await pilot.click("#online-checks")
            await pilot.pause()

        return seen_online

    seen_online = asyncio.run(scenario())
    assert seen_online == [False, True]
