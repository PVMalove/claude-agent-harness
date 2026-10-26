"""Pilot scenarios exercise explicit project selection and cancellation."""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass

import pytest
from pathlib import Path
from typing import Mapping, Sequence

from textual.pilot import Pilot
from textual.widgets import (
    Button,
    DirectoryTree,
    Input,
    RadioButton,
    RadioSet,
    Static,
)

from harness.console.app import HarnessConsoleApp
from harness.console.screens.dashboard import DashboardScreen
from harness.console.screens.project_setup import ProjectSetupScreen
from harness.console.shell import Fold

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
            await pilot.press("p")
            await wait_until(pilot, lambda: isinstance(app.screen, ProjectSetupScreen))
            app.screen.query_one("#project-path", Input).value = str(second)
            await press(pilot, "#inspect-project")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await press(pilot, "#activate-project")
            await wait_until(
                pilot,
                lambda: isinstance(app.screen, DashboardScreen)
                and app.screen.query("TopBar"),
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
            await pilot.press("p")
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
                and app.screen.query("TopBar"),
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
            await pilot.press("p")
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
            assert "▶ Предварительные проверки" in stage
            plan = str(screen.query_one("#installation-plan", Static).render())
            assert "Состав     Backend (backend-orchestration)" in plan
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
    from harness.console.screens.project_setup import ChecksSummary, summarize_report
    from harness.health.model import CheckResult, Fix, Report

    report = Report(
        1,
        "/x",
        False,
        [
            CheckResult("environment.codex_sandbox", "environment", "ok", "ok"),
            CheckResult("tracker.auth", "tracker", "skipped", "офлайн"),
            CheckResult("files.project_json", "files", "fail", "bad", Fix("fix")),
            CheckResult("files.agents_md", "files", "warn", "old"),
        ],
    )
    # A skipped check is not a success: 4 checks, 3 ran, 2 need a fix, 1 was not checked.
    assert summarize_report(report) == ChecksSummary(3, 4, 2, 1)


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
            await pilot.press("p")
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


def test_the_actions_are_disabled_while_an_operation_runs(tmp_path: Path) -> None:
    second = tmp_path / "backend"
    second.mkdir()
    subprocess.run(["git", "init", "-q", str(second)], check=True)

    async def scenario() -> tuple[list[bool], list[bool]]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            await pilot.press("p")
            await wait_until(pilot, lambda: isinstance(app.screen, ProjectSetupScreen))
            screen = app.screen
            assert isinstance(screen, ProjectSetupScreen)
            screen.query_one("#project-path", Input).value = str(second)
            await press(pilot, "#inspect-project")
            # The local diagnostics start by themselves: wait until they are done.
            await wait_until(
                pilot,
                lambda: not screen.busy
                and "Итого" in str(screen.query_one("#setup-output", Static).render())
                and screen._blocked is not None,
            )
            names = ("inspect-project", "activate-project", "install-project")
            idle = [screen.query_one("#" + n, Button).disabled for n in names]
            screen._set_busy(True)
            busy = [screen.query_one("#" + n, Button).disabled for n in names]
            screen._set_busy(False)
            # The plan's blockers are read again in a worker; "Установить" waits for them.
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert [screen.query_one("#" + n, Button).disabled for n in names] == idle
            return idle, busy

    idle, busy = asyncio.run(scenario())
    assert idle == [False, False, False]
    assert busy == [True, True, True]


def test_capability_change_keeps_install_disabled_during_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from harness.console import data
    from harness.console.screens.harness import CommandFormScreen
    from harness.health.model import Report

    second = tmp_path / "backend with spaces"
    second.mkdir()
    subprocess.run(["git", "init", "-q", str(second)], check=True)
    started, release = threading.Event(), threading.Event()

    def collect(repo: Path, *, online: bool = False) -> Report:
        started.set()
        if not release.wait(10):
            raise TimeoutError("Test did not release diagnostics")
        return Report(1, str(repo), online, [])

    monkeypatch.setattr(data, "collect_diagnostics", collect)

    async def scenario() -> None:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(140, 60)) as pilot:
            try:
                await pilot.press("p")
                await wait_until(
                    pilot, lambda: isinstance(app.screen, ProjectSetupScreen)
                )
                screen = app.screen
                assert isinstance(screen, ProjectSetupScreen)
                screen.query_one("#project-path", Input).value = str(second)
                await press(pilot, "#inspect-project")
                await wait_until(pilot, started.is_set)
                assert screen.busy
                assert screen.query_one("#install-project", Button).disabled

                # A queued capability change must not enable installation mid-operation.
                screen.query_one("#capability", RadioSet).query(RadioButton)[
                    -1
                ].value = True
                await pilot.pause()
                assert screen.busy
                assert screen.query_one("#install-project", Button).disabled

                release.set()
                await wait_until(pilot, lambda: not screen.busy)
                await press(pilot, "#install-project")
                await wait_until(
                    pilot, lambda: isinstance(app.screen, CommandFormScreen)
                )
                await press(pilot, "#cancel")
                await wait_until(pilot, lambda: app.screen is screen)
            finally:
                release.set()

    asyncio.run(scenario())


