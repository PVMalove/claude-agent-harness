"""SVG snapshots of the Repo Map screen and its settings pane: one pair (160x52 and 80x24) for every
state of the accepted mockup docs/design/console-mockups/repo-map.html. The HEAD, the map, the
parsers and the project are fixed and injected, so a snapshot changes only when the screen does; see
tests/console/_svg_snapshot.py to regenerate them."""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import pytest
from textual.app import App
from textual.pilot import Pilot
from textual.screen import Screen
from textual.widgets import Button, TabbedContent, Tree

from harness.console import repo_map as console_repo_map
from harness.console.repo_map import RepoMapView
from harness.console.screens.repo_map import RepoMapScreen
from harness.repo_map.bundle_fetch import ParserStatus
from tests.console._console_repo_map_fixture import HUB, map_payload
from tests.console._svg_snapshot import (
    HEAD,
    PROJECT,
    SIZES,
    Case,
    Drive,
    capture,
    check_snapshot,
    mockup_captions,
    mockup_states,
    snapshot_path,
    until,
)
from tests.console.test_console_repo_map_settings_app import NO_BUNDLE, _git_project

MOCKUP = "repo-map.html"
DIRECTORY = "repo_map"
FILES = "7 · Repo Map — вкладка «Файлы» (≈160×52), компактно"
SETTINGS = "7а · Repo Map — вкладка «Настройки» (≈160×52)"
OTHER = "Остальные вкладки"
CONFIRM = "Подтверждение сборки"
STATES = "Состояния Repo Map"
NARROW = "7 · Repo Map — узкий терминал (≈80×24)"

READY = ParserStatus(
    Path("/registry"), "cp312-linux_x86_64", "local-cache", ("python",), None
)


def _view() -> RepoMapView:
    view = console_repo_map.parse_map(map_payload(HEAD), origin="кэш")
    assert not isinstance(view, str)
    return view


class _Runner:
    """The map CLI: answers with the fixed map, fails, or waits until the test lets it finish."""

    def __init__(self, *, fail: bool = False, hold: bool = False) -> None:
        self.fail, self.hold = fail, hold
        self.release = threading.Event()

    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> "subprocess.CompletedProcess[str]":
        if self.hold:
            self.release.wait(timeout=30)
        if self.fail:
            return subprocess.CompletedProcess(
                list(argv), 2, "", "parser worker crashed"
            )
        return subprocess.CompletedProcess(
            list(argv), 0, json.dumps(map_payload(HEAD)), ""
        )


def _screen(
    repo: Path,
    *,
    head: str | None = HEAD,
    cached: bool = True,
    parsers: ParserStatus = READY,
    runner: _Runner | None = None,
) -> Screen[object]:
    """The Repo Map screen with the fixed HEAD, map and parsers injected."""
    return cast(
        Screen[object],
        RepoMapScreen(
            repo,
            command_runner=runner or _Runner(),
            find_head=lambda _repo: head,
            load_cached=lambda _repo, _head: _view() if cached else None,
            load_parsers=lambda _repo: parsers,
        ),
    )


def _project(repo: Path, *, orchestration: bool = True) -> Screen[object]:
    """The screen over a project that has a policy file, for the settings pane."""
    _git_project(repo, orchestration=orchestration)
    return _screen(repo, parsers=NO_BUNDLE)


async def _tab(pilot: Pilot[None], app: App[None], pane: str) -> None:
    tabs = app.screen.query_one("#repo-map-tabs", TabbedContent)
    tabs.active = pane
    await until(pilot, lambda: tabs.active == pane)
    await pilot.app.workers.wait_for_complete()


async def _files(pilot: Pilot[None], app: App[None]) -> None:
    await _tab(pilot, app, "tab-files")
    tree: Tree[str] = app.screen.query_one("#file-tree", Tree)
    tree.root.expand_all()
    await pilot.pause()
    core = next(node for node in tree.root.children[0].children if node.data == HUB)
    tree.select_node(core)
    await pilot.pause()


def _tab_driver(pane: str) -> Drive:
    async def drive(pilot: Pilot[None], app: App[None]) -> None:
        await _tab(pilot, app, pane)

    return drive


