"""The home screen against its accepted mockup, docs/design/console-mockups/home.html: the blocks,
their order, headings, field labels, actions, footer keys and folds, in the wide (≈160×52) and the
narrow (≈80×24) terminal, in Russian and in English. Every fact is injected: no test touches the
real project, the ledger or the network."""

from __future__ import annotations

# ruff: noqa: E402 — optional Textual imports must follow importorskip.

import asyncio
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import pytest

pytest.importorskip("textual")
from textual.widgets import Button, Collapsible, ListView, Static
from textual.widgets._collapsible import CollapsibleTitle
from textual.widgets._footer import FooterKey

from harness.console import brand, reports, task_titles
from harness.console.app import HarnessConsoleApp
from harness.console.data import DashboardData
from harness.console.repo_map import MapEdge, MapFile, RepoMapView
from harness.console.repo_map_settings import MemoryStatus
from harness.console.screens.dashboard import DashboardScreen, HomeContext
from harness.console.shell import Breadcrumbs, JumpLink, SectionCard, ShellSidebar
from harness.health.model import CheckResult
from tests.console._pilot_wait import crumb_text, wait_until

WIDE, NARROW = (160, 52), (80, 24)
BRANCH = "feature/issue-721-console-design-system"
NOW = datetime(2026, 10, 17, 14, 40, tzinfo=UTC)

DATA = DashboardData(
    ok=14,
    warn=0,
    fail=2,
    skipped=11,
    repo_map_tier="Repo Map: tier=full (offline parser bundle detected)",
    harness_version="1.4.6",
    drift_state="drift",
    active_batches=3,
    problems=(
        ("fail", "files.skill_snapshot", "snapshot differs from 1.4.6"),
        ("fail", "files.integrations", "integrations: tracker token missing"),
    ),
    memory_index=CheckResult(
        id="memory.index", group="memory", status="ok", message="индекс валиден (FTS5)"
    ),
)
BANNER = brand.BannerInfo(
    version="1.4.6",
    capabilities=("backend-orchestration",),
    repo="~/Projects/Code/claude-agent-harness",
    branch=BRANCH,
    installed_version="1.4.6",
    installed_commit="85abd57a",
)
CONTEXT = HomeContext(
    repo_map=RepoMapView(
        payload={},
        origin="кэш",
        commit="a" * 40,
        tier="full",
        parser="tree-sitter",
        degradation_reason="",
        estimated_tokens=38400,
        provenance={},
        files=tuple(MapFile(f"f{index}.py", (), "ok") for index in range(412)),
        edges=tuple(MapEdge("f1.py", "f0.py", "import", "high") for _ in range(1860)),
        diagnostics=(),
    ),
    memory=MemoryStatus(True, True, ("adr", "glossary"), 18, top_k=5, max_tokens=1000),
)
STAGES = [
    reports.StageEntry(
        "dispatch-716-a1",
        "batch-716",
        "#716",
        "architect",
        "working",
        "2026-10-17T14:38:00+00:00",
    ),
    reports.StageEntry(
        "dispatch-704-d1",
        "batch-704",
        "#704",
        "developer",
        "blocked",
        "2026-10-17T14:26:00+00:00",
    ),
    reports.StageEntry(
        "dispatch-555-a1",
        "batch-555",
        "#555",
        "architect",
        "dispatched",
        "2026-10-17T14:09:00+00:00",
    ),
]