# -- Open project on the shared shell --------------------------------------------------------


def test_open_project_sits_on_the_shell_and_digits_typed_in_the_path_stay_there(
    tmp_path: Path,
) -> None:
    from textual.widgets import ListItem, ListView

    from harness.console.shell import SECTIONS, Breadcrumbs
    from tests.console._pilot_wait import crumb_text

    async def scenario() -> tuple[str, list[str | None], int | None, bool, str, bool]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            await pilot.press("p")
            await wait_until(pilot, lambda: isinstance(app.screen, ProjectSetupScreen))
            screen = app.screen
            menu = screen.query_one("#shell-sections", ListView)
            active = [
                item.name for item in menu.query(ListItem) if item.has_class("-active")
            ]
            crumbs = crumb_text(screen.query_one("#shell-breadcrumbs", Breadcrumbs))
            field = screen.query_one("#project-path", Input)
            focused = field.has_focus
            await pilot.press("2", "6")
            await pilot.pause()
            typed = field.value
            # Typing into the path opens no section; the section's own number is a no-op too.
            stayed = app.screen is screen
            field.blur()
            await pilot.press("1")
            await pilot.pause()
            return (
                crumbs,
                active,
                menu.index,
                focused,
                typed,
                stayed and (app.screen is screen),
            )

    crumbs, active, index, focused, typed, stayed = asyncio.run(scenario())
    assert crumbs == "Главная  ›  Открыть проект"
    assert active == ["Open project"]
    assert index == SECTIONS.index("Open project")
    assert focused and typed.endswith("26") and stayed


def test_escape_cancels_with_none_and_the_menu_opens_another_section(
    tmp_path: Path,
) -> None:
    results: list[object] = []

    async def scenario() -> tuple[bool, bool, bool]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            screen = ProjectSetupScreen(tmp_path, command_runner=lambda *a, **k: None)  # type: ignore[arg-type]
            await app.push_screen(screen, results.append)
            await wait_until(pilot, lambda: app.screen is screen)
            screen.query_one("#project-path", Input).blur()
            await pilot.press("6")  # Reports opens on top of the screen
            await wait_until(pilot, lambda: app.screen is not screen)
            above = app.screen is not screen
            await pilot.press("escape")
            await wait_until(pilot, lambda: app.screen is screen)
            back = app.screen is screen and results == []
            await pilot.press("escape")
            await wait_until(pilot, lambda: app.screen is not screen)
            return above, back, isinstance(app.screen, DashboardScreen)

    above, back, home = asyncio.run(scenario())
    assert above and back and home
    assert results == [None]


def test_the_section_menu_waits_for_a_running_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> tuple[bool, bool, list[str]]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            await pilot.press("p")
            await wait_until(pilot, lambda: isinstance(app.screen, ProjectSetupScreen))
            screen = app.screen
            assert isinstance(screen, ProjectSetupScreen)
            notes: list[str] = []
            monkeypatch.setattr(
                screen, "notify", lambda message, **_: notes.append(message)
            )
            screen.query_one("#project-path", Input).blur()
            screen._set_busy(True)
            await pilot.press("6")
            await pilot.pause()
            number_ignored = app.screen is screen
            await pilot.press("escape")
            await pilot.pause()
            escape_ignored = app.screen is screen
            screen._set_busy(False)
            await pilot.press("6")
            # Times out unless the section opens once the operation is over.
            await wait_until(pilot, lambda: app.screen is not screen)
            return number_ignored, escape_ignored, notes

    number_ignored, escape_ignored, notes = asyncio.run(scenario())
    assert number_ignored and escape_ignored
    assert notes == ["Дождитесь окончания операции"] * 2


def test_the_busy_note_has_an_english_text() -> None:
    from harness.console import i18n

    assert (
        i18n.tr("en", "Дождитесь окончания операции")
        == "Wait for the operation to finish"
    )


def test_setup_output_marks_report_statuses_but_not_plain_list_items() -> None:
    from harness.console.screens.diagnostics import styled_log

    text = "- установка\nИтого: ok=1 warn=0 fail=1 skipped=1\n[FAIL] env.uv: нет\n- net: офлайн"
    assert styled_log(text).plain.split("\n") == [
        "- установка",
        "Итого: ✔ ok=1 ⚠ warn=0 ✖ fail=1 ○ skipped=1",
        "✖ [FAIL] env.uv: нет",
        "○ - net: офлайн",
    ]


