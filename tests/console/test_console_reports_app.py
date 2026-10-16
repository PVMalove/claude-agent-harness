"""Pilot tests for the console's Reports section on a real lifecycle-ledger fixture
(tests/_console_ledger_fixture.py). Skipped when textual is not installed, like
tests/test_console_app.py; driven with `asyncio.run` for the same reason."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

import pytest

pytest.importorskip("textual")

from textual.app import App
from textual.pilot import Pilot
from textual.widgets import Button, Input, ListView, Select, Static

from tests.console._pilot_wait import wait_until
from tests.console._console_ledger_fixture import QA_LOG_SHA256, build_reports_fixture
from harness.console.app import HarnessConsoleApp
from harness.console.data import DashboardData
from harness.console.screens.dashboard import DashboardScreen
from harness.console.shell import SECTIONS
from harness.console.screens.reports import (
    BatchTimelineScreen,
    QaLogsScreen,
    ReportScreen,
    ReportsScreen,
)


class _ReportsHost(App[None]):
    CSS = HarnessConsoleApp.CSS

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


async def _until(pilot: Pilot[None], condition: Callable[[], object]) -> None:
    """Pause until `condition` holds: filtering rebuilds the report list asynchronously, and one
    pause is not always enough on a slow runner."""
    await wait_until(pilot, condition)


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
            await wait_until(pilot, lambda: isinstance(app.screen, ReportsScreen))
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
            await _until(pilot, lambda: len(_report_names(app)) == 3)
            by_ticket = _report_names(app)
            app.screen.query_one("#filter-role", Input).value = "developer"
            await _until(pilot, lambda: len(_report_names(app)) == 1)
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


def test_current_stage_opens_timeline_and_refresh_preserves_filters_sort_and_selection(
    tmp_path: Path,
) -> None:
    from harness.orchestration.core.constants import STATE_REL
    from harness.orchestration.ledger.lifecycle import LifecycleLedger
    from tests.console._console_ledger_fixture import _Batch

    build_reports_fixture(tmp_path)
    ledger = LifecycleLedger(tmp_path / STATE_REL)
    batch = _Batch(ledger, "batch-live", "#303")
    batch.save("awaiting-approval")
    batch.dispatch("dispatch-live", "developer")
    status_path = batch.root / "dispatch-status/dispatch-live.json"
    ledger.write_immutable(
        status_path, {"dispatch_id": "dispatch-live", "state": "working"}
    )

    async def scenario() -> None:
        app = _ReportsHost(tmp_path)
        async with app.run_test(size=(100, 35)) as pilot:
            rendered = "\n".join(
                "".join(segment.text for segment in strip)
                for strip in app.screen._compositor.render_strips()
            )
            assert "Все стадии" in rendered
            assert "По времени" in rendered
            assert "По убыванию" in rendered
            assert "dispatch-live" in _report_names(app)
            app.screen.query_one("#filter-state", Input).value = "working"
            await _until(pilot, lambda: _report_names(app) == ["dispatch-live"])
            listing = app.screen.query_one("#report-list", ListView)
            listing.focus()
            listing.index = 0
            await pilot.press("enter")
            await _until(
                pilot,
                lambda: isinstance(app.screen, BatchTimelineScreen)
                and bool(app.screen.query("#batch-timeline")),
            )
            assert "отчёт ещё не получен" in _static_text(app, "#batch-timeline")
            await pilot.press("escape")
            await _until(pilot, lambda: isinstance(app.screen, ReportsScreen))
            app.screen.query_one("#filter-state", Input).value = ""
            app.screen.query_one("#filter-role", Input).value = "developer"
            app.screen.query_one("#sort-field", Select).value = "ticket"
            app.screen.query_one("#sort-direction", Select).value = "desc"
            await _until(
                pilot,
                lambda: _report_names(app)
                == ["dispatch-live", "dispatch-stuck", "dispatch-dev"],
            )
            listing = app.screen.query_one("#report-list", ListView)
            listing.index = 0
            batch.report("dispatch-live", "developer", "completed", output="Обновлено")
            ledger.replace(
                status_path, {"dispatch_id": "dispatch-live", "state": "reported"}
            )
            refresh = app.screen.query_one("#refresh-reports", Button)
            assert refresh.region.height == 1
            assert refresh.region.width <= 5
            await pilot.click("#refresh-reports")
            await app.workers.wait_for_complete()
            await _until(pilot, lambda: not refresh.disabled)
            assert _report_names(app) == [
                "dispatch-live",
                "dispatch-stuck",
                "dispatch-dev",
            ]
            assert listing.highlighted_child is not None
            assert listing.highlighted_child.name == "dispatch-live"
            assert app.screen.query_one("#filter-role", Input).value == "developer"
            assert app.screen.query_one("#sort-field", Select).value == "ticket"
            listing.focus()
            await pilot.press("enter")
            await _until(
                pilot,
                lambda: isinstance(app.screen, ReportScreen)
                and bool(app.screen.query("#section-output")),
            )
            assert _static_text(app, "#section-output") == "Обновлено"
            await pilot.press("escape")
            await _until(pilot, lambda: isinstance(app.screen, ReportsScreen))
            app.screen.query_one("#filter-completion", Select).value = "open"
            await _until(
                pilot,
                lambda: not _report_names(app)
                and bool(app.screen.query("#reports-empty")),
            )
            assert "Ничего не найдено" in _static_text(app, "#reports-empty")

    asyncio.run(scenario())


def test_batch_rows_and_timeline_show_start_and_time_to_pr_in_a_narrow_window(
    tmp_path: Path,
) -> None:
    from dataclasses import replace
    from harness.console.reports import load_ledger_view

    build_reports_fixture(tmp_path)
    view = load_ledger_view(tmp_path)
    view.batches[:] = [
        replace(
            batch,
            created_at="2026-10-12T10:00:00Z",
            pr_created_at="2026-10-12T12:14:00Z",
            pr_url="https://example.test/pr/10",
            pr_state="found",
        )
        for batch in view.batches
    ]

    class Host(App[None]):
        CSS = HarnessConsoleApp.CSS

        def on_mount(self) -> None:
            self.push_screen(ReportsScreen(tmp_path, load_view=lambda _repo: view))

    async def scenario() -> None:
        app = Host()
        async with app.run_test(size=(60, 35)) as pilot:
            listing = app.screen.query_one("#batch-list", ListView)
            first = str(listing.children[0].query_one(Static).content)
            assert "12.10.2026 10:00 UTC" in first
            assert "blocked · до PR: 2 ч 14 мин" in first
            refresh = app.screen.query_one("#refresh-reports", Button)
            assert refresh.region.height == 1
            assert refresh.region.right <= 60
            listing.focus()
            listing.index = 0
            await pilot.press("enter")
            await _until(
                pilot,
                lambda: isinstance(app.screen, BatchTimelineScreen)
                and bool(app.screen.query("#batch-timeline")),
            )
            text = _static_text(app, "#batch-timeline")
            assert "Начало: 12.10.2026 10:00 UTC" in text
            assert "blocked · до PR: 2 ч 14 мин" in text

    asyncio.run(scenario())


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
            await _until(
                pilot,
                lambda: (
                    isinstance(app.screen, ReportScreen)
                    and bool(app.screen.query("#section-output"))
                    and app.screen.query_one("#section-output", Static).size.height > 1
                ),
            )
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
    assert blockers == "none"


def test_report_text_is_rendered_literally_not_as_markup(tmp_path: Path) -> None:
    build_reports_fixture(tmp_path)

    async def scenario() -> tuple[str, str]:
        app = _ReportsHost(tmp_path)
        async with app.run_test() as pilot:
            app.screen.query_one("#filter-role", Input).value = "code-review"
            await _until(pilot, lambda: _report_names(app) == ["dispatch-review"])
            report_list = app.screen.query_one("#report-list", ListView)
            report_list.focus()
            report_list.index = 0
            await pilot.press("enter")
            await _until(
                pilot,
                lambda: isinstance(app.screen, ReportScreen)
                and bool(app.screen.query("#section-output"))
                and bool(app.screen.query("#section-review")),
            )
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
            await _until(pilot, lambda: isinstance(app.screen, ReportScreen))
            await pilot.press("b")
            await _until(
                pilot,
                lambda: isinstance(app.screen, BatchTimelineScreen)
                and bool(app.screen.query("#batch-timeline")),
            )
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
            await _until(
                pilot,
                lambda: isinstance(app.screen, BatchTimelineScreen)
                and bool(app.screen.query("#batch-timeline")),
            )
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
    await _until(pilot, lambda: isinstance(app.screen, ReportScreen))


def test_qa_report_shows_its_gate_log_tail_and_attempts(tmp_path: Path) -> None:
    build_reports_fixture(tmp_path)

    async def scenario() -> str:
        app = _ReportsHost(tmp_path)
        async with app.run_test() as pilot:
            await _open_report(app, pilot, 1)  # dispatch-qa
            await _until(pilot, lambda: bool(app.screen.query("#section-qa-log")))
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
            await _until(
                pilot,
                lambda: isinstance(app.screen, QaLogsScreen)
                and bool(app.screen.query("#qa-logs")),
            )
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
            await _until(pilot, lambda: bool(app.screen.query("#export")))
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
            await _until(pilot, lambda: isinstance(app.screen, BatchTimelineScreen))
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
