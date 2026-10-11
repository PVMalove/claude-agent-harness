"""Pilot scenarios exercise explicit project selection and cancellation."""

from __future__ import annotations

import asyncio
import subprocess

import pytest
from pathlib import Path
from typing import Mapping, Sequence

from textual.widgets import Input, RadioButton, RadioSet, Static

from harness.console.app import HarnessConsoleApp
from harness.console.screens.dashboard import DashboardScreen
from harness.console.screens.project_setup import ProjectSetupScreen

from tests.console._pilot_wait import press, wait_until


def test_selecting_another_project_replaces_the_dashboard_context(
    tmp_path: Path,
) -> None:
    second = tmp_path / "backend"
    second.mkdir()
    subprocess.run(["git", "init", "-q", str(second)], check=True)

    async def scenario() -> None:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            await press(pilot, "#open-project")
            await wait_until(pilot, lambda: isinstance(app.screen, ProjectSetupScreen))
            app.screen.query_one("#project-path", Input).value = str(second)
            await press(pilot, "#inspect-project")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await press(pilot, "#activate-project")
            await wait_until(
                pilot,
                lambda: isinstance(app.screen, DashboardScreen)
                and app.screen.query("HeaderTitle"),
            )
            assert isinstance(app.screen, DashboardScreen)
            assert app.repo == second.resolve()
            assert app.screen.repo == second.resolve()

    asyncio.run(scenario())
    assert not (second / ".harness").exists()


def test_install_runs_the_distribution_in_target_and_activates_it(
    tmp_path: Path,
) -> None:
    second = tmp_path / "backend with spaces"
    second.mkdir()
    subprocess.run(["git", "init", "-q", str(second)], check=True)
    seen: list[tuple[list[str], Path | None]] = []

    def runner(
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        seen.append((list(argv), cwd))
        return subprocess.run(
            list(argv),
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )

    async def scenario() -> None:
        app = HarnessConsoleApp(tmp_path, command_runner=runner)
        async with app.run_test(size=(140, 60)) as pilot:
            await press(pilot, "#open-project")
            await wait_until(pilot, lambda: isinstance(app.screen, ProjectSetupScreen))
            app.screen.query_one("#project-path", Input).value = str(second)
            await press(pilot, "#inspect-project")
            await app.workers.wait_for_complete()
            await pilot.pause()
            options = app.screen.query_one("#capability", RadioSet)
            options.query(RadioButton)[-1].value = True
            await pilot.pause()
            await press(pilot, "#install-project")
            await pilot.pause()
            assert seen == []
            # The Harness section's confirmation form: nothing runs until it is accepted.
            await press(pilot, "#cancel")
            await pilot.pause()
            assert seen == []
            await press(pilot, "#install-project")
            await pilot.pause()
            await press(pilot, "#run")
            await app.workers.wait_for_complete()
            await pilot.pause()
            output = str(app.screen.query_one("#setup-output", Static).render())
            assert len(seen) == 1, (seen, output)
            # The flow continues: the project becomes the target only on completion.
            assert isinstance(app.screen, ProjectSetupScreen)
            assert app.repo == tmp_path
            await press(pilot, "#activate-project")
            await wait_until(
                pilot,
                lambda: isinstance(app.screen, DashboardScreen)
                and app.screen.query("HeaderTitle"),
            )
            assert app.repo == second.resolve()
            assert isinstance(app.screen, DashboardScreen)

    asyncio.run(scenario())
    assert len(seen) == 1 and seen[0][1] == second
    assert str(second) in seen[0][0]
    assert (second / ".harness/orchestration/roles").is_dir()
    assert (second / ".harness/project.json").is_file()
    assert not (tmp_path / ".harness").exists()


def test_installed_capability_stays_selected_and_the_flow_shows_its_stages(
    tmp_path: Path,
) -> None:
    import json

    second = tmp_path / "backend"
    (second / ".harness").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(second)], check=True)
    (second / ".harness/harness.lock").write_text(
        json.dumps(
            {"capabilities": ["pvmalove-suite", "backend-orchestration"], "files": {}}
        ),
        encoding="utf-8",
    )

    async def scenario() -> None:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            await press(pilot, "#open-project")
            await wait_until(pilot, lambda: isinstance(app.screen, ProjectSetupScreen))
            screen = app.screen
            assert "Выбор проекта" in str(
                screen.query_one("#setup-stage", Static).render()
            )
            screen.query_one("#project-path", Input).value = str(second)
            await press(pilot, "#inspect-project")
            await app.workers.wait_for_complete()
            await pilot.pause()
            pressed = screen.query_one("#capability", RadioSet).pressed_index
            assert screen.options[pressed][0] == "backend-orchestration"  # type: ignore[attr-defined]
            stage = str(screen.query_one("#setup-stage", Static).render())
            assert "[Предварительные проверки]" in stage
            plan = str(screen.query_one("#installation-plan", Static).render())
            assert "Состав:" in plan
            await press(pilot, "#browse-project")
            await pilot.pause()
            assert len(screen.query("#directory-browser")) == 1

    asyncio.run(scenario())