def test_styled_log_ignores_foreign_lines_that_only_look_like_a_report_total() -> None:
    from harness.console.screens.diagnostics import styled_log

    text = "Итого: 3 файла\n- элемент списка"
    assert styled_log(text).plain == text


# -- Esc through the shell's `action_back`, symbols on status lines, narrow terminal ----------


def test_open_project_has_no_escape_binding_of_its_own() -> None:
    from textual.binding import Binding

    own = ProjectSetupScreen.__dict__.get("BINDINGS", [])
    keys = [b.key if isinstance(b, Binding) else b[0] for b in own]
    assert "escape" not in keys
    assert not hasattr(ProjectSetupScreen, "action_cancel")


def test_escape_waits_while_an_operation_runs_and_then_dismisses_with_none(
    tmp_path: Path,
) -> None:
    results: list[object] = []

    async def scenario() -> tuple[list[str], bool]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            screen = ProjectSetupScreen(tmp_path, command_runner=lambda *a, **k: None)  # type: ignore[arg-type]
            await app.push_screen(screen, results.append)
            await wait_until(pilot, lambda: app.screen is screen)
            notes: list[str] = []
            screen.notify = lambda message, **_: notes.append(message)  # type: ignore[method-assign]
            screen.query_one("#project-path", Input).blur()
            screen._set_busy(True)
            await pilot.press("escape")
            await pilot.pause()
            stayed = app.screen is screen and results == []
            screen._set_busy(False)
            await pilot.press("escape")
            await wait_until(pilot, lambda: app.screen is not screen)
            return notes, stayed

    notes, stayed = asyncio.run(scenario())
    assert stayed and notes == ["Дождитесь окончания операции"]
    assert results == [None]


def test_status_lines_start_with_a_symbol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from harness.console.project_target import inspect_target

    project = tmp_path / "backend"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    gate = threading.Event()
    real = inspect_target

    def slow(value: str) -> object:
        gate.wait(30)
        return real(value)

    monkeypatch.setattr("harness.console.screens.project_setup.inspect_target", slow)

    async def scenario() -> dict[str, str]:
        seen: dict[str, str] = {}
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            await pilot.press("p")
            await wait_until(pilot, lambda: isinstance(app.screen, ProjectSetupScreen))
            screen = app.screen
            assert isinstance(screen, ProjectSetupScreen)
            status = screen.query_one("#target-status", Static)
            output = screen.query_one("#setup-output", Static)
            screen.query_one("#project-path", Input).value = str(project)
            await press(pilot, "#inspect-project")
            seen["inspecting"] = str(status.content)
            gate.set()
            await app.workers.wait_for_complete()
            await wait_until(
                pilot, lambda: not screen.busy and "✔" in str(status.content)
            )
            seen["inspected"] = str(status.content)
            await app.workers.wait_for_complete()
            await wait_until(pilot, lambda: not screen.busy)
            screen._run(lambda: "готово")
            seen["running"] = str(output.content)
            await app.workers.wait_for_complete()
            await wait_until(pilot, lambda: not screen.busy)
            screen.query_one("#project-path", Input).value = str(tmp_path / "missing")
            await press(pilot, "#inspect-project")
            await wait_until(pilot, lambda: str(status.content).startswith("✖"))
            seen["failed"] = str(status.content)
            app.action_toggle_language()
            await wait_until(pilot, lambda: str(status.content) != seen["failed"])
            seen["failed en"] = str(status.content)
        return seen

    seen = asyncio.run(scenario())
    assert seen["inspecting"] == "▶ Проверяется путь…"
    assert seen["inspected"].splitlines()[0] == (
        "✔ Git-проект найден · действие: новая установка"
    )
    assert seen["running"] == "▶ Выполняется…"
    assert seen["failed"].splitlines()[0] == "✖ Каталог проекта не существует"
    assert seen["failed"].splitlines()[1].startswith("Проверьте путь")
    assert seen["failed en"].splitlines()[0] == "✖ The project directory does not exist"
    assert seen["failed en"].splitlines()[1].startswith("Check the path")