async def _confirm_dialog(pilot: Pilot[None], app: App[None]) -> None:
    await pilot.press("b")
    await until(pilot, lambda: type(app.screen).__name__ == "BuildConfirmScreen")


def _click(app: App[None], selector: str) -> None:
    """The press of a button without `Button.press()`: that one lights the button for a moment,
    and a snapshot must not depend on when it is taken."""
    button = app.screen.query_one(selector, Button)
    button.post_message(Button.Pressed(button))


async def _build(pilot: Pilot[None], app: App[None], kind: str) -> None:
    await _confirm_dialog(pilot, app)
    _click(app, "#confirm-build")
    await until(pilot, lambda: type(app.screen).__name__ == "RepoMapScreen")
    screen = cast(RepoMapScreen, app.screen)
    await until(pilot, lambda: screen._status_kind == kind)


async def _building(pilot: Pilot[None], app: App[None]) -> None:
    await _build(pilot, app, "running")


async def _build_failed(pilot: Pilot[None], app: App[None]) -> None:
    await _build(pilot, app, "error")


async def _built(pilot: Pilot[None], app: App[None]) -> None:
    await _build(pilot, app, "accepted")


_HELD: list[_Runner] = []


def _holding(repo: Path) -> Screen[object]:
    runner = _Runner(hold=True)
    _HELD.append(runner)
    return _screen(repo, cached=False, runner=runner)


def _release_held() -> None:
    while _HELD:
        _HELD.pop().release.set()


CASES = (
    Case(
        "7-files",
        (FILES, NARROW),
        "",
        lambda repo: _screen(repo),
        _files,
    ),
    Case(
        "7a-settings",
        (SETTINGS,),
        "",
        lambda repo: _project(repo),
        _tab_driver("tab-settings"),
    ),
    Case("other-summary", (OTHER,), "", lambda repo: _screen(repo)),
    Case(
        "other-hubs", (OTHER,), "", lambda repo: _screen(repo), _tab_driver("tab-hubs")
    ),
    Case(
        "other-diagnostics",
        (OTHER,),
        "",
        lambda repo: _project_with_ready_parsers(repo),
        _tab_driver("tab-diagnostics"),
    ),
    Case(
        "confirm-build",
        (CONFIRM,),
        "",
        lambda repo: _screen(repo, cached=False),
        _confirm_dialog,
    ),
    Case(
        "state-no-head",
        (STATES,),
        "HEAD не найден",
        lambda repo: _screen(repo, head=None, cached=False),
    ),
    Case(
        "state-no-cache",
        (STATES,),
        "Кэша нет",
        lambda repo: _screen(repo, cached=False),
    ),
    Case(
        "state-building", (STATES,), "Идёт сборка", _holding, _building, _release_held
    ),
    Case(
        "state-build-failed",
        (STATES,),
        "Сборка не удалась",
        lambda repo: _screen(repo, cached=False, runner=_Runner(fail=True)),
        _build_failed,
    ),
    Case(
        "state-built",
        (STATES,),
        "Карта построена",
        lambda repo: _screen(repo, cached=False),
        _built,
    ),
    Case(
        "state-full-without-parser",
        (STATES,),
        "full, но парсер не готов",
        lambda repo: _project(repo),
        _tab_driver("tab-settings"),
    ),
    Case(
        "state-immutable-mode",
        (STATES,),
        "Режим неизменяем",
        lambda repo: _project(repo, orchestration=False),
        _tab_driver("tab-settings"),
    ),
)


def _project_with_ready_parsers(repo: Path) -> Screen[object]:
    _git_project(repo)
    return _screen(repo, parsers=READY)


def test_every_mockup_caption_and_state_has_a_snapshot_pair() -> None:
    captions = mockup_captions(MOCKUP)
    assert captions, "no captions parsed from the mockup"
    assert set(captions) == {section for case in CASES for section in case.sections}
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
    repo.mkdir()
    svg = asyncio.run(capture(case, size, repo, source=MOCKUP))
    check_snapshot(snapshot_path(DIRECTORY, case, size), svg)
