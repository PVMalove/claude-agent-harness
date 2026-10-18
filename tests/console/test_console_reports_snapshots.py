"""SVG snapshots of the Reports screens: one pair (160x52 and 80x24) for every state of the accepted
mockup docs/design/console-mockups/reports.html. The data is fixed and injected, so a snapshot
changes only when the screen does; see tests/console/_svg_snapshot.py to regenerate them."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import cast

import pytest

from harness.orchestration.ledger.lifecycle import JsonObject, JsonValue
from textual.app import App
from textual.pilot import Pilot
from textual.screen import Screen
from textual.widgets import Button, Input, ListView

from harness.console.reports import (
    BatchSummary,
    BatchTimeline,
    LedgerView,
    QaAttempt,
    QaRun,
    ReportEntry,
    StageEntry,
    TimelineEvent,
)
from harness.console.screens import reports as reports_screens
from harness.console.screens.reports import (
    BatchTimelineScreen,
    QaLogsScreen,
    ReportScreen,
    ReportsScreen,
)
from tests.console._svg_snapshot import (
    PROJECT,
    SIZES,
    Case,
    capture,
    check_snapshot,
    mockup_captions,
    mockup_states,
    snapshot_path,
    until,
)

MOCKUP = "reports.html"
DIRECTORY = "reports"
LIST = "6 · Отчёты — список (≈160×52), компактно"
REPORT = "6а · Отчёты → Отчёт (≈160×52)"
TIMELINE = "6б · Отчёты → Хронология batch (≈160×52)"
QA_LOGS = "6в · Отчёты → QA-логи (F3)"
STATES = "Состояния экрана «Отчёты»"
NARROW_LIST = "6 · Отчёты — узкий терминал (≈80×24)"

BATCH = "batch-698fa45a-70a2-497c-9086-7ae0cb7c986e"
DISPATCH = "dispatch-555837f4-5664-47fc-874c-3e01f5def6af"
REVIEW_OUTPUT = (
    "Ревью диффа 799f11d..34cbdc6 (8 коммитов, 23 файла), файлы не менялись. Проверено: "
    "hex-литералов вне brand.py нет; один SHELL_CSS; пять видов статуса, символ плюс текст; "
    "выбор пункта меню команд не запускает. Находки только уровня info."
)


def _report(
    dispatch: str,
    batch: str,
    ticket: str,
    role: str,
    outcome: str,
    at: str,
    **extra: JsonValue,
) -> ReportEntry:
    body: JsonObject = {
        "output": f"{role}: итог по {ticket}",
        "checks_run": [],
        "risks": {"status": "none", "details": ""},
        "blockers": {"status": "none", "details": ""},
        "next_coordinator_action": "accept",
        **extra,
    }
    return ReportEntry(dispatch, batch, ticket, role, outcome, at, body)


def _code_review_report() -> ReportEntry:
    clean: JsonObject = {"severity": "none", "findings": []}
    return _report(
        DISPATCH,
        BATCH,
        "#721",
        "code-review",
        "completed",
        "2026-10-16T12:59:00+00:00",
        output=REVIEW_OUTPUT,
        checks_run=[
            {"command": "make lint", "result": "pass", "evidence": ""},
            {
                "command": "pytest -q tests/orchestration tests/memory",
                "result": "pass",
                "evidence": "1577 passed, 2 skipped",
            },
        ],
        review={"candidate_commit": "34cbdc6", "standards": clean, "spec": clean},
        next_coordinator_action="accept; затем QA.",
    )


def _stages() -> list[StageEntry]:
    def done(report: ReportEntry) -> StageEntry:
        return StageEntry(
            report.dispatch_id,
            report.batch_id,
            report.ticket,
            report.role,
            "reported",
            report.reported_at,
            report,
        )

    review = _code_review_report()
    developer = _report(
        "dispatch-e98a4a72-0001",
        BATCH,
        "#721",
        "developer",
        "completed",
        "2026-10-16T11:44:00+00:00",
    )
    architect = _report(
        "dispatch-ae7067fe-0002",
        BATCH,
        "#721",
        "architect",
        "completed",
        "2026-10-16T11:30:00+00:00",
    )
    blocked = _report(
        "dispatch-5a2c7e11-0003",
        "batch-5a2c7e11",
        "#556",
        "developer",
        "blocked",
        "2026-10-15T18:20:00+00:00",
        blockers={"status": "present", "details": "нет токена трекера в окружении"},
    )
    older = _report(
        "dispatch-9f0e1d2c-0004",
        "batch-504d9b14",
        "#704",
        "architect",
        "completed",
        "2026-10-15T16:02:00+00:00",
    )
    failed = _report(
        "dispatch-c0a4be72-0005",
        "batch-c0a4be72",
        "#716",
        "publish",
        "failed",
        "2026-10-14T19:12:00+00:00",
    )
    working = StageEntry(
        "dispatch-d1c2b3a4-0006",
        "batch-1d3e9c40",
        "#715",
        "qa",
        "working",
        "2026-10-16T09:10:00+00:00",
    )
    return [
        done(review),
        done(developer),
        done(architect),
        working,
        done(blocked),
        done(older),
        done(failed),
    ]


PR = "github.com/PVMalove/claude-agent-harness/pull/729"


def _batches() -> list[BatchSummary]:
    def batch(
        identifier: str, ticket: str, state: str, start: str, pr: str = "", **extra: str
    ) -> BatchSummary:
        return BatchSummary(
            f"batch-{identifier}",
            ticket,
            state,
            start,
            branch="feature/x",
            pr_created_at=pr,
            pr_url=PR if pr and identifier == "698fa45a" else "",
            pr_state="found" if pr else extra.get("pr_state", "missing"),
        )

    return [
        batch(
            "698fa45a",
            "#721",
            "completed",
            "2026-10-16T11:22:00+00:00",
            "2026-10-16T13:33:00+00:00",
        ),
        batch("1d3e9c40", "#715", "active", "2026-10-16T08:40:00+00:00"),
        batch(
            "5a2c7e11",
            "#556",
            "blocked",
            "2026-10-15T17:03:00+00:00",
            pr_state="unknown",
        ),
        batch("504d9b14", "#704", "active", "2026-10-15T15:49:00+00:00"),
        batch(
            "c0a4be72",
            "#716",
            "completed",
            "2026-10-14T19:12:00+00:00",
            "2026-10-14T20:59:00+00:00",
        ),
    ]


def _view() -> LedgerView:
    stages = _stages()
    return LedgerView(
        reports=[stage.report for stage in stages if stage.report is not None],
        batches=_batches(),
        qa_runs=[
            QaRun(
                artifact="qa-artifacts/104ce6b5" + "0" * 56 + ".log",
                logged_at="2026-10-16T13:23:00+00:00",
                dispatch_id="dispatch-d8222613-0007",
                ticket="#721",
                outcome="completed",
                commands=[("make bootstrap", 0), ("make verify", 0)],
                tail=[
                    "$ make verify",
                    "mypy: no issues found in 391 source files",
                    "3102 passed, 2 skipped",
                    "clean-room: passed",
                    "agent-harness verification passed",
                ],
                total_lines=412,
            )
        ],
        qa_attempts=[
            QaAttempt(
                "dispatch-d8222613-0007",
                "preparation",
                "2026-10-16T13:20:00+00:00",
                "ledger is locked by another operation",
            ),
            QaAttempt(
                "dispatch-4be2c9a1-0008",
                "preparation",
                "2026-10-15T17:41:00+00:00",
                "timeout",
            ),
        ],
        stages=stages,
    )


def _timeline() -> BatchTimeline:
    def event(at: str, kind: str, text: str) -> TimelineEvent:
        return TimelineEvent(f"2026-10-16T{at}:00+00:00", kind, text)

    return BatchTimeline(
        batch=_batches()[0],
        role_flow=["architect", "developer", "code-review", "qa", "publish"],
        events=[
            event("11:22", "state", "батч создан"),
            event("11:23", "state", "состояние: planned → awaiting-approval → active"),
            event("11:24", "dispatch", "architect: dispatch-ae7067fe, итог: reported"),
            event("11:30", "report", "отчёт architect: completed"),
            event("11:31", "decision", "решение coordinator: accept (policy:auto)"),
            event("11:32", "dispatch", "developer: dispatch-e98a4a72, итог: reported"),
            event("12:26", "report", "отчёт developer: completed"),
            event("12:27", "risk", "оценка риска: 4 триггера; нужен review"),
            event(
                "12:30",
                "dispatch",
                "code-review (review): dispatch-555837f4, итог: reported",
            ),
            event("13:07", "report", "отчёт code-review: completed"),
            event("13:23", "report", "отчёт qa: completed"),
            event("13:26", "report", "отчёт publish: completed"),
            event("13:27", "state", "состояние: active → completed"),
        ],
    )


def _waiting_timeline() -> BatchTimeline:
    batch = _batches()[1]
    return BatchTimeline(
        batch=batch,
        role_flow=["qa"],
        events=[
            TimelineEvent("2026-10-16T08:40:00+00:00", "state", "батч создан"),
            TimelineEvent(
                "2026-10-16T09:10:00+00:00",
                "dispatch",
                "qa: dispatch-d1c2b3a4, итог: working",
            ),
        ],
    )


def _list(repo: Path, view: LedgerView | None = None) -> Screen[object]:
    shown = view or _view()
    return cast(Screen[object], ReportsScreen(repo, load_view=lambda _repo: shown))


async def _type(pilot: Pilot[None], app: App[None], selector: str, value: str) -> None:
    app.screen.query_one(selector, Input).value = value
    await pilot.pause()


async def _filter_nothing(pilot: Pilot[None], app: App[None]) -> None:
    await _type(pilot, app, "#filter-ticket", "#000")
    await until(pilot, lambda: app.screen.query_one("#reports-empty").display)


class _Refresh:
    """A loader whose second call (the refresh) waits until the test lets it finish, or fails."""

    def __init__(self, view: LedgerView, *, fail: bool = False) -> None:
        self.view, self.fail, self.calls = view, fail, 0
        self.release = threading.Event()

    def __call__(self, _repo: Path) -> LedgerView:
        self.calls += 1
        if self.calls == 1:
            return self.view
        if self.fail:
            raise RuntimeError("ledger is locked by another operation")
        self.release.wait(timeout=30)
        return self.view


_HELD: list[_Refresh] = []


def _refreshing(repo: Path) -> Screen[object]:
    loader = _Refresh(_view())
    _HELD.append(loader)
    return cast(Screen[object], ReportsScreen(repo, load_view=loader))


def _release_held() -> None:
    while _HELD:
        _HELD.pop().release.set()


def _click_refresh(app: App[None]) -> None:
    """The press of the refresh button without `Button.press()`: that one lights the button for a
    moment, and a snapshot must not depend on when it is taken."""
    button = app.screen.query_one("#refresh-reports", Button)
    button.post_message(Button.Pressed(button))


async def _press_refresh(pilot: Pilot[None], app: App[None]) -> None:
    _click_refresh(app)
    screen = cast(ReportsScreen, app.screen)
    await until(pilot, lambda: screen.refreshing or bool(screen.refresh_error))


async def _press_refresh_that_fails(pilot: Pilot[None], app: App[None]) -> None:
    _click_refresh(app)
    screen = cast(ReportsScreen, app.screen)
    await until(pilot, lambda: bool(screen.refresh_error))
    await until(pilot, lambda: not screen.refreshing)


async def _open_stage_without_timeline(pilot: Pilot[None], app: App[None]) -> None:
    listing = app.screen.query_one("#report-list", ListView)
    listing.focus()
    listing.index = (
        3  # the stage that has no report and whose batch the ledger does not know
    )
    await pilot.press("enter")
    await pilot.pause()


async def _export(pilot: Pilot[None], app: App[None]) -> None:
    await pilot.press("e")
    await pilot.pause()


def _exporting_report(repo: Path) -> Screen[object]:
    return cast(Screen[object], ReportScreen(_code_review_report(), _view(), repo))


@pytest.fixture(autouse=True)
def fixed_export_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The export notification names a file whose name carries the time; the snapshot fixes it."""

    def fake_export(screen: Screen[object], _repo: Path, _document: object) -> None:
        screen.notify(
            "экспортировано: docs/tasks/console-exports/2026-10-16-report-dispatch-555837f4.md"
        )

    monkeypatch.setattr(reports_screens, "export_document", fake_export)