def test_late_results_of_the_previous_project_do_not_reach_the_new_dashboard(
    tmp_path: Path,
) -> None:
    import threading

    from textual.app import App

    from harness.console.data import DashboardData
    from tests.console.test_console_app import _fake_dashboard_data

    release = threading.Event()
    first, second = tmp_path / "first", tmp_path / "second"

    def slow_first(repo: Path, *, online: bool = False) -> DashboardData:
        release.wait(10)
        return _fake_dashboard_data()

    def marked_second(repo: Path, *, online: bool = False) -> DashboardData:
        return DashboardData(1, 0, 0, 0, "full", "second-project-marker", "clean", None)

    class Host(App[None]):
        def on_mount(self) -> None:
            self.push_screen(DashboardScreen(first, collect_dashboard=slow_first))

    async def scenario() -> str:
        app = Host()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.switch_screen(DashboardScreen(second, collect_dashboard=marked_second))
            await pilot.pause()
            release.set()
            await pilot.pause(0.5)
            return str(app.screen.query_one("#dashboard-summary", Static).content)

    summary = asyncio.run(scenario())
    assert "second-project-marker" in summary
    assert "9.9.9" not in summary


def test_report_summary_separates_checked_skipped_and_failing_checks() -> None:
    from harness.console.screens.project_setup import describe_report
    from harness.health.model import CheckResult, Fix, Report

    report = Report(
        1,
        "/x",
        False,
        [
            CheckResult("environment.codex_sandbox", "environment", "ok", "ok"),
            CheckResult("tracker.auth", "tracker", "skipped", "офлайн"),
            CheckResult("files.project_json", "files", "fail", "bad", Fix("fix")),
        ],
    )
    text = describe_report(report)
    assert "Проверено: 2 из 3" in text
    assert "Не проверено: 1" in text and "не считается успешной" in text
    assert "Требует исправления: 1" in text
    assert "Online" in text and "файлы настроек" in text


def test_online_check_and_local_fixes_target_the_selected_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.console import data
    from harness.health.model import CheckResult, Report

    second = tmp_path / "backend"
    second.mkdir()
    subprocess.run(["git", "init", "-q", str(second)], check=True)
    calls: list[tuple[str, Path, bool]] = []

    def report(online: bool, status: str, message: str) -> Report:
        return Report(
            1,
            "x",
            online,
            [CheckResult("tracker.auth", "tracker", status, message)],  # type: ignore[arg-type]
        )

    def collect(repo: Path, *, online: bool = False) -> Report:
        calls.append(("collect", repo, online))
        if online:
            return report(True, "fail", "доступ запрещён (тест)")
        return report(False, "skipped", "офлайн")

    def fix(repo: Path, *, online: bool = False) -> Report:
        calls.append(("fix", repo, online))
        return report(False, "ok", "исправлено")

    monkeypatch.setattr(data, "collect_diagnostics", collect)
    monkeypatch.setattr(data, "apply_local_fixes", fix)

    async def scenario() -> str:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            await press(pilot, "#open-project")
            await wait_until(pilot, lambda: isinstance(app.screen, ProjectSetupScreen))
            app.screen.query_one("#project-path", Input).value = str(second)
            await press(pilot, "#inspect-project")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await app.workers.wait_for_complete()
            await press(pilot, "#setup-online")
            await app.workers.wait_for_complete()
            await pilot.pause()
            online_text = str(app.screen.query_one("#setup-output", Static).render())
            await press(pilot, "#setup-fix")
            await pilot.pause()
            await press(pilot, "#confirm")
            await app.workers.wait_for_complete()
            await pilot.pause()
            return online_text

    online_text = asyncio.run(scenario())
    assert "доступ запрещён (тест)" in online_text
    assert ("collect", second.resolve(), True) in calls
    assert ("fix", second.resolve(), False) in calls
    assert all(repo == second.resolve() for _, repo, _ in calls)
