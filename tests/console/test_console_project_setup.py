"""Pilot scenarios exercise explicit project selection and cancellation."""

from __future__ import annotations

import asyncio
import subprocess

import pytest
from pathlib import Path
from typing import Mapping, Sequence

from textual.widgets import Button, Input, RadioButton, RadioSet, Static

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
                and "Итого" in str(screen.query_one("#setup-output", Static).render()),
            )
            names = ("inspect-project", "activate-project", "install-project")
            idle = [screen.query_one("#" + n, Button).disabled for n in names]
            screen._set_busy(True)
            busy = [screen.query_one("#" + n, Button).disabled for n in names]
            screen._set_busy(False)
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
    assert seen["inspected"].splitlines()[0] == "✔ " + str(project.resolve())
    assert seen["running"] == "▶ Выполняется…"
    assert seen["failed"] == "✖ Каталог проекта не существует"
    assert seen["failed en"] == "✖ The project directory does not exist"


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


async def _open_setup(app: HarnessConsoleApp, pilot: object) -> ProjectSetupScreen:
    await pilot.press("p")  # type: ignore[attr-defined]
    await wait_until(pilot, lambda: isinstance(app.screen, ProjectSetupScreen))  # type: ignore[arg-type]
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
