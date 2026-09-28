"""Pilot tests for the console's Reports section on a real lifecycle-ledger fixture
(tests/_console_ledger_fixture.py). Skipped when textual is not installed, like
tests/test_console_app.py; driven with `asyncio.run` for the same reason."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("textual")

from textual.app import App
from textual.pilot import Pilot
from textual.widgets import Input, ListView, Static

from tests.console._console_ledger_fixture import QA_LOG_SHA256, build_reports_fixture
from harness.console.data import DashboardData
from harness.console.screens.dashboard import SECTIONS, DashboardScreen
from harness.console.screens.reports import (
    BatchTimelineScreen,
    QaLogsScreen,
    ReportScreen,
    ReportsScreen,
)


class _ReportsHost(App[None]):
    def __init__(self, repo: Path) -> None:
        super().__init__()
        self.repo = repo

    def on_mount(self) -> None:
        self.push_screen(ReportsScreen(self.repo))


def _report_names(app: App[None]) -> list[str | None]:
    return [
        item.name for item in app.screen.query_one("#report-list", ListView).children
    ]


def _static_text(app: App[None], selector: str) -> str:
    return str(app.screen.query_one(selector, Static).content)


def test_dashboard_reports_section_opens_the_reports_screen(tmp_path: Path) -> None:
    build_reports_fixture(tmp_path)

    async def scenario() -> bool:
        screen = DashboardScreen(
            tmp_path,
            collect_dashboard=lambda _repo, *, online=False: DashboardData(
                0, 0, 0, 0, "-", "0", "-", None
            ),
        )

        class _DashboardHost(App[None]):
            def on_mount(self) -> None:
                self.push_screen(screen)

        app = _DashboardHost()
        async with app.run_test() as pilot:
            menu = app.screen.query_one("#section-menu", ListView)
            menu.focus()
            menu.index = SECTIONS.index("Reports")
            await pilot.press("enter")
            await pilot.pause()
            return isinstance(app.screen, ReportsScreen)

    assert asyncio.run(scenario())


def test_report_list_filters_by_ticket_and_role(tmp_path: Path) -> None:
    build_reports_fixture(tmp_path)

    async def scenario() -> (
        tuple[list[str | None], list[str | None], list[str | None], str]
    ):
        app = _ReportsHost(tmp_path)
        async with app.run_test() as pilot:
            everything = _report_names(app)
            app.screen.query_one("#filter-ticket", Input).value = "#101"
            await pilot.pause()
            by_ticket = _report_names(app)
            app.screen.query_one("#filter-role", Input).value = "developer"
            await pilot.pause()
            by_ticket_and_role = _report_names(app)
            status = _static_text(app, "#reports-status")
        return everything, by_ticket, by_ticket_and_role, status

    everything, by_ticket, by_ticket_and_role, status = asyncio.run(scenario())
    assert everything == [
        "dispatch-stuck",
        "dispatch-qa",
        "dispatch-review",
        "dispatch-dev",
    ]
    assert by_ticket == ["dispatch-qa", "dispatch-review", "dispatch-dev"]
    assert by_ticket_and_role == ["dispatch-dev"]
    assert status == "отчётов: 1 из 4"


def test_report_screen_shows_sections_wrapped_to_the_screen_width(
    tmp_path: Path,
) -> None:
    build_reports_fixture(tmp_path)

    async def scenario() -> tuple[list[str], int, int, int, str]:
        app = _ReportsHost(tmp_path)
        async with app.run_test(size=(60, 50)) as pilot:
            report_list = app.screen.query_one("#report-list", ListView)
            report_list.focus()
            report_list.index = 3  # dispatch-dev, the one with the long Output
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ReportScreen)
            titles = [
                str(widget.content)
                for widget in app.screen.query(".section-title").results(Static)
            ]
            output = app.screen.query_one("#section-output", Static)
            return (
                titles,
                output.size.height,
                output.size.width,
                app.screen.size.width,
                _static_text(app, "#section-blockers"),
            )

    titles, height, width, screen_width, blockers = asyncio.run(scenario())
    assert titles == ["Output", "Checks", "Risks", "Blockers", "Next action"]
    assert width <= screen_width
    assert height > 1  # the long Output wraps instead of running off-screen
    assert blockers == "нет"


def test_report_text_is_rendered_literally_not_as_markup(tmp_path: Path) -> None:
    build_reports_fixture(tmp_path)

    async def scenario() -> tuple[str, str]:
        app = _ReportsHost(tmp_path)
        async with app.run_test() as pilot:
            app.screen.query_one("#filter-role", Input).value = "code-review"
            await pilot.pause()
            report_list = app.screen.query_one("#report-list", ListView)
            report_list.focus()
            report_list.index = 0
            await pilot.press("enter")
            await pilot.pause()
            return _static_text(app, "#section-output"), _static_text(
                app, "#section-review"
            )

    output, review = asyncio.run(scenario())
    assert output == "Ревью без блокирующих находок [high] не найдено"
    assert "[low] имя функции" in review


def test_report_screen_opens_its_batch_timeline(tmp_path: Path) -> None:
    build_reports_fixture(tmp_path)

    async def scenario() -> tuple[bool, str]:
        app = _ReportsHost(tmp_path)
        async with app.run_test() as pilot:
            report_list = app.screen.query_one("#report-list", ListView)
            report_list.focus()
            report_list.index = 1  # dispatch-qa
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("b")
            await pilot.pause()
            return isinstance(app.screen, BatchTimelineScreen), _static_text(
                app, "#batch-timeline"
            )

    is_timeline, text = asyncio.run(scenario())
    assert is_timeline
    assert "#101 · batch-flow · состояние: completed" in text
    assert "маршрут: developer → code-review → qa" in text
    assert "[decision] решение coordinator: accept → code-review" in text
    assert "[risk] оценка риска: schema; нужен review" in text
    assert "[state] состояние: awaiting-approval → completed" in text


def test_batch_list_opens_a_blocked_batch_timeline(tmp_path: Path) -> None:
    build_reports_fixture(tmp_path)

    async def scenario() -> str:
        app = _ReportsHost(tmp_path)
        async with app.run_test() as pilot:
            batch_list = app.screen.query_one("#batch-list", ListView)
            assert [item.name for item in batch_list.children] == [
                "batch-stuck",
                "batch-flow",
            ]
            batch_list.focus()
            batch_list.index = 0
            await pilot.press("enter")
            await pilot.pause()
            return _static_text(app, "#batch-timeline")

    text = asyncio.run(scenario())
    assert "маршрут: developer" in text
    assert "[report] отчёт developer: blocked" in text
    assert "[decision] решение coordinator: block" in text


def test_reports_screen_without_a_ledger_says_so(tmp_path: Path) -> None:
    async def scenario() -> tuple[str, list[str | None]]:
        app = _ReportsHost(tmp_path)
        async with app.run_test():
            return _static_text(app, "#reports-status"), _report_names(app)

    status, names = asyncio.run(scenario())
    assert status == "леджер оркестрации не найден или не инициализирован"
    assert names == []


async def _open_report(app: App[None], pilot: Pilot[None], index: int) -> None:
    report_list = app.screen.query_one("#report-list", ListView)
    report_list.focus()
    report_list.index = index
    await pilot.press("enter")
    await pilot.pause()


def test_qa_report_shows_its_gate_log_tail_and_attempts(tmp_path: Path) -> None:
    build_reports_fixture(tmp_path)

    async def scenario() -> str:
        app = _ReportsHost(tmp_path)
        async with app.run_test() as pilot:
            await _open_report(app, pilot, 1)  # dispatch-qa
            return _static_text(app, "#section-qa-log")

    text = asyncio.run(scenario())
    assert "попытки qa-lane: 1" in text
    assert "gate-run: clean-room checkout failed" in text
    assert "итог: passed — команд 2, с ошибкой 0" in text
    assert text.rstrip().endswith("412 passed in 9.81s")


def test_qa_logs_screen_lists_every_log_and_attempt(tmp_path: Path) -> None:
    build_reports_fixture(tmp_path)

    async def scenario() -> tuple[bool, str]:
        app = _ReportsHost(tmp_path)
        async with app.run_test() as pilot:
            await pilot.press("f3")
            await pilot.pause()
            return isinstance(app.screen, QaLogsScreen), _static_text(app, "#qa-logs")

    is_qa_logs, text = asyncio.run(scenario())
    assert is_qa_logs
    assert f"qa-artifacts/{QA_LOG_SHA256}.log" in text
    assert "dispatch-qa · #101 · отчёт: completed" in text
    assert "dispatch-qa · gate-run: clean-room checkout failed" in text


def test_report_export_writes_markdown_into_the_ticket_folder(tmp_path: Path) -> None:
    build_reports_fixture(tmp_path)
    ticket_folder = tmp_path / "docs" / "tasks" / "issue-101-console-reports"
    ticket_folder.mkdir(parents=True)

    async def scenario() -> None:
        app = _ReportsHost(tmp_path)
        async with app.run_test() as pilot:
            await _open_report(app, pilot, 1)  # dispatch-qa
            await pilot.press("e")
            await pilot.pause()

    asyncio.run(scenario())
    [exported] = (ticket_folder / "artifacts").glob("*.md")
    assert exported.name.endswith("-report-dispatch-qa.md")
    text = exported.read_text(encoding="utf-8")
    assert text.startswith("# Completion report dispatch-qa\n")
    assert "- **Ticket:** #101" in text
    assert "## Output\n\nQA gate passed" in text
    assert "## QA log\n\n```text\nпопытки qa-lane: 1" in text
    assert "412 passed in 9.81s" in text


def test_timeline_export_writes_markdown_table(tmp_path: Path) -> None:
    build_reports_fixture(tmp_path)

    async def scenario() -> None:
        app = _ReportsHost(tmp_path)
        async with app.run_test() as pilot:
            batch_list = app.screen.query_one("#batch-list", ListView)
            batch_list.focus()
            batch_list.index = 1  # batch-flow
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, BatchTimelineScreen)
            await pilot.press("e")
            await pilot.pause()

    asyncio.run(scenario())
    [exported] = (
        tmp_path / "docs" / "tasks" / "issue-101-console-export" / "artifacts"
    ).glob("*.md")
    assert exported.name.endswith("-timeline-batch-flow.md")
    text = exported.read_text(encoding="utf-8")
    assert text.startswith("# Хронология батча batch-flow\n")
    assert "- **Маршрут:** developer → code-review → qa" in text
    assert "| Время | Тип | Роль | Событие |" in text
