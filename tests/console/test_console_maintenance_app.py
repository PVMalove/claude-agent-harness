"""Maintenance states in both languages and terminal sizes, using real filesystem facts."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from threading import Event
from typing import cast

import pytest

from textual.widgets import Button, Input, Static
from textual.screen import Screen

from harness.console import maintenance
from harness.console.screens import harness as screen_module
from harness.console.screens.harness import CommandFormScreen, HarnessScreen
from harness.console.screens.commands import harness_runs
from harness.console.shell import Fold
from harness.console.catalog import CatalogEntry, Reversibility
from tests.console._svg_snapshot import until
from tests.console.test_console_harness import _RecordingRunner
from tests.console.harness.snapshots import EvidenceApp
from tests.console.test_console_maintenance import _real_tree


@pytest.mark.parametrize("size", [(160, 52), (80, 24)])
@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize(
    "state",
    ["loading", "empty", "error", "git-unavailable", "files", "removable", "dirty"],
)
def test_maintenance_states_and_confirmations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    size: tuple[int, int],
    language: str,
    state: str,
) -> None:
    tree = _real_tree(tmp_path) if state in ("removable", "dirty") else None
    if tree and state == "dirty":
        (tree / "dirty.txt").write_text("keep")
        os.utime(tree, (1, 1))
    if state == "files":
        log = tmp_path / ".harness/.sandboxes/logs/old.log"
        log.parent.mkdir(parents=True)
        log.write_bytes(b"1234567")
        os.utime(log, (1, 1))
    ready = maintenance.load_maintenance(tmp_path)
    release = Event()

    def load(_: Path) -> maintenance.MaintenanceView:
        if state == "loading":
            assert release.wait(10)
        if state == "error":
            raise PermissionError("fixture access denied")
        return ready

    monkeypatch.setattr(screen_module, "load_maintenance", load)
    runner = _RecordingRunner()

    async def scenario() -> None:
        screen = HarnessScreen(
            tmp_path, command_runner=runner, load_drift=lambda _: "clean"
        )
        app = EvidenceApp(tmp_path, lambda _: cast(Screen[object], screen))
        app.language = language
        async with app.run_test(size=size) as pilot:
            try:
                screen.query_one("#maintenance-fold", Fold).collapsed = False
                if state != "loading":
                    await until(
                        pilot,
                        lambda: screen._maintenance is not None
                        or bool(screen._maintenance_error),
                    )
                    await app.workers.wait_for_complete()
                await pilot.pause()
                summary = str(screen.query_one("#maintenance-summary", Static).content)
                clean = screen.query_one("#maintenance-clean", Button)
                if state == "loading":
                    assert (
                        "считается" if language == "ru" else "Calculating"
                    ) in summary
                    assert clean.disabled
                elif state == "empty":
                    assert (
                        "Временных файлов нет"
                        if language == "ru"
                        else "No temporary files"
                    ) in summary
                    assert clean.disabled
                elif state == "error":
                    assert "fixture access denied" in summary and clean.disabled
                elif state == "git-unavailable":
                    text = str(
                        screen.query_one(
                            "#maintenance-worktrees Static", Static
                        ).content
                    )
                    assert (
                        "Git недоступен" if language == "ru" else "Git unavailable"
                    ) in text
                    assert not screen.query("#maintenance-worktrees Button")
                elif state == "files":
                    assert "7 B" in summary and "logs" in summary and not clean.disabled
                    clean.press()
                    await until(
                        pilot,
                        lambda: isinstance(app.screen, CommandFormScreen)
                        and bool(app.screen.query("#maintenance-confirmation")),
                    )
                    await until(
                        pilot,
                        lambda: isinstance(app.screen, CommandFormScreen)
                        and app.screen._facts_ready,
                    )
                    form = app.screen
                    assert "7 B" in str(
                        form.query_one("#maintenance-confirmation", Static).content
                    )
                    assert not form.query_one("#run", Button).disabled
                    form.query_one("#cancel", Button).press()
                else:
                    assert tree is not None
                    index = next(
                        i for i, item in enumerate(ready.worktrees) if item.path == tree
                    )
                    button = screen.query_one(f"#tree-remove-{index}", Button)
                    button.scroll_visible(immediate=True)
                    await pilot.pause()
                    button.press()
                    await until(
                        pilot,
                        lambda: isinstance(app.screen, CommandFormScreen)
                        and bool(app.screen.query("#maintenance-confirmation")),
                    )
                    await until(
                        pilot,
                        lambda: isinstance(app.screen, CommandFormScreen)
                        and app.screen._facts_ready,
                    )
                    form = app.screen
                    assert form.query_one("#input-worktree", Input).value == str(tree)
                    text = str(
                        form.query_one("#maintenance-confirmation", Static).content
                    )
                    assert (
                        str(tree) in text
                        and f"dirty: {1 if state == 'dirty' else 0}" in text
                    )
                    assert form.query_one("#run", Button).disabled == (state == "dirty")
                    assert form.query_one("#run").region.bottom <= size[1]
                    form.query_one("#cancel", Button).press()
                await pilot.pause()
                assert screen.query_one("#command-menu").region.height >= 2
                assert screen.query_one("#maintenance-fold").region.bottom <= size[1]
                assert runner.calls == []
                (
                    tmp_path / f"maintenance-{state}-{language}-{size[0]}x{size[1]}.svg"
                ).write_text(app.export_screenshot(), encoding="utf-8")
            finally:
                release.set()

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(160, 52), (80, 24)])
@pytest.mark.parametrize("language", ["ru", "en"])
def test_live_output_survives_navigation_without_a_duplicate_process(
    tmp_path: Path,
    size: tuple[int, int],
    language: str,
) -> None:
    release = tmp_path / "release"
    count = tmp_path / "starts"
    code = (
        "import pathlib,sys,time; "
        f"pathlib.Path({str(count)!r}).open('a').write('start\\n'); "
        "sys.stdout.write('LIVE-OUT');sys.stdout.flush(); "
        "sys.stderr.write('LIVE-ERR');sys.stderr.flush(); "
        f"p=pathlib.Path({str(release)!r}); "
        "exec('while not p.exists(): time.sleep(.01)'); "
        "print('FINAL');sys.exit(7)"
    )
    entry = CatalogEntry(
        "probe",
        "probe",
        (sys.executable, "-u", "-c", code),
        Reversibility.REVERSIBLE,
        "test:probe",
    )

    async def scenario() -> None:
        screen = HarnessScreen(tmp_path, catalog=[entry], load_drift=lambda _: "clean")
        app = EvidenceApp(tmp_path, lambda _: cast(Screen[object], screen))
        app.language = language
        async with app.run_test(size=size) as pilot:
            try:
                screen.open_command("probe")

                def live_visible() -> bool:
                    run = harness_runs(screen).current
                    terminal = "\n".join(
                        strip.text for strip in screen._compositor.render_strips()
                    )
                    return bool(
                        run
                        and run.stdout == "LIVE-OUT"
                        and run.stderr == "LIVE-ERR"
                        and (
                            "LIVE-OUTLIVE-ERR" in terminal
                            or "LIVE-ERRLIVE-OUT" in terminal
                        )
                    )

                await until(pilot, live_visible)
                assert "exit 7" not in str(
                    screen.query_one("#run-status", Static).content
                )
                (tmp_path / f"streaming-{language}-{size[0]}x{size[1]}.svg").write_text(
                    app.export_screenshot(), encoding="utf-8"
                )
                await app.pop_screen()
                replacement = HarnessScreen(
                    tmp_path, catalog=[entry], load_drift=lambda _: "clean"
                )
                await app.push_screen(replacement)
                replacement.open_command("probe")
                await until(
                    pilot,
                    lambda: "LIVE-OUT"
                    in str(replacement.query_one("#command-output", Static).content),
                )
                assert count.read_text() == "start\n"
                release.touch()
                await until(
                    pilot,
                    lambda: "exit 7"
                    in str(replacement.query_one("#run-status", Static).content),
                )
                output = str(replacement.query_one("#command-output", Static).content)
                assert all(
                    marker in output for marker in ("LIVE-OUT", "LIVE-ERR", "FINAL")
                )
                assert count.read_text() == "start\n"
            finally:
                release.touch()

    asyncio.run(scenario())