# The texts of the mockup (and their English), per language.
TEXTS: dict[str, dict[str, str]] = {
    "ru": {
        "crumb": "Главная",
        "title": "Рабочее пространство",
        "attention": " ТРЕБУЕТ ВНИМАНИЯ  2 ",
        "sessions": " РАБОЧИЕ СЕССИИ  3 ",
        "check": "ПРОВЕРКА",
        "mode": "offline · пропущено 11",
        "report": "▸ Подробный технический отчёт",
        "report_summary": "27 проверок · 11 пропущено",
        "context": "▸ Контекст проекта",
        "open": "Открыть другой проект →",
        "open_short": "Проект →",
        "menu": "ГЛАВНОЕ МЕНЮ",
    },
    "en": {
        "crumb": "Home",
        "title": "Workspace",
        "attention": " REQUIRES ATTENTION  2 ",
        "sessions": " WORKING SESSIONS  3 ",
        "check": "CHECK",
        "mode": "offline · skipped 11",
        "report": "▸ Detailed technical report",
        "report_summary": "27 checks · 11 skipped",
        "context": "▸ Project context",
        "open": "Open another project →",
        "open_short": "Project →",
        "menu": "MAIN MENU",
    },
}
MODES: dict[str, list[str]] = {
    "ru": ["Офлайн", "Онлайн"],
    "en": ["Offline", "Online"],
}
JUMPS: dict[str, list[str]] = {
    "ru": ["Обновить Harness  2 →", "Открыть настройку  8 →"],
    "en": ["Update Harness  2 →", "Open setup  8 →"],
}
CARDS: dict[str, list[str]] = {
    "ru": ["КОНТЕКСТ ПРОЕКТА", "HARNESS", "ОРКЕСТРАЦИЯ", "REPO MAP", "ПАМЯТЬ ПРОЕКТА"],
    "en": ["PROJECT CONTEXT", "HARNESS", "ORCHESTRATION", "REPO MAP", "PROJECT MEMORY"],
}


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Git project on the mockup's branch, with three working sessions in its ledger."""
    repo = tmp_path / "claude-agent-harness"
    subprocess.run(
        ["git", "init", "-q", "-b", BRANCH, str(repo)], check=True, capture_output=True
    )
    monkeypatch.setattr(
        reports, "load_ledger_view", lambda _repo: reports.LedgerView(stages=STAGES)
    )
    return repo


def _home(
    repo: Path, collect_titles: task_titles.TaskTitlesLoader | None = None
) -> DashboardScreen:
    return DashboardScreen(
        repo,
        collect_dashboard=lambda _repo, *, online=False: DATA,
        collect_banner=lambda _repo: BANNER,
        collect_context=lambda _repo: CONTEXT,
        now=lambda: NOW,
        collect_titles=collect_titles,
    )


async def _open(
    app: HarnessConsoleApp, pilot: object, repo: Path, language: str
) -> DashboardScreen:
    home = _home(repo)
    await app.switch_screen(home)
    for _ in range(2):
        await pilot.pause()  # type: ignore[attr-defined]
    if language == "en":
        await pilot.press("f2")  # type: ignore[attr-defined]
        for _ in range(2):
            await pilot.pause()  # type: ignore[attr-defined]
    return home


def _shown(widget: object) -> bool:
    return bool(widget.display and widget.region.area)  # type: ignore[attr-defined]


