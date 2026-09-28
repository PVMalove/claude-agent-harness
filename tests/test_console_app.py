"""harness.console.app: the textual TUI. Skipped entirely when textual is not installed - this
module is the seam DoD calls out: "Pilot tests use a recording runner and are skipped when
textual is unavailable". Tests drive the App with `asyncio.run` rather than pytest-asyncio, since
the harness dev dependency group carries no async pytest plugin."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Sequence

import pytest

pytest.importorskip("textual")

from textual.pilot import Pilot

from harness.console.app import HarnessConsoleApp
from harness.console.data import DashboardData
from harness.console.screens.dashboard import SECTIONS, DashboardScreen
from harness.console.screens.diagnostics import DiagnosticsScreen
from harness.console.screens.harness import HarnessScreen
from harness.health.model import CheckResult, Fix, Report


def test_dashboard_shows_the_section_menu_in_order(tmp_path: Path) -> None:
    async def scenario() -> list[str | None]:
        from textual.widgets import ListView

        app = HarnessConsoleApp(tmp_path)
        async with app.run_test():
            menu = app.screen.query_one("#section-menu", ListView)
            return [item.name for item in menu.children]

    assert asyncio.run(scenario()) == list(SECTIONS)


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


def test_selecting_orchestration_pushes_the_orchestration_screen(tmp_path: Path) -> None:
    async def scenario() -> bool:
        from textual.widgets import ListView

        from harness.console.screens.orchestration import OrchestrationScreen

        app = HarnessConsoleApp(tmp_path)
        async with app.run_test() as pilot:
            menu = app.screen.query_one("#section-menu", ListView)
            menu.index = SECTIONS.index("Orchestration")
            await pilot.press("enter")
            await pilot.pause()
            return isinstance(app.screen, OrchestrationScreen)

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

        screen = DashboardScreen(tmp_path, collect_dashboard=lambda _repo, *, online=False: _fake_dashboard_data())

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


async def _settle(pilot: "Pilot[None]", delay: float | None = None) -> None:
    """Let a click land, then wait for the screen's worker thread (health runs off the UI thread)."""
    await pilot.pause(delay)
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


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
        async with app.run_test() as pilot:
            await _settle(pilot)
            report_widget = app.screen.query_one("#diagnostics-report", Static)
            return str(report_widget.content)

    rendered = asyncio.run(scenario())
    assert "environment.uv" in rendered
    assert "uv недоступен" in rendered
    assert "поставить uv" in rendered
    assert "echo installing-uv" in rendered


class _RecordingFixes:
    """Test double for `apply_local_fixes`: records each call instead of touching the repo."""

    def __init__(self, fixes_applied: list[str] | None = None) -> None:
        self.calls: list[bool] = []
        self._fixes_applied = fixes_applied

    def __call__(self, repo: Path, *, online: bool = False) -> Report:
        self.calls.append(online)
        return _fake_report(online=online, fixes_applied=self._fixes_applied)


def test_diagnostics_apply_fixes_asks_before_applying(tmp_path: Path) -> None:
    """DoD/architect seed: apply-fixes must not change anything on the first press - only a
    second, confirming press applies the `harness health --fix` fixers."""

    async def scenario() -> tuple[int, int]:
        from textual.app import App

        fixes = _RecordingFixes()
        screen = DiagnosticsScreen(
            tmp_path,
            collect_diagnostics=lambda repo, *, online=False: _fake_report(),
            apply_local_fixes=fixes,
        )

        class _HostApp(App[None]):
            def on_mount(self) -> None:
                self.push_screen(screen)

        app = _HostApp()
        async with app.run_test() as pilot:
            await _settle(pilot)
            await pilot.click("#apply-fixes")
            # Button's own brief "-active" press animation ignores a second click while it runs.
            await _settle(pilot, 0.4)
            calls_after_first_press = len(fixes.calls)

            await pilot.click("#apply-fixes")
            await _settle(pilot, 0.4)
            calls_after_second_press = len(fixes.calls)

        return calls_after_first_press, calls_after_second_press

    after_first, after_second = asyncio.run(scenario())
    assert after_first == 0
    assert after_second == 1