@pytest.mark.parametrize("size", [(80, 24), (160, 52)])
def test_focus_starts_in_the_path_and_a_long_path_never_widens_the_page(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    from tests.console.test_console_layout import layout_problems

    long_repo = tmp_path / ("very-long-project-name-" * 5).rstrip("-")
    long_repo.mkdir()

    async def scenario() -> tuple[str | None, str | None, list[str]]:
        app = HarnessConsoleApp(long_repo)
        async with app.run_test(size=size) as pilot:
            screen = ProjectSetupScreen(long_repo, command_runner=lambda *a, **k: None)  # type: ignore[arg-type]
            await app.push_screen(screen)
            await wait_until(pilot, lambda: screen.focused is not None)
            await pilot.pause()
            first = screen.focused.id if screen.focused else None
            await pilot.press("tab")
            await pilot.pause()
            second = screen.focused.id if screen.focused else None
            return first, second, layout_problems(screen)  # type: ignore[arg-type]

    first, second, problems = asyncio.run(scenario())
    assert first == "project-path" and second == "browse-project"
    assert problems == []


# -- An invalid harness.lock, stale targets, the folder browser, threads, stages ---------------

_ACTIONS = (
    "activate-project",
    "install-project",
    "setup-offline",
    "setup-online",
    "setup-fix",
    "setup-config",
)


def _git_project(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _corrupt_lock(project: Path) -> None:
    (project / ".harness").mkdir(exist_ok=True)
    (project / ".harness/harness.lock").write_text("{not json", encoding="utf-8")


async def _open_setup(app: HarnessConsoleApp, pilot: Pilot[None]) -> ProjectSetupScreen:
    await pilot.press("p")
    await wait_until(pilot, lambda: isinstance(app.screen, ProjectSetupScreen))
    screen = app.screen
    assert isinstance(screen, ProjectSetupScreen)
    return screen


def _status(screen: ProjectSetupScreen) -> str:
    return str(screen.query_one("#target-status", Static).content)


def test_a_corrupt_lock_on_the_check_path_is_a_status_with_a_next_step(
    tmp_path: Path,
) -> None:
    project = _git_project(tmp_path / "backend")
    _corrupt_lock(project)

    async def scenario() -> tuple[str, bool, list[bool]]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            screen = await _open_setup(app, pilot)
            screen.query_one("#project-path", Input).value = str(project)
            await press(pilot, "#inspect-project")
            await wait_until(pilot, lambda: _status(screen).startswith("✖"))
            await app.workers.wait_for_complete()
            await pilot.pause()
            actions = [screen.query_one("#" + n, Button).disabled for n in _ACTIONS]
            return _status(screen), app.is_running and app.screen is screen, actions

    status, alive, actions = asyncio.run(scenario())
    assert alive
    lines = status.splitlines()
    assert lines[0] == "✖ harness.lock повреждён"
    assert "«Диагностика» (3)" in lines[1]
    assert all(actions)


def test_a_corrupt_lock_on_the_install_path_is_a_status_and_nothing_is_installed(
    tmp_path: Path,
) -> None:
    project = _git_project(tmp_path / "backend")
    seen: list[list[str]] = []

    def runner(
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        seen.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    async def scenario() -> tuple[str, bool, list[bool]]:
        app = HarnessConsoleApp(tmp_path, command_runner=runner)
        async with app.run_test(size=(140, 60)) as pilot:
            screen = await _open_setup(app, pilot)
            screen.query_one("#project-path", Input).value = str(project)
            await press(pilot, "#inspect-project")
            await app.workers.wait_for_complete()
            await wait_until(pilot, lambda: not screen.busy)
            await press(pilot, "#install-project")
            # The lock breaks between the check and the confirmation.
            _corrupt_lock(project)
            await press(pilot, "#run")
            await wait_until(pilot, lambda: _status(screen).startswith("✖"))
            await app.workers.wait_for_complete()
            await pilot.pause()
            actions = [screen.query_one("#" + n, Button).disabled for n in _ACTIONS]
            return _status(screen), app.is_running and app.screen is screen, actions

    status, alive, actions = asyncio.run(scenario())
    assert alive and seen == []
    assert status.splitlines()[0] == "✖ harness.lock повреждён"
    assert "«Диагностика» (3)" in status
    assert all(actions)


async def _checked_project(
    app: HarnessConsoleApp, pilot: Pilot[None], project: Path
) -> ProjectSetupScreen:
    """Open the screen on `project`, check the path and wait for the automatic diagnostics."""
    screen = await _open_setup(app, pilot)
    screen.query_one("#project-path", Input).value = str(project)
    await press(pilot, "#inspect-project")
    await app.workers.wait_for_complete()
    await wait_until(
        pilot,
        lambda: not screen.busy
        and "Итого" in str(screen.query_one("#setup-output", Static).render()),
    )
    assert screen.target is not None and screen.stage == 1
    return screen


def _blank(screen: ProjectSetupScreen) -> dict[str, object]:
    """What the screen shows once no target is selected."""
    return {
        "target": screen.target,
        "stage": screen.stage,
        "plan": str(screen.query_one("#installation-plan", Static).content),
        "output": str(screen.query_one("#setup-output", Static).content),
        "disabled": [screen.query_one("#" + n, Button).disabled for n in _ACTIONS],
    }


_BLANK = {
    "target": None,
    "stage": 0,
    "plan": "План появится после проверки пути.",
    "output": "Результат проверок и установки появится здесь.",
    "disabled": [True] * len(_ACTIONS),
}


def test_a_failed_recheck_clears_the_previous_target_plan_and_result(
    tmp_path: Path,
) -> None:
    project = _git_project(tmp_path / "backend")

    async def scenario() -> tuple[dict[str, object], str]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            screen = await _checked_project(app, pilot, project)
            _corrupt_lock(project)
            await press(pilot, "#inspect-project")  # the same path, checked again
            await wait_until(pilot, lambda: _status(screen).startswith("✖"))
            await app.workers.wait_for_complete()
            await pilot.pause()
            return _blank(screen), _status(screen)

    blank, status = asyncio.run(scenario())
    assert blank == _BLANK
    assert status.splitlines()[0] == "✖ harness.lock повреждён"


def test_a_lock_with_a_wrong_files_field_leaves_nothing_of_the_previous_run(
    tmp_path: Path,
) -> None:
    project = _git_project(tmp_path / "backend")
    (project / ".harness").mkdir()
    (project / ".harness/harness.lock").write_text(
        '{"capabilities": ["pvmalove-suite"], "files": []}', encoding="utf-8"
    )

    async def scenario() -> tuple[dict[str, object], str, bool]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            screen = await _open_setup(app, pilot)
            screen.query_one("#project-path", Input).value = str(project)
            await press(pilot, "#inspect-project")
            await wait_until(pilot, lambda: _status(screen).startswith("✖"))
            await app.workers.wait_for_complete()
            await pilot.pause()
            summary = screen.query_one("#checks-summary", Static).display
            return _blank(screen), _status(screen), summary

    blank, status, summary_shown = asyncio.run(scenario())
    assert blank == _BLANK and not summary_shown
    assert status.splitlines()[0] == "✖ harness.lock повреждён"


def test_a_result_of_an_operation_that_a_reset_outdated_is_dropped(
    tmp_path: Path,
) -> None:
    import threading

    from harness.health.model import CheckResult, Report

    project = _git_project(tmp_path / "backend")
    release = threading.Event()

    def slow() -> Report:
        release.wait(10)
        return Report(
            1,
            str(project),
            False,
            [CheckResult("files.agents_md", "files", "ok", "ok")],
        )

    async def scenario() -> tuple[dict[str, object], bool, bool]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            screen = await _checked_project(app, pilot, project)
            screen._run(slow)
            await pilot.pause()
            screen._failed("Путь изменился; проверьте его ещё раз")
            release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            summary = screen.query_one("#checks-summary", Static).display
            return _blank(screen), screen.busy, summary

    blank, busy, summary_shown = asyncio.run(scenario())
    assert blank == _BLANK and not busy and not summary_shown


def test_editing_the_path_clears_the_previous_target_status_plan_and_result(
    tmp_path: Path,
) -> None:
    project = _git_project(tmp_path / "backend")

    async def scenario() -> tuple[dict[str, object], str, str]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            screen = await _checked_project(app, pilot, project)
            screen.query_one("#project-path", Input).value = str(project) + "-2"
            await pilot.pause()
            ru = _status(screen)
            app.action_toggle_language()
            await pilot.pause()
            return _blank(screen), ru, _status(screen)

    blank, ru, en = asyncio.run(scenario())
    assert blank["target"] is None and blank["stage"] == 0
    assert blank["disabled"] == _BLANK["disabled"]
    assert ru == "○ Укажите путь к проекту и нажмите «Проверить путь»."
    assert en == "○ Enter the project path and press “Check path”."
    assert blank["plan"] == "The plan appears after the path is checked."
    assert blank["output"] == "Check and install results appear here."


async def _open_browser(
    app: HarnessConsoleApp, pilot: Pilot[None], screen: ProjectSetupScreen, root: Path
) -> DirectoryTree:
    screen.query_one("#project-path", Input).value = str(root)
    await press(pilot, "#browse-project")
    await wait_until(pilot, lambda: len(screen.query("#directory-browser")) == 1)
    tree = screen.query_one("#directory-browser", DirectoryTree)
    await wait_until(pilot, lambda: tree.has_focus and tree.root.children)
    return tree


def test_escape_in_the_folder_browser_closes_only_the_tree(tmp_path: Path) -> None:
    (tmp_path / "projects" / "app").mkdir(parents=True)
    results: list[object] = []

    async def scenario() -> tuple[bool, int, bool]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            screen = ProjectSetupScreen(tmp_path, command_runner=lambda *a, **k: None)  # type: ignore[arg-type]
            await app.push_screen(screen, results.append)
            await wait_until(pilot, lambda: app.screen is screen)
            await _open_browser(app, pilot, screen, tmp_path / "projects")
            await pilot.press("escape")
            await wait_until(pilot, lambda: not screen.query("#directory-browser"))
            stayed = app.screen is screen and results == []
            # The next Esc leaves the screen as before.
            await pilot.press("escape")
            await wait_until(pilot, lambda: app.screen is not screen)
            return stayed, len(results), True

    stayed, closed, _ = asyncio.run(scenario())
    assert stayed and closed == 1 and results == [None]


def test_right_arrow_expands_a_folder_and_enter_puts_it_in_the_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "projects"
    (root / "app" / "inner").mkdir(parents=True)

    async def scenario() -> tuple[list[str], str, int, bool]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            screen = await _open_setup(app, pilot)
            tree = await _open_browser(app, pilot, screen, root)
            await pilot.press("down")  # the root's only child: `app`
            await pilot.press("right")
            node = tree.cursor_node
            assert node is not None
            await wait_until(pilot, lambda: node.is_expanded and node.children)
            inner = [str(child.label) for child in node.children]
            # Enter selects the highlighted folder as the project path and closes the tree.
            await pilot.press("enter")
            await wait_until(pilot, lambda: not screen.query("#directory-browser"))
            value = screen.query_one("#project-path", Input).value
            return inner, value, len(app.screen_stack), app.screen is screen

    inner, value, _, on_screen = asyncio.run(scenario())
    assert inner == ["inner"]
    assert value == str(root / "app")
    assert on_screen


def test_a_click_on_a_folder_name_highlights_it_but_does_not_select_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "projects"
    (root / "app").mkdir(parents=True)

    async def scenario() -> tuple[str, str, bool, str | None]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            screen = await _open_setup(app, pilot)
            tree = await _open_browser(app, pilot, screen, root)
            field = screen.query_one("#project-path", Input)
            before = field.value
            # Row 0 is the border: the root is row 1, the folder `app` row 2; the click is on its name.
            await pilot.click(tree, offset=(14, 2))
            await pilot.pause(0.3)
            node = tree.cursor_node
            return (
                before,
                field.value,
                bool(screen.query("#directory-browser")),
                str(node.label) if node else None,
            )

    before, after, still_open, highlighted = asyncio.run(scenario())
    assert before == str(root) and after == before
    assert still_open and highlighted == "app"


def _locked_project(path: Path, *capabilities: str) -> Path:
    import json

    project = _git_project(path)
    (project / ".harness").mkdir(exist_ok=True)
    (project / ".harness/harness.lock").write_text(
        json.dumps({"capabilities": list(capabilities), "files": {}}), encoding="utf-8"
    )
    return project


def test_git_and_the_snapshot_comparison_never_run_in_the_ui_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    import harness.bin.harness as cli

    project = _locked_project(tmp_path / "backend", "pvmalove-suite")
    ui_thread = threading.get_ident()
    in_ui: list[str] = []
    calls: list[str] = []
    watching = threading.Event()

    def spy(name: str, real: object) -> object:
        def wrapper(*args: object, **kwargs: object) -> object:
            if watching.is_set():
                calls.append(name)
                if threading.get_ident() == ui_thread:
                    in_ui.append(name)
            return real(*args, **kwargs)  # type: ignore[operator]

        return wrapper

    monkeypatch.setattr(subprocess, "run", spy("git", subprocess.run))
    monkeypatch.setattr(cli, "snapshot_diff", spy("snapshot_diff", cli.snapshot_diff))

    async def scenario() -> None:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 48)) as pilot:
            screen = await _open_setup(app, pilot)
            watching.set()
            screen.query_one("#project-path", Input).value = str(project)
            await press(pilot, "#inspect-project")
            await app.workers.wait_for_complete()
            await wait_until(pilot, lambda: not screen.busy)
            # Another capability: the plan is checked again.
            screen.query_one("#capability", RadioSet).query(RadioButton)[0].value = True
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert not screen.query_one("#install-project", Button).disabled

    asyncio.run(scenario())
    assert "git" in calls and "snapshot_diff" in calls
    assert in_ui == []


def test_stage_three_is_current_while_installing_and_the_stage_follows_the_outcome(
    tmp_path: Path,
) -> None:
    import threading

    project = _git_project(tmp_path / "backend")
    started, release = threading.Event(), threading.Event()
    outcome = {"code": 0}

    def runner(
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        started.set()
        release.wait(30)
        return subprocess.CompletedProcess(list(argv), outcome["code"], "", "")

    async def install(
        app: HarnessConsoleApp, pilot: Pilot[None], screen: ProjectSetupScreen
    ) -> str:
        await press(pilot, "#install-project")
        await press(pilot, "#run")
        await wait_until(pilot, started.is_set)
        await pilot.pause()
        during = str(screen.query_one("#setup-stage", Static).render())
        release.set()
        await app.workers.wait_for_complete()
        await wait_until(pilot, lambda: not screen.busy)
        return during

    async def scenario() -> tuple[str, int, str, int]:
        app = HarnessConsoleApp(tmp_path, command_runner=runner)
        async with app.run_test(size=(140, 60)) as pilot:
            try:
                screen = await _checked_project(app, pilot, project)
                during = await install(app, pilot, screen)
                after_success = screen.stage
                # A failed installation returns to the checks.
                started.clear()
                release.clear()
                outcome["code"] = 1
                during_failure = await install(app, pilot, screen)
                return during, after_success, during_failure, screen.stage
            finally:
                release.set()

    during, after_success, during_failure, after_failure = asyncio.run(scenario())
    assert "▶ Установка / внедрение" in during
    assert "▶ Установка / внедрение" in during_failure
    assert after_success == 3
    assert after_failure == 1


# -- The screen at 160x52, 120x40 and 80x24, in Russian and English ----------------------------

_SIZES = [(160, 52), (120, 40), (80, 24)]
_BLOCKS = (
    "project-frame",
    "capability-frame",
    "plan-frame",
    "checks-frame",
    "output-frame",
)
_WORDS: dict[str, dict[str, tuple[str, ...]]] = {
    "ru": {
        "title": ("Открыть проект",),
        "wide": ("Обзор папок", "Проверить путь", "Открыть проект →"),
        "narrow": ("Обзор", "Проверить", "Открыть →"),
        "cards": (
            "ПРОЕКТ",
            "СОСТАВ HARNESS",
            "ПЛАН УСТАНОВКИ",
            "ПРОВЕРКИ",
            "РЕЗУЛЬТАТ",
        ),
        "status": ("Git-проект найден · действие: новая установка",),
        "summary": ("Проверено",),
    },
    "en": {
        "title": ("Open project",),
        "wide": ("Browse folders", "Check path", "Open project →"),
        "narrow": ("Browse", "Check", "Open →"),
        "cards": ("PROJECT", "HARNESS SET", "INSTALL PLAN", "CHECKS", "RESULT"),
        "status": ("Git project found · action: new installation",),
        "summary": ("Checked",),
    },
}


def _shown(widget: object) -> bool:
    """On the screen: itself and everything around it displayed, and it has an area."""
    from textual.widget import Widget

    assert isinstance(widget, Widget)
    return widget.region.area > 0 and all(
        node.display for node in [widget, *widget.ancestors] if isinstance(node, Widget)
    )


@dataclass
class _Seen:
    """What a user sees of the screen: texts, labels, the places of the blocks."""

    title: str
    subtitle: bool
    strip: bool
    labels: tuple[str, ...]
    status: str
    stage: str
    cards: list[str]
    y: list[int]
    x: list[int]
    open_folds: list[bool]
    install: bool
    command: str
    summary: str
    footer: set[str]
    chrome: list[str]


async def _look(
    app: HarnessConsoleApp, pilot: Pilot[None], project: Path, language: str
) -> _Seen:
    screen = await _open_setup(app, pilot)
    if language != app.language:
        app.action_toggle_language()
    await pilot.pause()
    await app.workers.wait_for_complete()
    screen.query_one("#project-path", Input).value = str(project)
    await press(pilot, "#inspect-project")
    await app.workers.wait_for_complete()
    await wait_until(
        pilot,
        lambda: not screen.busy
        and screen._summary is not None
        and screen._blocked is not None,
    )
    await pilot.pause()

    def text(selector: str) -> str:
        return str(screen.query_one(selector, Static).content)

    labels = tuple(
        str(screen.query_one("#" + name, Button).label)
        for name in ("browse-project", "inspect-project", "activate-project")
    )
    cards = [str(screen.query_one("#" + name).border_title) for name in _BLOCKS]
    return _Seen(
        title=text("#setup-title"),
        subtitle=_shown(screen.query_one("#setup-subtitle")),
        strip=_shown(screen.query_one("#setup-open")),
        labels=labels,
        status=text("#target-status").splitlines()[0],
        stage=text("#setup-stage"),
        cards=cards,
        y=[screen.query_one("#" + name).region.y for name in _BLOCKS],
        x=[screen.query_one("#" + name).region.x for name in _BLOCKS],
        open_folds=[
            not screen.query_one("#" + name, Fold).collapsed
            for name in ("capability-frame", "output-frame")
        ],
        install=_shown(screen.query_one("#install-project")),
        command=text("#plan-command"),
        summary=text("#checks-summary"),
        footer={
            key for key, active in screen.active_bindings.items() if active.binding.show
        },
        chrome=[
            text("#setup-title"),
            text("#setup-subtitle"),
            text("#setup-open"),
            text("#setup-stage"),
            text("#target-status"),
            text("#plan-command-label"),
            text("#plan-hint"),
            text("#capability-detail"),
            text("#checks-summary"),
            *labels,
            *cards,
        ],
    )


@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize("size", _SIZES, ids=[f"{w}x{h}" for w, h in _SIZES])
def test_the_screen_follows_the_mockup_composition_at_every_size(
    tmp_path: Path, size: tuple[int, int], language: str
) -> None:
    import re
    import shlex

    project = _git_project(tmp_path / "backend")
    words = _WORDS[language]
    narrow = size[0] < 100

    async def scenario() -> _Seen:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=size) as pilot:
            return await _look(app, pilot, project, language)

    seen = asyncio.run(scenario())
    assert seen.title == words["title"][0]
    assert seen.labels == (words["narrow"] if narrow else words["wide"])
    assert seen.status == "✔ " + words["status"][0]
    assert "▶" in seen.stage
    assert seen.command == shlex.join(
        ["harness", "init", str(project.resolve()), "--capability", "pvmalove-suite"]
    )
    assert seen.summary.startswith(f"✔ {words['summary'][0]}")
    assert seen.install
    # Narrow: no subtitle and no strip, the set and the result are folded, the cards have no
    # titles. Wide: all of them shown, open.
    assert seen.subtitle is not narrow and seen.strip is not narrow
    assert seen.open_folds == [not narrow, not narrow]
    if not narrow:
        for title, card in zip(seen.cards, words["cards"]):
            assert card in title
    # The footer holds the mockup's keys and nothing else: Esc, F1, F2 and the palette.
    assert {"escape", "f1", "f2"} <= seen.footer
    assert "y" not in seen.footer
    # Block order: the project, the set, the plan, the checks, the result. Two columns only on a
    # spacious terminal (the plan beside the project), otherwise one under another.
    ys, xs = seen.y, seen.x
    assert ys[0] < ys[1] and ys[2] < ys[3] < ys[4]
    if size[0] >= 140:
        assert ys[0] == ys[2] and xs[0] < xs[2]
    else:
        assert ys[1] < ys[2] and xs[0] == xs[2]
    if language == "en":
        # Everything fixed on the screen has an English text (the report lines are data).
        assert [line for line in seen.chrome if re.search("[А-Яа-я]", line)] == []


def test_the_screen_shows_no_demo_data_from_the_mockup() -> None:
    """The mockup's sample values (branch, counts, paths, version) are not in the sources: the
    screen shows only what the console receives."""
    root = Path(__file__).resolve().parents[2] / "harness" / "console"
    for source in (root / "screens" / "project_setup.py", root / "project_target.py"):
        text = source.read_text(encoding="utf-8")
        for demo in (
            "feature/issue-721",
            "16 из 27",
            "11 онлайн",
            "/home/malove",
            "1.4.6",
        ):
            assert demo not in text, f"{source.name}: {demo}"


def test_y_copies_the_git_init_hint_and_then_the_installation_command(
    tmp_path: Path,
) -> None:
    import shlex

    plain = tmp_path / "notes"
    plain.mkdir()
    project = _git_project(tmp_path / "backend")

    async def scenario() -> tuple[str, str]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(140, 60)) as pilot:
            screen = await _open_setup(app, pilot)
            screen.query_one("#project-path", Input).value = str(plain)
            await press(pilot, "#inspect-project")
            await wait_until(pilot, lambda: _status(screen).startswith("✖"))
            screen.set_focus(None)  # `y` is a letter: in the path field it is typed
            await pilot.press("y")
            hint = app.clipboard
            screen.query_one("#project-path", Input).value = str(project)
            await press(pilot, "#inspect-project")
            await app.workers.wait_for_complete()
            await wait_until(
                pilot, lambda: not screen.busy and screen._blocked is not None
            )
            screen.set_focus(None)
            await pilot.press("y")
            return hint, app.clipboard

    hint, command = asyncio.run(scenario())
    assert hint == "git init"
    assert command == shlex.join(
        ["harness", "init", str(project.resolve()), "--capability", "pvmalove-suite"]
    )


def test_an_option_button_keeps_both_lines_of_its_label() -> None:
    """Guards the private Textual API `_OptionButton` relies on (`_make_label`, `_label`)."""
    from textual.geometry import Size

    from harness.console.screens.project_setup import _OptionButton

    button = _OptionButton("name\ndescription")
    assert button._label.plain == "name\ndescription"
    assert button.get_content_height(Size(40, 10), Size(80, 24), 40) == 2
