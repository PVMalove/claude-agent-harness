"""SVG snapshots of the Open project screen: one pair (160x52 and 80x24) for every picture and every
state of the accepted mockup docs/design/console-mockups/open-project.html. The checked paths, the
branch and the health report are fixed, so a snapshot changes only when the screen does; see
tests/console/_svg_snapshot.py to regenerate them.

The snapshots show POSIX paths (`/srv/projects/...`), so they are compared on POSIX systems only; the
check that every mockup caption and state has its snapshot files reads no path and runs everywhere."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from textual.app import App
from textual.pilot import Pilot
from textual.screen import Screen
from textual.widgets import Button, DirectoryTree, Input

from harness.console import data as console_data
from harness.console import project_target
from harness.console.project_target import ProjectTarget
from harness.console.screens import project_setup
from harness.console.screens.project_setup import ProjectSetupScreen
from harness.health.model import CheckResult, Fix, Report
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

MOCKUP = "open-project.html"
DIRECTORY = "project_setup"
WIDE = "1 · Открыть проект — широкий терминал (≈160×52)"
NARROW = "1 · Открыть проект — узкий терминал (≈80×24)"
STATES = "Состояния строки под путём"
TREE = "«Обзор папок» — раскрыто"

# The paths the screen shows; each maps to the directory a case prepared in the test's temp dir.
FIXED_REPO = "/srv/projects/claude-agent-harness"
FIXED_PLAIN = "/srv/projects/notes"
FIXED_MISSING = "/srv/projects/missing"
FIXED_CODE = "/srv/projects/Code"
_REAL: dict[str, Path] = {}


class _StillScreen(ProjectSetupScreen):
    """The screen with a steady caret: a blinking one would make a snapshot depend on the clock."""

    def on_mount(self) -> None:
        self.query_one("#project-path", Input).cursor_blink = False


def _git(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _prepared(repo: Path, kind: str) -> Path:
    """The project the case checks, in the state its mockup row describes."""
    _git(repo)
    if kind == "update":
        (repo / ".harness").mkdir()
        (repo / ".harness/harness.lock").write_text(
            json.dumps({"capabilities": ["pvmalove-suite"], "files": {}}),
            encoding="utf-8",
        )
    elif kind == "adopt":
        (repo / "AGENTS.md").write_text("Project-owned instructions", encoding="utf-8")
        (repo / ".claude/skills").mkdir(parents=True)
    elif kind == "broken":
        (repo / ".harness").mkdir()
        (repo / ".harness/harness.lock").write_text("{not json", encoding="utf-8")
    _REAL.clear()
    _REAL[FIXED_REPO] = repo
    _REAL[FIXED_PLAIN] = repo.parent / "notes"
    _REAL[FIXED_PLAIN].mkdir(exist_ok=True)
    return repo


def _screen(kind: str, repo: Path) -> Screen[object]:
    _prepared(repo, kind)
    return cast(
        Screen[object],
        _StillScreen(repo, command_runner=lambda *a, **k: None),  # type: ignore[arg-type]
    )


def _report() -> Report:
    def check(
        identifier: str, status: str, message: str, fix: Fix | None = None
    ) -> CheckResult:
        return CheckResult(identifier, identifier.split(".")[0], status, message, fix)  # type: ignore[arg-type]

    return Report(
        1,
        FIXED_REPO,
        False,
        [
            check(
                "files.lock", "ok", "harness.lock согласован с установленным пакетом"
            ),
            check(
                "files.discovery_links",
                "ok",
                "ссылки .agents/skills и .claude/skills на месте",
            ),
            check("files.registry", "ok", "реестр скиллов совпадает со скиллами"),
            check("files.agents_md", "ok", "AGENTS.md актуален"),
            check("git.available", "ok", "Git доступен"),
            check(
                "overlays.locks",
                "warn",
                "скиллы отличаются от установленной версии Harness",
                Fix("Обновить Harness (2)"),
            ),
            check(
                "integrations.settings",
                "warn",
                "нужно проверить настройки интеграций",
                Fix("Настройка (8)"),
            ),
            check("tracker.auth", "skipped", "офлайн-проверка: онлайн-часть пропущена"),
        ],
    )


@pytest.fixture(autouse=True)
def fixed_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """The screen reports the fixed paths while the checks run on the directories of the case."""
    real_inspect = project_target.inspect_target
    real_blockers = project_target.installation_blockers

    def inspect(value: str) -> ProjectTarget:
        target = real_inspect(str(_REAL.get(value, value)))
        return replace(target, repo=Path(value))

    def blockers(target: ProjectTarget, capability: str) -> tuple[str, ...]:
        return real_blockers(replace(target, repo=_REAL[str(target.repo)]), capability)

    monkeypatch.setattr(project_setup, "inspect_target", inspect)
    monkeypatch.setattr(project_setup, "installation_blockers", blockers)
    monkeypatch.setattr(
        console_data, "collect_diagnostics", lambda _repo, *, online=False: _report()
    )


def _press(app: App[None], selector: str) -> None:
    """The press of a button without `Button.press()`: that one lights the button for a moment,
    and a snapshot must not depend on when it is taken."""
    button = app.screen.query_one(selector, Button)
    button.post_message(Button.Pressed(button))


def _setup(app: App[None]) -> ProjectSetupScreen:
    screen = app.screen
    assert isinstance(screen, ProjectSetupScreen)
    return screen


async def _enter_path(pilot: Pilot[None], app: App[None], value: str) -> None:
    _setup(app).query_one("#project-path", Input).value = value
    await pilot.pause()


async def _check_fixed(pilot: Pilot[None], app: App[None]) -> None:
    """Check the case's project and wait for its plan and the automatic local checks."""
    await _enter_path(pilot, app, FIXED_REPO)
    _press(app, "#inspect-project")
    screen = _setup(app)
    await until(
        pilot,
        lambda: not screen.busy
        and screen.target is not None
        and screen._blocked is not None
        and screen._summary is not None,
    )