@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize("size", [WIDE, NARROW], ids=["wide", "narrow"])
def test_home_matches_the_mockup_blocks_order_and_headings(
    project: Path, size: tuple[int, int], language: str
) -> None:
    """home.html · Главное окно · широкий терминал (≈160×52) and узкий терминал (≈80×24)."""
    texts = TEXTS[language]
    wide = size == WIDE

    async def scenario() -> None:
        app = HarnessConsoleApp(project)
        async with app.run_test(size=size) as pilot:
            home = await _open(app, pilot, project, language)
            screen_region = home.region

            # Top bar: the project and the branch; on a narrow terminal the branch alone.
            where = str(home.query_one("#topbar-right", Static).render())
            assert where == (
                f"claude-agent-harness · ⎇ {BRANCH}" if wide else f"⎇ {BRANCH}"
            ) or (not wide and where.startswith("⎇ feature/issue-721"))

            # Breadcrumbs: one crumb; the menu toggle opens the row on a narrow terminal.
            assert crumb_text(home.query_one(Breadcrumbs)) == texts["crumb"]
            toggle = home.query_one("#dashboard-menu", Button)
            sidebar = home.query_one(ShellSidebar)
            assert _shown(toggle) != wide and _shown(sidebar) == wide
            if wide:
                heading = sidebar.query_one(".menu-heading", Static)
                assert str(heading.render()) == texts["menu"]

            # The path line ends with the action that opens another project.
            actions = [
                str(button.label)
                for button in home.query(".context-strip Button").results(Button)
                if _shown(button)
            ]
            assert actions == [texts["open"] if wide else texts["open_short"]]

            # The blocks of the page, top to bottom.
            title = home.query_one("#dashboard-title", Static)
            attention = home.query_one("#dashboard-attention-card", SectionCard)
            check = home.query_one("#dashboard-check")
            sessions = home.query_one("#home-sessions-card", SectionCard)
            report = home.query_one("#dashboard-details", Collapsible)
            blocks = [title, attention, check, sessions, report]
            assert [block.region.y for block in blocks] == sorted(
                block.region.y for block in blocks
            )
            for block in blocks:
                assert screen_region.contains_region(block.region), block
            assert str(title.render()) == texts["title"]
            assert str(attention.border_title) == texts["attention"]
            assert str(sessions.border_title) == texts["sessions"]
            assert report.region.y - sessions.region.bottom <= 1

            # Requires attention: every problem with a jump to its section.
            jumps = [str(link.render()) for link in attention.query(JumpLink)]
            assert jumps == (JUMPS[language] if wide else ["2 →", "8 →"])

            # Check: the caption (wide only), the Offline | Online toggle and the mode line.
            label = home.query_one("#dashboard-check-label", Static)
            assert _shown(label) == wide
            if wide:
                assert str(label.render()) == texts["check"]
            modes = [
                str(button.label) for button in check.query(Button).results(Button)
            ]
            assert modes == MODES[language]
            assert home.query_one("#dashboard-offline").has_class("-on")
            mode = str(home.query_one("#dashboard-attention-mode", Static).render())
            assert mode == texts["mode"]

            # Working sessions: one line each; the hint only on a wide terminal.
            rows = list(home.query("#home-sessions > ListItem"))
            assert [row.region.height for row in rows] == [1, 1, 1]
            assert _shown(home.query_one("#home-sessions-hint")) == wide

            # The technical report, folded, with the number of checks at its right end.
            report_title = str(report.query_one(CollapsibleTitle).render())
            assert report.collapsed
            assert report_title.startswith(texts["report"])
            assert report_title.endswith(texts["report_summary"])

            # Project context: cards beside the page, or one fold under the report.
            panel = home.query_one("#dashboard-context")
            fold = panel.query_one(".context-fold", Collapsible)
            if wide:
                cards = [
                    str(card.border_title).strip() for card in panel.query(SectionCard)
                ]
                assert cards == CARDS[language]
                assert panel.region.x >= report.region.right
            else:
                assert fold.collapsed
                fold_title = str(fold.query_one(CollapsibleTitle).render())
                assert fold_title.startswith(texts["context"])
                assert fold.region.y >= report.region.bottom
                assert screen_region.contains_region(fold.region)

            # The footer: the mockup's keys; Esc is not one of them on home.
            keys = {key.key for key in home.query(FooterKey)} - {
                app.COMMAND_PALETTE_BINDING
            }
            assert keys == ({"r", "o", "p", "f1", "f2"} if wide else {"p", "f1", "f2"})

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [WIDE, NARROW], ids=["wide", "narrow"])
def test_home_shows_no_demo_data_without_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: tuple[int, int]
) -> None:
    """With no sessions, no cached map and no memory policy the cards say so: nothing is made up."""
    monkeypatch.setattr(reports, "load_ledger_view", lambda _repo: reports.LedgerView())

    async def scenario() -> tuple[str, int, str]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=size) as pilot:
            home = DashboardScreen(
                tmp_path,
                collect_dashboard=lambda _repo, *, online=False: DATA,
                collect_banner=lambda _repo: brand.BannerInfo(
                    version="1.4.6", capabilities=(), repo="~/x", branch=None
                ),
                collect_context=lambda _repo: HomeContext(),
            )
            await app.switch_screen(home)
            await pilot.pause()
            sessions = home.query_one("#home-sessions-card", SectionCard)
            empty = home.query_one("#home-empty", Static)
            return (
                str(sessions.border_title),
                len(home.query("#home-sessions-hint")),
                str(empty.render()),
            )

    title, hints, empty = asyncio.run(scenario())
    assert title == " РАБОЧИЕ СЕССИИ  0 "
    assert hints == 0
    assert empty.startswith("Активных сессий нет")


def _session_lines(home: DashboardScreen) -> list[str]:
    """Read terminal cells, including Rich truncation, instead of the table's source text."""
    return [
        item.query_one(Static).render_line(0).text
        for item in home.query("#home-sessions > ListItem")
    ]


