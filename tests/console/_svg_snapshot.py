"""SVG snapshots of console screens, driven by Pilot.

A snapshot is `App.export_screenshot()` of a screen at a fixed size, with everything that could vary
between runs fixed: the harness version, the project name, the git branch, the data (injected
views and loaders) and the language. It is compared as text with the file stored next to the tests.
`UPDATE_SNAPSHOTS=1 pytest <test file>` writes the files instead of comparing, and the diff of the
SVGs is then reviewed like any other change.

Every state of a mockup (docs/design/console-mockups/*.html) has a snapshot of both sizes, and the
mockup caption it comes from is written into the SVG title, so a snapshot names its source section."""

from __future__ import annotations

import html
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from textual.app import App
from textual.pilot import Pilot
from textual.screen import Screen
from textual.scrollbar import ScrollBar

from harness.console import brand
from harness.console import data as console_data
from harness.console.app import HARNESS_THEME, HarnessConsoleApp
from harness.console.style import ThinScrollBarRender

SNAPSHOTS = Path(__file__).resolve().parent / "snapshots"
MOCKUPS = Path(__file__).resolve().parents[2] / "docs" / "design" / "console-mockups"
WIDE = (160, 52)
NARROW = (80, 24)
SIZES = (WIDE, NARROW)
PROJECT = "claude-agent-harness"
VERSION = "1.4.6"
BRANCH = "feature/issue-721-console-design-system"
HEAD = "85abd57ef0123456789abcdef0123456789abcde"

Drive = Callable[[Pilot[None], App[None]], Awaitable[None]]


@dataclass(frozen=True)
class Case:
    """One mockup state: the screen to show, how to reach the state, and where the mockup shows it."""

    name: str
    # The mockup captions the state is drawn under, e.g. "6 · Отчёты — список (≈160×52), компактно".
    sections: tuple[str, ...]
    state: str  # the state's row in the mockup's state table, or "" for a section's own picture
    make_screen: Callable[[Path], Screen[object]]
    drive: Drive | None = None
    # Runs after the screenshot, e.g. to let a worker held for the screenshot finish.
    cleanup: Callable[[], None] | None = None


class _SnapshotApp(App[None]):
    """The console's look (its CSS, theme, key hints and scrollbars) around one screen, in place of
    the home screen. It is not a `HarnessConsoleApp` subclass: that app's own `on_mount` would
    still run and push the home screen over the one shown."""

    CSS = HarnessConsoleApp.CSS
    BINDINGS = HarnessConsoleApp.BINDINGS

    def __init__(
        self, repo: Path, make_screen: Callable[[Path], Screen[object]]
    ) -> None:
        super().__init__()
        ScrollBar.renderer = ThinScrollBarRender
        self.repo = repo
        self.language = "ru"
        self.health_counts = None
        self.section_badges: dict[str, tuple[str, str]] = {}
        self._make_screen = make_screen

    def on_mount(self) -> None:
        self.register_theme(HARNESS_THEME)
        self.theme = brand.THEME_NAME
        self.push_screen(self._make_screen(self.repo))


async def settle(pilot: Pilot[None], *, workers: bool = True) -> None:
    """Let the screen finish what it started: pending messages, workers, animations."""
    for _ in range(3):
        await pilot.pause()
        if workers:
            await pilot.app.workers.wait_for_complete()
    await pilot.wait_for_animation()
    await pilot.pause()


async def until(pilot: Pilot[None], ready: Callable[[], bool]) -> None:
    """Pause until `ready()` holds (a worker or an async handler has shown its result)."""
    for _ in range(200):
        if ready():
            return
        await pilot.pause(0.05)
    assert ready(), "the screen did not reach the state to photograph"


async def capture(case: Case, size: tuple[int, int], repo: Path, *, source: str) -> str:
    """The SVG of `case` at `size`; `source` names the mockup file in the title."""
    with (
        patch.dict(os.environ),
        patch.object(console_data, "harness_version", lambda: VERSION),
        patch.object(brand, "current_branch", lambda _repo, **_kw: BRANCH),
    ):
        os.environ.pop("NO_COLOR", None)
        app = _SnapshotApp(repo, case.make_screen)
        # Notifications are off in a test app by default; a toast is part of some states.
        async with app.run_test(size=size, notifications=True) as pilot:
            await settle(pilot)
            if case.drive is not None:
                await case.drive(pilot, app)
                # Not the workers: a state may hold one on purpose (a refresh still running).
                await settle(pilot, workers=False)
            title = f"{source} · {' / '.join(case.sections)}" + (
                f" · {case.state}" if case.state else ""
            )
            svg = app.export_screenshot(title=title)
            if case.cleanup is not None:
                case.cleanup()
            return svg


def snapshot_path(directory: str, case: Case, size: tuple[int, int]) -> Path:
    return SNAPSHOTS / directory / f"{case.name}-{size[0]}x{size[1]}.svg"


def check_snapshot(path: Path, svg: str) -> None:
    """Compare `svg` with the stored file; with `UPDATE_SNAPSHOTS` set, write it instead."""
    if os.environ.get("UPDATE_SNAPSHOTS"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(svg, encoding="utf-8", newline="\n")
        return
    assert path.is_file(), f"{path.name}: no snapshot; run with UPDATE_SNAPSHOTS=1"
    assert (
        path.read_text(encoding="utf-8") == svg
    ), f"{path.name} differs from the screen; review it and run with UPDATE_SNAPSHOTS=1"


def mockup_text(mockup: str, pattern: str) -> list[str]:
    """The texts the regular expression `pattern` captures in a mockup page, in page order."""
    source = (MOCKUPS / mockup).read_text(encoding="utf-8")
    return [html.unescape(found) for found in re.findall(pattern, source)]


def mockup_captions(mockup: str) -> list[str]:
    """The captions of a mockup page: one per picture."""
    return mockup_text(mockup, r'<div class="caption"><span><b>(.+?)</b>')


def mockup_states(mockup: str) -> list[str]:
    """The rows of a mockup page's state table."""
    return mockup_text(mockup, r'<span class="lab2">(.+?)</span>')