CASES = (
    Case("6-list", (LIST, NARROW_LIST), "", _list),
    Case(
        "6a-report",
        (REPORT,),
        "",
        lambda repo: cast(
            Screen[object], ReportScreen(_code_review_report(), _view(), repo)
        ),
    ),
    Case(
        "6b-timeline",
        (TIMELINE,),
        "",
        lambda repo: cast(Screen[object], BatchTimelineScreen(_timeline(), repo)),
    ),
    Case(
        "6c-qa-logs",
        (QA_LOGS,),
        "",
        lambda _repo: cast(Screen[object], QaLogsScreen(_view())),
    ),
    Case(
        "state-ledger-unavailable",
        (STATES,),
        "Ledger недоступен",
        lambda repo: _list(
            repo,
            LedgerView(
                unavailable="леджер оркестрации не найден или не инициализирован"
            ),
        ),
    ),
    Case("state-nothing-found", (STATES,), "Ничего не найдено", _list, _filter_nothing),
    Case(
        "state-refreshing",
        (STATES,),
        "Идёт обновление",
        _refreshing,
        _press_refresh,
        _release_held,
    ),
    Case(
        "state-refresh-error",
        (STATES,),
        "Ошибка обновления",
        lambda repo: cast(
            Screen[object], ReportsScreen(repo, load_view=_Refresh(_view(), fail=True))
        ),
        _press_refresh_that_fails,
    ),
    Case(
        "state-stage-without-report",
        (STATES,),
        "Стадия без отчёта",
        lambda repo: cast(
            Screen[object],
            BatchTimelineScreen(
                _waiting_timeline(),
                repo,
                notice="dispatch-d1c2b3a4-0006 · working: отчёт ещё не получен",
            ),
        ),
    ),
    Case(
        "state-timeline-unavailable",
        (STATES,),
        "Хронология недоступна",
        _list,
        _open_stage_without_timeline,
    ),
    Case("state-export", (STATES,), "Экспорт", _exporting_report, _export),
)


def test_every_mockup_caption_and_state_has_a_snapshot_pair() -> None:
    captions = mockup_captions(MOCKUP)
    assert captions, "no captions parsed from the mockup"
    covered = {section for case in CASES for section in case.sections}
    assert set(captions) == covered
    states = mockup_states(MOCKUP)
    assert states, "no state rows parsed from the mockup"
    assert {case.state for case in CASES if case.state} == set(states)
    for case in CASES:
        for size in SIZES:
            assert snapshot_path(
                DIRECTORY, case, size
            ).is_file(), f"{case.name} {size[0]}x{size[1]}: no snapshot"


@pytest.mark.parametrize("size", SIZES, ids=[f"{w}x{h}" for w, h in SIZES])
@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_screen_matches_its_snapshot(
    case: Case, size: tuple[int, int], tmp_path: Path
) -> None:
    repo = tmp_path / PROJECT
    (repo / "docs").mkdir(parents=True)
    svg = asyncio.run(capture(case, size, repo, source=MOCKUP))
    check_snapshot(snapshot_path(DIRECTORY, case, size), svg)
