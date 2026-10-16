"""Layout invariants of the console's workspace screens, measured on the real Textual layout.

One helper, `layout_problems`, walks the mounted widget tree of a screen and reports where it breaks
the shell's contract: a widget sticks out of the screen, a widget sticks out of a container that does
not scroll, two siblings overlap, or a title loses its left padding. A screen is checked at the three
terminal sizes the shell is designed for (160x52, 120x40, 80x24), in Russian and in English."""

from __future__ import annotations

# ruff: noqa: E402 — optional Textual imports must follow importorskip.

import asyncio
from collections.abc import Iterable
from typing import cast
from pathlib import Path

import pytest

pytest.importorskip("textual")
from textual.geometry import Region
from textual.screen import Screen
from textual.widget import Widget

from harness.console import reports
from harness.console.app import HarnessConsoleApp
from harness.console.screens.dashboard import DashboardScreen

SIZES = [(160, 52), (120, 40), (80, 24)]
LANGUAGES = ["ru", "en"]
# Titles keep this much space between the content edge of their column and the first letter.
TITLE_PADDING = 2
# The containers a title is measured against (the shell's content column and its side panel).
TITLE_OWNERS = ("shell-content", "context-panel", "shell-main")


def _shown(widget: Widget) -> bool:
    return all(node.display for node in widget.ancestors_with_self) and bool(
        widget.region.area
    )


def _scrolls(widget: Widget) -> bool:
    return widget.max_scroll_y > 0 or widget.virtual_size.height > widget.size.height


def _name(widget: Widget) -> str:
    return (
        f"{type(widget).__name__}#{widget.id}" if widget.id else type(widget).__name__
    )


def _is_overlay(widget: Widget) -> bool:
    return widget.styles.overlay == "screen" or widget.styles.dock != "none"


def _shown_widgets(screen: Screen[None]) -> list[Widget]:
    return [
        widget
        for widget in screen.walk_children()
        if isinstance(widget, Widget) and _shown(widget)
    ]


def _owner(widget: Widget) -> Widget | None:
    for node in widget.ancestors:
        if isinstance(node, Widget) and any(
            node.has_class(name) for name in TITLE_OWNERS
        ):
            return node
    return None


def layout_problems(screen: Screen[None], titles: Iterable[str] = ()) -> list[str]:
    """Every layout defect found on the mounted `screen`, one readable line each."""
    problems: list[str] = []
    bounds = Region(0, 0, screen.size.width, screen.size.height)
    widgets = _shown_widgets(screen)
    for widget in widgets:
        region = widget.region
        parent = widget.parent
        scrolled_y = any(
            _scrolls(node)
            for node in widget.ancestors
            if isinstance(node, Widget) and not isinstance(node, Screen)
        )
        if region.x < 0 or region.right > bounds.right:
            problems.append(f"{_name(widget)} sticks out of the screen: {region}")
        elif not scrolled_y and region.bottom > bounds.bottom:
            problems.append(f"{_name(widget)} sticks out of the screen: {region}")
        if (
            isinstance(parent, Widget)
            and not isinstance(parent, Screen)
            and not _is_overlay(widget)
            and not _scrolls(parent)
            and parent.region.area
            and not parent.region.contains_region(region)
        ):
            problems.append(f"{_name(widget)} sticks out of {_name(parent)}")
    for container in [screen, *widgets]:
        siblings = [
            child
            for child in container.children
            if _shown(child) and not _is_overlay(child)
        ]
        for index, first in enumerate(siblings):
            for second in siblings[index + 1 :]:
                if first.region.overlaps(second.region):
                    problems.append(
                        f"{_name(first)} {first.region} overlaps {_name(second)} {second.region}"
                    )
    for selector in titles:
        for title in screen.query(selector):
            if not _shown(title):
                continue
            owner = _owner(title)
            if owner is None:
                continue
            gap = title.content_region.x - owner.region.x
            if gap < TITLE_PADDING:
                problems.append(
                    f"title {_name(title)} keeps {gap} columns of left padding in {_name(owner)}"
                )
    return problems


@pytest.fixture
def sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    stages = [
        reports.StageEntry(
            f"dispatch-e98a4a72-50ac-4fd8-b421-1db55c17f28{index}",
            "batch-layout",
            f"#72{index}",
            role,
            state,
            f"2026-10-1{index}",
        )
        for index, (role, state) in enumerate(
            [
                ("architect", "working"),
                ("developer", "working"),
                ("code-review", "dispatched"),
                ("qa", "approved"),
            ]
        )
    ]
    monkeypatch.setattr(
        reports, "load_ledger_view", lambda _repo: reports.LedgerView(stages=stages)
    )


DASHBOARD_TITLES = (
    "#dashboard-title",
    "#home-sessions-title",
    ".card-title",
    ".context-label",
    ".breadcrumbs",
)


async def last_card_is_reachable(
    screen: Screen[None], pilot: object, selector: str = ".context-panel"
) -> list[str]:
    """Scroll the context panel's last card into view: it must then lie whole inside the panel and
    the screen, so a reader can always reach it."""
    problems: list[str] = []
    for panel in screen.query(selector):
        if not _shown(panel) or not panel.children:
            continue
        last = panel.children[-1]
        last.scroll_visible(animate=False, immediate=True)
        await pilot.pause()  # type: ignore[attr-defined]
        visible = panel.content_region.intersection(screen.region)
        if not visible.contains_region(last.region):
            problems.append(
                f"{_name(last)} {last.region} is cut off by {_name(panel)} {visible}"
            )
    return problems


async def _open(
    app: HarnessConsoleApp, pilot: object, screen: Screen[None] | None, language: str
) -> Screen[None]:
    if screen is not None:
        await app.push_screen(screen)
    if language != app.language:
        app.action_toggle_language()
    for _ in range(3):
        await pilot.pause()  # type: ignore[attr-defined]
    await app.workers.wait_for_complete()
    await pilot.pause()  # type: ignore[attr-defined]
    return cast(Screen[None], app.screen)


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("size", SIZES)
def test_dashboard_layout(
    tmp_path: Path, sessions: None, size: tuple[int, int], language: str
) -> None:
    async def scenario() -> list[str]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=size) as pilot:
            screen = await _open(app, pilot, None, language)
            assert isinstance(screen, DashboardScreen)
            return layout_problems(
                screen, DASHBOARD_TITLES
            ) + await last_card_is_reachable(screen, pilot)

    assert asyncio.run(scenario()) == []


ORCHESTRATION_TITLES = (".breadcrumbs", "#orchestration-title")


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("size", SIZES)
def test_orchestration_layout(
    tmp_path: Path, size: tuple[int, int], language: str
) -> None:
    from textual.widgets import Button, ListView

    from harness.console.screens.orchestration import OrchestrationScreen
    from tests.console._console_ledger_fixture import build_reports_fixture

    build_reports_fixture(tmp_path)

    async def scenario() -> tuple[list[str], int, str]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=size) as pilot:
            screen = await _open(app, pilot, OrchestrationScreen(tmp_path), language)
            header = screen.query_one("#orchestration-header")
            button = screen.query_one("#worker-sessions", Button)
            assert header.region.contains_region(button.region)
            problems = layout_problems(screen, ORCHESTRATION_TITLES)
            menu = screen.query_one("#command-menu", ListView)
            menu.scroll_visible(animate=False, immediate=True)
            await pilot.pause()
            return problems, menu.region.height, str(button.label)

    problems, menu_height, label = asyncio.run(scenario())
    assert problems == []
    assert menu_height >= 8  # the command list stays usable on the smallest terminal
    assert label.endswith(" · 0")