def test_home_stays_responsive_while_titles_load(project: Path) -> None:
    started, release = Event(), Event()

    def titles(repo: Path, tickets: object) -> dict[str, str]:
        started.set()
        assert release.wait(10), "test did not release title reader"
        return {"#716": "Тикет"}

    async def scenario() -> None:
        app = HarnessConsoleApp(project)
        try:
            async with app.run_test(size=WIDE) as pilot:
                home = _home(project, titles)
                await app.switch_screen(home)
                await wait_until(pilot, started.is_set)

                assert "dispatch-716-a1" in _session_lines(home)[0]
                await pilot.press("f2")
                assert "Workspace" in str(
                    home.query_one("#dashboard-title", Static).render()
                )
                release.set()
                await wait_until(pilot, lambda: "Тикет" in _session_lines(home)[0])
        finally:
            release.set()

    asyncio.run(scenario())


@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize("size", [WIDE, NARROW], ids=["wide", "narrow"])
def test_session_titles_follow_terminal_width_and_keep_their_language(
    project: Path, size: tuple[int, int], language: str
) -> None:
    async def scenario() -> None:
        app = HarnessConsoleApp(project)
        async with app.run_test(size=size) as pilot:
            home = _home(project, lambda repo, tickets: {"#716": "Я [bold]"})
            await app.switch_screen(home)
            await app.workers.wait_for_complete()
            if language == "en":
                await pilot.press("f2")
            await pilot.pause()

            lines = _session_lines(home)
            assert "dispatch-716-a1" in lines[0]
            if size == WIDE:
                assert "dispatch-716-a1 · Я [bold]" in lines[0]
            else:
                assert "Я [bold]" not in lines[0]
                assert " · " not in lines[0]
            assert " · " not in lines[1]
            assert [
                item.region.height for item in home.query("#home-sessions > ListItem")
            ] == [1, 1, 1]
            assert "▶" in lines[0]
            assert ("2 мин" if language == "ru" else "2 min") in lines[0]

    asyncio.run(scenario())


def test_long_and_multiline_titles_stay_on_one_terminal_line(project: Path) -> None:
    async def scenario() -> None:
        app = HarnessConsoleApp(project)
        async with app.run_test(size=WIDE) as pilot:
            home = _home(
                project,
                lambda repo, tickets: {
                    "#716": "Очень длинное название " * 20,
                    "#704": "А\nБ\tВ\x1bГ\x00Д",
                    "#555": " \n\t ",
                },
            )
            await app.switch_screen(home)
            await app.workers.wait_for_complete()
            await pilot.pause()

            lines = _session_lines(home)
            assert "dispatch-716-a1 · Очень" in lines[0]
            assert "…" in lines[0]
            assert "▶ Выполняется" in lines[0] and "2 мин" in lines[0]
            assert "dispatch-704-d1 · А Б В Г Д" in lines[1]
            assert " · " not in lines[2]
            for item in home.query("#home-sessions > ListItem"):
                widget = item.query_one(Static)
                assert item.region.height == 1
                assert widget.content_size.height == 1

    asyncio.run(scenario())


