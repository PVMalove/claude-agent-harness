"""Pilot scenarios exercise explicit project selection and cancellation."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

from textual.widgets import Checkbox, Input, RadioButton, RadioSet, Static

from harness.console.app import HarnessConsoleApp
from harness.console.screens.dashboard import DashboardScreen


def test_selecting_another_project_replaces_the_dashboard_context(
    tmp_path: Path,
) -> None:
    second = tmp_path / "backend"
    second.mkdir()
    subprocess.run(["git", "init", "-q", str(second)], check=True)

    async def scenario() -> None:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            await pilot.click("#open-project")
            app.screen.query_one("#project-path", Input).value = str(second)
            await pilot.click("#inspect-project")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.click("#activate-project")
            await pilot.pause()
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
            await pilot.click("#open-project")
            app.screen.query_one("#project-path", Input).value = str(second)
            await pilot.click("#inspect-project")
            await app.workers.wait_for_complete()
            await pilot.pause()
            options = app.screen.query_one("#capability", RadioSet)
            options.query(RadioButton)[-1].value = True
            await pilot.pause()
            await pilot.click("#install-project")
            await pilot.pause()
            assert seen == []
            app.screen.query_one("#confirm-install", Checkbox).value = True
            await pilot.pause(0.3)
            await pilot.click("#install-project")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.repo == second.resolve(), (
                seen,
                str(app.screen.query_one("#setup-output", Static).render()),
            )
            assert isinstance(app.screen, DashboardScreen)

    asyncio.run(scenario())
    assert len(seen) == 1 and seen[0][1] == second
    assert str(second) in seen[0][0]
    assert (second / ".harness/orchestration/roles").is_dir()
    assert (second / ".harness/project.json").is_file()
    assert not (tmp_path / ".harness").exists()