def test_diagnostics_apply_fixes_never_runs_remedy_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Epic #341 out of scope: a check's `fix.command` (global git config, Windows registry, data
    removal) is only shown, never executed - "apply fixes" is exactly `harness health --fix`."""

    async def scenario() -> tuple[str, list[list[str]]]:
        from textual.app import App
        from textual.widgets import Static

        spawned: list[list[str]] = []

        def recording_run(argv: "Sequence[str]", *args: object, **kwargs: object) -> object:
            spawned.append(list(argv))
            return subprocess.CompletedProcess(list(argv), 0, "", "")

        monkeypatch.setattr(subprocess, "run", recording_run)
        screen = DiagnosticsScreen(
            tmp_path,
            collect_diagnostics=lambda repo, *, online=False: _fake_report(),
            apply_local_fixes=_RecordingFixes(fixes_applied=["создан каталог .harness"]),
        )

        class _HostApp(App[None]):
            def on_mount(self) -> None:
                self.push_screen(screen)

        app = _HostApp()
        async with app.run_test() as pilot:
            await _settle(pilot)
            await pilot.click("#apply-fixes")
            await _settle(pilot, 0.4)
            await pilot.click("#apply-fixes")
            await _settle(pilot, 0.4)
            report_widget = app.screen.query_one("#diagnostics-report", Static)
            return str(report_widget.content), spawned

    rendered, calls = asyncio.run(scenario())
    assert calls == []
    assert "создан каталог .harness" in rendered
    # the remedy command stays visible for the developer to run by hand
    assert "echo installing-uv" in rendered


def test_diagnostics_apply_fixes_keeps_online_mode(tmp_path: Path) -> None:
    """After "online checks", apply-fixes runs `--online --fix` (label creation) - still only
    after the confirming press."""

    async def scenario() -> list[bool]:
        from textual.app import App

        fixes = _RecordingFixes()
        screen = DiagnosticsScreen(
            tmp_path,
            collect_diagnostics=lambda repo, *, online=False: _fake_report(online=online),
            apply_local_fixes=fixes,
        )

        class _HostApp(App[None]):
            def on_mount(self) -> None:
                self.push_screen(screen)

        app = _HostApp()
        async with app.run_test() as pilot:
            await _settle(pilot)
            await pilot.click("#online-checks")
            await _settle(pilot, 0.4)
            await pilot.click("#apply-fixes")
            await _settle(pilot, 0.4)
            await pilot.click("#apply-fixes")
            await _settle(pilot, 0.4)
        return fixes.calls

    assert asyncio.run(scenario()) == [True]


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
            await _settle(pilot)
            await pilot.click("#online-checks")
            await _settle(pilot)

        return seen_online

    seen_online = asyncio.run(scenario())
    assert seen_online == [False, True]


def test_dashboard_online_checks_action_reruns_with_online_and_shows_the_cli(
    tmp_path: Path,
) -> None:
    """Story 42: online checks run from the dashboard with one action; story 51: its CLI
    equivalent is shown next to it."""

    async def scenario() -> tuple[list[bool], str, str]:
        from textual.app import App
        from textual.widgets import Static

        seen_online: list[bool] = []

        def fake_collect(_repo: Path, *, online: bool = False) -> DashboardData:
            seen_online.append(online)
            return _fake_dashboard_data()

        class _HostApp(App[None]):
            def on_mount(self) -> None:
                self.push_screen(DashboardScreen(tmp_path, collect_dashboard=fake_collect))

        app = _HostApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.click("#dashboard-online")
            await _settle(pilot)
            summary = str(app.screen.query_one("#dashboard-summary", Static).content)
            cli = str(app.screen.query_one("#dashboard-online-cli", Static).content)
        return seen_online, summary, cli

    seen_online, summary, cli = asyncio.run(scenario())
    assert seen_online == [False, True]
    assert "health (online)" in summary
    assert "harness health" in cli and "--online" in cli


def test_diagnostics_shows_cli_equivalents_and_exports_the_report(tmp_path: Path) -> None:
    """Story 51: every action shows its CLI equivalent; story 57: the health report exports to a
    dated Markdown file in the common console-exports folder under docs/tasks/."""

    async def scenario() -> tuple[str, str]:
        from textual.app import App
        from textual.widgets import Static

        screen = DiagnosticsScreen(
            tmp_path, collect_diagnostics=lambda repo, *, online=False: _fake_report()
        )

        class _HostApp(App[None]):
            def on_mount(self) -> None:
                self.push_screen(screen)

        app = _HostApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            online_cli = str(app.screen.query_one("#online-checks-cli", Static).content)
            fix_cli = str(app.screen.query_one("#apply-fixes-cli", Static).content)
            await pilot.click("#export")
            await _settle(pilot)
        return online_cli, fix_cli

    online_cli, fix_cli = asyncio.run(scenario())
    assert online_cli.endswith("--online")
    assert fix_cli.endswith("--fix")
    exported = list((tmp_path / "docs" / "tasks" / "console-exports").glob("*health-report.md"))
    assert len(exported) == 1
    text = exported[0].read_text(encoding="utf-8")
    assert text.startswith("# Health-отчёт")
    assert "environment.uv" in text