async def _check_failing(pilot: Pilot[None], app: App[None], value: str) -> None:
    await _enter_path(pilot, app, value)
    _press(app, "#inspect-project")
    screen = _setup(app)
    await until(pilot, lambda: not screen.busy and screen._target_status[0] == "error")


async def _unchecked(pilot: Pilot[None], app: App[None]) -> None:
    await _enter_path(pilot, app, FIXED_REPO)


async def _missing(pilot: Pilot[None], app: App[None]) -> None:
    await _check_failing(pilot, app, FIXED_MISSING)


async def _not_git(pilot: Pilot[None], app: App[None]) -> None:
    await _check_failing(pilot, app, FIXED_PLAIN)


async def _git_unavailable(pilot: Pilot[None], app: App[None]) -> None:
    with patch(
        "harness.console.project_target.subprocess.run", side_effect=FileNotFoundError
    ):
        await _check_failing(pilot, app, FIXED_REPO)


async def _corrupt_lock(pilot: Pilot[None], app: App[None]) -> None:
    await _check_failing(pilot, app, FIXED_REPO)


def _folders(repo: Path) -> Screen[object]:
    """A `Code` directory like the mockup's: the project among its neighbours."""
    code = repo.parent / "Code"
    for name in (
        "agent-sandbox",
        "claude-agent-harness/.harness",
        "claude-agent-harness/docs",
        "claude-agent-harness/harness",
        "claude-agent-harness/tests",
        "notes-app",
        "old-experiments",
        "website",
    ):
        (code / name).mkdir(parents=True, exist_ok=True)
    _REAL.clear()
    _REAL[FIXED_CODE] = code
    return cast(
        Screen[object],
        _StillScreen(repo, command_runner=lambda *a, **k: None),  # type: ignore[arg-type]
    )


async def _browse(pilot: Pilot[None], app: App[None]) -> None:
    screen = _setup(app)
    await _enter_path(pilot, app, str(_REAL[FIXED_CODE]))
    _press(app, "#browse-project")
    await until(pilot, lambda: bool(screen.query("#directory-browser")))
    tree = screen.query_one("#directory-browser", DirectoryTree)
    await until(pilot, lambda: tree.has_focus and bool(tree.root.children))
    # `claude-agent-harness` is the second folder; Right opens it.
    await pilot.press("down", "down", "right")
    node = tree.cursor_node
    assert node is not None and str(node.label) == "claude-agent-harness"
    await until(pilot, lambda: node.is_expanded and bool(node.children))
    # The tree stays on the real directory; the screen shows the fixed one.
    screen._browser_root = FIXED_CODE
    screen._paint_browser()
    await _enter_path(pilot, app, FIXED_CODE)


CASES = (
    Case(
        "1-open",
        (WIDE, NARROW),
        "",
        lambda repo: _screen("update", repo),
        _check_fixed,
    ),
    Case("1-browse", (TREE,), "", _folders, _browse),
    Case(
        "state-unchecked",
        (STATES,),
        "Путь не проверен",
        lambda repo: _screen("init", repo),
        _unchecked,
    ),
    Case(
        "state-missing",
        (STATES,),
        "Каталога нет",
        lambda repo: _screen("init", repo),
        _missing,
    ),
    Case(
        "state-not-git",
        (STATES,),
        "Не Git-проект",
        lambda repo: _screen("init", repo),
        _not_git,
    ),
    Case(
        "state-git-unavailable",
        (STATES,),
        "Git недоступен",
        lambda repo: _screen("init", repo),
        _git_unavailable,
    ),
    Case(
        "state-lock-corrupt",
        (STATES,),
        "harness.lock повреждён",
        lambda repo: _screen("broken", repo),
        _corrupt_lock,
    ),
    Case(
        "state-init",
        (STATES,),
        "Новый проект (init)",
        lambda repo: _screen("init", repo),
        _check_fixed,
    ),
    Case(
        "state-adopt",
        (STATES,),
        "Есть свои файлы (adopt)",
        lambda repo: _screen("adopt", repo),
        _check_fixed,
    ),
    Case(
        "state-update",
        (STATES,),
        "Harness уже стоит (update)",
        lambda repo: _screen("update", repo),
        _check_fixed,
    ),
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


@pytest.mark.skipif(sys.platform == "win32", reason="the snapshots show POSIX paths")
@pytest.mark.parametrize("size", SIZES, ids=[f"{w}x{h}" for w, h in SIZES])
@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_screen_matches_its_snapshot(
    case: Case, size: tuple[int, int], tmp_path: Path
) -> None:
    repo = tmp_path / PROJECT
    (repo / "docs").mkdir(parents=True)
    svg = asyncio.run(capture(case, size, repo, source=MOCKUP))
    check_snapshot(snapshot_path(DIRECTORY, case, size), svg)