def test_real_dispatch_ids_leave_room_for_the_task_title(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stages = [
        replace(
            STAGES[0],
            dispatch_id="dispatch-e98a4a72-50ac-4fd8-b421-1db55c17f287",
        )
    ]
    monkeypatch.setattr(
        reports, "load_ledger_view", lambda repo: reports.LedgerView(stages=stages)
    )

    async def scenario() -> None:
        app = HarnessConsoleApp(project)
        async with app.run_test(size=WIDE) as pilot:
            home = _home(project, lambda repo, tickets: {"#716": "Тикет"})
            await app.switch_screen(home)
            await app.workers.wait_for_complete()
            await pilot.pause()

            [line] = _session_lines(home)
            assert "dispatch-e98" in line
            assert "… · Тикет" in line
            assert "▶ Выполняется" in line and "2 мин" in line

    asyncio.run(scenario())


def test_resize_language_and_offline_checks_reuse_titles(project: Path) -> None:
    calls: list[object] = []

    def titles(repo: Path, tickets: object) -> dict[str, str]:
        calls.append(tickets)
        return {"#716": "Тикет"}

    async def scenario() -> None:
        app = HarnessConsoleApp(project)
        async with app.run_test(size=WIDE) as pilot:
            home = _home(project, titles)
            await app.switch_screen(home)
            await app.workers.wait_for_complete()
            await pilot.pause()
            sessions = home.query_one("#home-sessions", ListView)
            sessions.index = 1
            sessions.focus()

            await pilot.resize_terminal(*NARROW)
            await pilot.pause()
            assert "Тикет" not in _session_lines(home)[0]
            await pilot.press("f2", "r")
            await app.workers.wait_for_complete()
            await pilot.resize_terminal(*WIDE)
            await pilot.pause()

            assert "dispatch-716-a1 · Тикет" in _session_lines(home)[0]
            assert "▶ Running" in _session_lines(home)[0]
            assert sessions.index == 1 and home.focused is sessions
            assert len(calls) == 1

    asyncio.run(scenario())


def test_a_late_title_response_cannot_replace_a_newer_pass(project: Path) -> None:
    starts = [Event(), Event()]
    releases = [Event(), Event()]
    old_done = Event()
    calls: list[object] = []

    def titles(repo: Path, tickets: object) -> dict[str, str]:
        index = len(calls)
        calls.append(tickets)
        starts[index].set()
        assert releases[index].wait(10)
        if index == 0:
            old_done.set()
            return {"#716": "Старое"}
        return {"#716": "Новое"}

    async def scenario() -> None:
        app = HarnessConsoleApp(project)
        try:
            async with app.run_test(size=WIDE) as pilot:
                home = _home(project, titles)
                await app.switch_screen(home)
                await wait_until(pilot, starts[0].is_set)
                await pilot.press("o")
                await wait_until(pilot, starts[1].is_set)
                releases[1].set()
                await wait_until(pilot, lambda: "Новое" in _session_lines(home)[0])
                releases[0].set()
                await wait_until(pilot, old_done.is_set)
                await pilot.pause()

                assert "Новое" in _session_lines(home)[0]
                assert "Старое" not in _session_lines(home)[0]
        finally:
            for release in releases:
                release.set()

    asyncio.run(scenario())


def test_failed_online_refresh_removes_the_previous_title(project: Path) -> None:
    calls: list[object] = []

    def titles(repo: Path, tickets: object) -> dict[str, str]:
        calls.append(tickets)
        if len(calls) == 1:
            return {"#716": "Тикет"}
        raise OSError("private CLI diagnostic")

    async def scenario() -> None:
        app = HarnessConsoleApp(project)
        async with app.run_test(size=WIDE) as pilot:
            home = _home(project, titles)
            await app.switch_screen(home)
            await wait_until(pilot, lambda: "Тикет" in _session_lines(home)[0])
            await pilot.press("o")
            await app.workers.wait_for_complete()
            await pilot.pause()

            line = _session_lines(home)[0]
            assert "dispatch-716-a1" in line
            assert "Тикет" not in line and " · " not in line
            assert "private CLI diagnostic" not in app.export_screenshot()

    asyncio.run(scenario())


def test_departed_home_cannot_apply_its_title_response(project: Path) -> None:
    started, release, finished = Event(), Event(), Event()

    def titles(repo: Path, tickets: object) -> dict[str, str]:
        started.set()
        assert release.wait(10)
        finished.set()
        return {"#716": "Старое"}

    async def scenario() -> None:
        app = HarnessConsoleApp(project)
        try:
            async with app.run_test(size=WIDE) as pilot:
                old_home = _home(project, titles)
                await app.switch_screen(old_home)
                await wait_until(pilot, started.is_set)
                home = _home(project, lambda repo, tickets: {"#716": "Новое"})
                await app.switch_screen(home)
                await wait_until(pilot, lambda: "Новое" in _session_lines(home)[0])
                release.set()
                await wait_until(pilot, finished.is_set)
                await pilot.pause()

                assert old_home not in app.screen_stack
                assert app.screen is home
                assert "Новое" in _session_lines(home)[0]
                assert "Старое" not in _session_lines(home)[0]
        finally:
            release.set()

    asyncio.run(scenario())
