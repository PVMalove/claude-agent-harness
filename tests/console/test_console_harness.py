"""harness.console.screens.harness: Pilot tests of the Harness section with a recording runner.
They assert the process argv the screen invoked, that every irreversible class asks first and a
cancelled confirmation runs nothing, and that ledger reset requires typing RESET. Skipped without
textual; driven with `asyncio.run` like tests/test_console_app.py."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

import pytest

pytest.importorskip("textual")

from textual.app import App
from textual.pilot import Pilot
from textual.widgets import Input, ListItem, ListView, Static

from harness.console.catalog import (
    HARNESS_COMMANDS,
    CatalogEntry,
    Reversibility,
    process_argv,
)
from harness.console.screens.harness import CommandFormScreen, HarnessScreen


class _RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> "subprocess.CompletedProcess[str]":
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "done\n", "")


class _HostApp(App[None]):
    def __init__(self, screen: HarnessScreen) -> None:
        super().__init__()
        self._harness_screen = screen
        self.language = "ru"

    def on_mount(self) -> None:
        self.push_screen(self._harness_screen)


def _select(app: App[None], key: str, entries: Sequence[CatalogEntry]) -> None:
    """Highlight `key` by its menu item, since the menu lists only commands available here."""
    assert key in {entry.key for entry in entries}
    screen = app.screen
    if isinstance(screen, HarnessScreen):
        screen.select_command(key)
    menu = app.screen.query_one("#command-menu", ListView)
    menu.index = [item.name for item in menu.query(ListItem)].index(key)


async def _choose(
    pilot: Pilot[None], app: App[None], key: str, entries: Sequence[CatalogEntry]
) -> None:
    if isinstance(app.screen, HarnessScreen):
        app.screen._expanded_group = app.screen.menu_group(key)
        await app.screen._rebuild_menu(key)
    _select(app, key, entries)
    await pilot.press("enter")
    await pilot.pause()


async def _settle(pilot: Pilot[None], app: App[None]) -> None:
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def _entry(key: str) -> CatalogEntry:
    return next(entry for entry in HARNESS_COMMANDS if entry.key == key)


def test_a_reversible_command_runs_immediately_and_shows_its_cli_equivalent(
    tmp_path: Path,
) -> None:
    runner = _RecordingRunner()

    async def scenario() -> str:
        app = _HostApp(HarnessScreen(tmp_path, command_runner=runner))
        async with app.run_test(size=(120, 40)) as pilot:
            await _choose(pilot, app, "diff", HARNESS_COMMANDS)
            await _settle(pilot, app)
            assert not isinstance(app.screen, CommandFormScreen)
            return str(app.screen.query_one("#command-output", Static).content)

    output = asyncio.run(scenario())
    assert runner.calls == [process_argv(["harness", "diff", str(tmp_path)])]
    assert output.startswith(f"$ {_entry('diff').cli_line(tmp_path)}")
    assert "done" in output


@pytest.mark.parametrize(
    "reversibility",
    [r for r in Reversibility if r is not Reversibility.REVERSIBLE],
    ids=lambda r: r.value,
)
def test_every_irreversible_class_asks_first_and_cancel_runs_nothing(
    tmp_path: Path, reversibility: Reversibility
) -> None:
    entry = CatalogEntry(
        "probe", "probe", ("harness", "list", "{repo}"), reversibility, "x:y"
    )
    runner = _RecordingRunner()

    async def scenario() -> tuple[bool, bool]:
        app = _HostApp(HarnessScreen(tmp_path, command_runner=runner, catalog=[entry]))
        async with app.run_test(size=(120, 40)) as pilot:
            await _choose(pilot, app, "probe", [entry])
            asked = isinstance(app.screen, CommandFormScreen) and bool(
                app.screen.query("#confirmation-reason")
            )
            await pilot.click("#cancel")
            await _settle(pilot, app)
            return asked, isinstance(app.screen, HarnessScreen)

    asked, back = asyncio.run(scenario())
    assert asked
    assert back
    assert runner.calls == []


def test_confirming_an_irreversible_command_runs_it(tmp_path: Path) -> None:
    runner = _RecordingRunner()

    async def scenario() -> None:
        app = _HostApp(HarnessScreen(tmp_path, command_runner=runner))
        async with app.run_test(size=(120, 40)) as pilot:
            await _choose(pilot, app, "cleanup-apply", HARNESS_COMMANDS)
            await pilot.click("#run")
            await _settle(pilot, app)

    asyncio.run(scenario())
    from harness.cleanup import plan_cleanup, plan_digest

    assert runner.calls == [
        process_argv(["harness", "cleanup", str(tmp_path), "--apply"])
        + ["--plan-digest", plan_digest(plan_cleanup(tmp_path, "soft"))]
    ]


def _with_script(repo: Path, relative: str) -> Path:
    """The Harness screen offers a `python <script>` command only where its script exists."""
    script = repo / relative
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("", encoding="utf-8")
    return repo


def test_ledger_reset_requires_typing_reset(tmp_path: Path) -> None:
    runner = _RecordingRunner()
    _with_script(tmp_path, ".harness/orchestration/coordinator.py")

    async def scenario() -> list[int]:
        app = _HostApp(HarnessScreen(tmp_path, command_runner=runner))
        calls_after_each_try: list[int] = []
        async with app.run_test(size=(120, 40)) as pilot:
            await _choose(pilot, app, "ledger-reset", HARNESS_COMMANDS)
            await pilot.click("#run")
            await pilot.pause()
            calls_after_each_try.append(len(runner.calls))
            await pilot.click("#typed-confirmation")
            await pilot.press(*"reset")
            await pilot.click("#run")
            await pilot.pause()
            calls_after_each_try.append(len(runner.calls))
            app.screen.query_one("#typed-confirmation", Input).value = ""
            await pilot.click("#typed-confirmation")
            await pilot.press(*"RESET")
            await pilot.click("#run")
            await _settle(pilot, app)
            calls_after_each_try.append(len(runner.calls))
        return calls_after_each_try

    assert asyncio.run(scenario()) == [0, 0, 1]
    assert runner.calls[0][-4:] == ["ledger", "reset", "--confirm", "RESET"]


def test_a_command_with_inputs_asks_for_them_before_running(tmp_path: Path) -> None:
    runner = _RecordingRunner()
    _with_script(tmp_path, "scripts/build_parser_bundle.py")

    async def scenario() -> None:
        app = _HostApp(HarnessScreen(tmp_path, command_runner=runner))
        async with app.run_test(size=(120, 40)) as pilot:
            await _choose(pilot, app, "parser-bundle", HARNESS_COMMANDS)
            assert isinstance(app.screen, CommandFormScreen)
            assert not app.screen.query("#confirmation-reason")
            await pilot.click("#input-wheelhouse")
            await pilot.press(*"wheels")
            await pilot.click("#run")
            await _settle(pilot, app)

    asyncio.run(scenario())
    expected = _entry("parser-bundle").cli_argv(tmp_path, {"wheelhouse": "wheels"})
    assert runner.calls == [process_argv(expected)]


def test_commands_whose_script_is_absent_are_not_offered(tmp_path: Path) -> None:
    """verify and parser-bundle exist only in the canonical harness repository: in a target project
    they would always fail with "can't open file", so the menu hides them."""

    async def scenario() -> list[str]:
        app = _HostApp(HarnessScreen(tmp_path, command_runner=_RecordingRunner()))
        async with app.run_test(size=(120, 40)):
            return list(app._harness_screen._entries)

    offered = asyncio.run(scenario())
    assert "verify" not in offered and "parser-bundle" not in offered
    assert "diff" in offered and "worktree-remove" in offered


def test_harness_section_shows_installation_state_and_pipeline_usage(
    tmp_path: Path,
) -> None:
    from tests.console._console_ledger_fixture import build_reports_fixture

    build_reports_fixture(tmp_path)

    async def scenario() -> str:
        app = _HostApp(HarnessScreen(tmp_path, command_runner=_RecordingRunner()))
        async with app.run_test(size=(120, 40)):
            return str(app.screen.query_one("#harness-state", Static).content) + str(
                app.screen.query_one("#harness-usage", Static).content
            )

    text = asyncio.run(scenario())
    assert "Харнесс не установлен" in text
    assert "Использование пайплайна: запусков 2, тикетов 2" in text


class _RaisingRunner(_RecordingRunner):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> "subprocess.CompletedProcess[str]":
        self.calls.append(list(argv))
        raise ValueError("embedded null byte")


def test_a_runner_failure_is_shown_instead_of_running_forever(tmp_path: Path) -> None:
    runner = _RaisingRunner()

    async def scenario() -> str:
        app = _HostApp(HarnessScreen(tmp_path, command_runner=runner))
        async with app.run_test(size=(120, 40)) as pilot:
            await _choose(pilot, app, "diff", HARNESS_COMMANDS)
            await _settle(pilot, app)
            return str(app.screen.query_one("#command-output", Static).content)

    output = asyncio.run(scenario())
    assert len(runner.calls) == 1
    assert output.startswith(f"$ {_entry('diff').cli_line(tmp_path)}")
    assert "сбой запуска: ValueError('embedded null byte')" in output


def test_every_harness_command_belongs_to_a_menu_group() -> None:
    from harness.console.screens.harness import _GROUPS

    assert {entry.key for entry in HARNESS_COMMANDS} <= set(_GROUPS)


def test_menu_starts_on_a_check_and_shows_the_full_cli_path(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[str | None, str, list[str]]:
        app = _HostApp(HarnessScreen(tmp_path, command_runner=_RecordingRunner()))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            menu = app.screen.query_one("#command-menu", ListView)
            detail = str(app.screen.query_one("#command-cli", Static).content)
            headings = [
                str(item.query_one(Static).content)
                for item in menu.query(ListItem)
                if item.name is None
            ]
            return (
                menu.highlighted_child.name if menu.highlighted_child else None,
                detail,
                headings,
            )

    name, detail, headings = asyncio.run(scenario())
    assert name == "diff"
    assert f"$ harness diff {tmp_path}" in detail
    assert "Проверки" in headings[0]


def test_y_copies_the_full_cli_line_of_the_highlighted_command(tmp_path: Path) -> None:
    async def scenario() -> list[str]:
        copied: list[str] = []
        app = _HostApp(HarnessScreen(tmp_path, command_runner=_RecordingRunner()))
        async with app.run_test(size=(120, 40)) as pilot:
            setattr(app, "copy_to_clipboard", copied.append)
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
        return copied

    (line,) = asyncio.run(scenario())
    assert line == _entry("diff").cli_line(tmp_path)


# -- the shared shell -----------------------------------------------------------------------


def test_harness_sits_on_the_shell_and_the_section_menu_runs_nothing(
    tmp_path: Path,
) -> None:
    from textual.screen import Screen

    from harness.console.app import HarnessConsoleApp
    from harness.console.shell import SECTIONS, Breadcrumbs, ConsoleShell
    from tests.console._pilot_wait import crumb_text

    runner = _RecordingRunner()

    async def scenario() -> tuple[str, int | None, bool, bool, bool, list[str | None]]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 60)) as pilot:
            screen = HarnessScreen(tmp_path, command_runner=runner)
            await app.push_screen(screen)
            await _settle(pilot, app)
            assert isinstance(screen, ConsoleShell)
            crumbs = crumb_text(screen.query_one("#shell-breadcrumbs", Breadcrumbs))
            menu = screen.query_one("#shell-sections", ListView)
            active = [
                item.name for item in menu.query(ListItem) if item.has_class("-active")
            ]
            focused = screen.query_one("#command-menu", ListView).has_focus
            # Enter on the open section's own entry opens nothing and runs no command.
            menu.focus()
            await pilot.press("enter")
            await _settle(pilot, app)
            stayed = app.screen is screen
            # Another number opens its section on top of the screen.
            await pilot.press("6")
            await _settle(pilot, app)
            left = app.screen is not screen and isinstance(app.screen, Screen)
            return crumbs, menu.index, focused, stayed, left, active

    crumbs, index, focused, stayed, left, active = asyncio.run(scenario())
    assert crumbs == "Главная  ›  Harness"
    assert index == SECTIONS.index("Harness")
    assert active == ["Harness"]
    assert focused and stayed and left
    assert runner.calls == []


def test_harness_screen_opens_help_switches_language_and_goes_back(
    tmp_path: Path,
) -> None:
    from harness.console.app import HarnessConsoleApp
    from harness.console.screens.help import HelpScreen
    from harness.console.shell import Breadcrumbs
    from tests.console._pilot_wait import crumb_text

    async def scenario() -> tuple[bool, bool, str, bool]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(120, 60)) as pilot:
            screen = HarnessScreen(tmp_path, command_runner=_RecordingRunner())
            await app.push_screen(screen)
            await _settle(pilot, app)
            await pilot.press("f1")
            await _settle(pilot, app)
            help_open = isinstance(app.screen, HelpScreen)
            await pilot.press("escape")
            await _settle(pilot, app)
            help_closed = app.screen is screen
            await pilot.press("f2")
            await _settle(pilot, app)
            crumbs = crumb_text(screen.query_one("#shell-breadcrumbs", Breadcrumbs))
            await pilot.press("escape")
            await _settle(pilot, app)
            return help_open, help_closed, crumbs, app.screen is not screen

    help_open, help_closed, crumbs, went_back = asyncio.run(scenario())
    assert help_open and help_closed
    assert crumbs == "Home  ›  Harness"
    assert went_back


@pytest.mark.parametrize("key", ["diff", "cleanup-apply"])
def test_click_and_arrow_selection_never_activate_a_command(
    tmp_path: Path, key: str
) -> None:
    runner = _RecordingRunner()

    async def scenario() -> None:
        app = _HostApp(HarnessScreen(tmp_path, command_runner=runner))
        async with app.run_test(size=(160, 52)) as pilot:
            menu = app.screen.query_one("#command-menu", ListView)
            app._harness_screen.select_command(key)
            await pilot.pause()
            row = next(item for item in menu.query(ListItem) if item.name == key)
            row.scroll_visible(immediate=True)
            await pilot.pause()
            await pilot.click(row)
            await pilot.press("up", "down")
            await pilot.pause()
            assert app.screen is app._harness_screen
            assert runner.calls == []
            assert menu.highlighted_child is row
            await pilot.press("enter")
            await _settle(pilot, app)
            if key == "diff":
                assert len(runner.calls) == 1
            else:
                assert isinstance(app.screen, CommandFormScreen)
                assert runner.calls == []

    asyncio.run(scenario())


class _BlockingRunner(_RecordingRunner):
    def __init__(self) -> None:
        from threading import Event

        super().__init__()
        self.started = Event()
        self.release = Event()

    def __call__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        self.started.set()
        assert self.release.wait(10)
        return subprocess.CompletedProcess(
            list(argv),
            7,
            "FIRST\n" + "line\n" * 300 + "LAST",
            "ERR-FIRST\n" + "err\n" * 300 + "ERR-LAST",
        )


def test_busy_harness_survives_reopen_and_retains_full_output(tmp_path: Path) -> None:
    runner = _BlockingRunner()

    async def scenario() -> None:
        screen = HarnessScreen(tmp_path, command_runner=runner)
        app = _HostApp(screen)
        async with app.run_test(size=(160, 52), notifications=True) as pilot:
            try:
                await _choose(pilot, app, "diff", HARNESS_COMMANDS)
                assert runner.started.wait(1)
                await pilot.press("enter")
                await pilot.pause()
                assert len(runner.calls) == 1
                assert "Дождитесь" in str(
                    screen.query_one("#run-feedback", Static).content
                )
                await app.pop_screen()
                replacement = HarnessScreen(tmp_path, command_runner=runner)
                await app.push_screen(replacement)
                replacement.open_command("diff")
                assert len(runner.calls) == 1
                runner.release.set()
                await _settle(pilot, app)
                await pilot.pause(0.2)
                output = str(replacement.query_one("#command-output", Static).content)
                assert all(
                    marker in output
                    for marker in ("FIRST", "LAST", "ERR-FIRST", "ERR-LAST")
                )
                assert "exit 7" in str(
                    replacement.query_one("#run-status", Static).content
                )
            finally:
                runner.release.set()

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(160, 52), (80, 24)])
@pytest.mark.parametrize("language", ["ru", "en"])
def test_layout_explicit_run_copy_and_confirmation_validity(
    tmp_path: Path, size: tuple[int, int], language: str
) -> None:
    from textual.widgets import Button

    runner = _RecordingRunner()

    async def scenario() -> None:
        app = _HostApp(
            HarnessScreen(tmp_path, command_runner=runner, load_drift=lambda _: "clean")
        )
        app.language = language
        copied: list[str] = []
        setattr(app, "copy_to_clipboard", copied.append)
        async with app.run_test(size=size) as pilot:
            screen = app._harness_screen
            await _settle(pilot, app)
            assert (
                screen.query_one("#harness-title").region.y
                < screen.query_one("#harness-state").region.y
            )
            assert screen.query_one("#command-menu").region.height >= 2
            if size[0] == 80:
                await pilot.press("enter")
                await pilot.pause()
                assert runner.calls == []
            await pilot.press("y")
            assert copied == [_entry("diff").cli_line(tmp_path)]
            await pilot.press("enter")
            await _settle(pilot, app)
            status = screen.query_one("#run-status", Static)
            assert "exit 0" in str(status.content)
            assert 0 <= status.region.y < size[1] - 1
            screen.open_command("worktree-remove")
            await pilot.pause()
            assert isinstance(app.screen, CommandFormScreen)
            run = app.screen.query_one("#run", Button)
            field = app.screen.query_one("#input-worktree", Input)
            label = app.screen.query_one("#label-worktree", Static)
            assert run.disabled
            assert label.region.y < field.region.y
            field.value = "  "
            await pilot.pause()
            assert run.disabled
            field.value = "worktrees/example"
            await pilot.pause()
            await _settle(pilot, app)
            assert run.disabled  # A typed path alone is no proof of safe deletion.
            assert str(
                app.screen.query_one("#maintenance-confirmation", Static).content
            )
            assert run.region.bottom <= size[1]
            await pilot.click("#cancel")
            assert len(runner.calls) == 1

    asyncio.run(scenario())


def test_twenty_commands_five_groups_filters_and_catalog_semantics(
    tmp_path: Path,
) -> None:
    from collections import Counter
    from harness.console.catalog import REVERSIBILITY_LABELS

    for path in (
        "scripts/verify.py",
        "scripts/build_parser_bundle.py",
        ".harness/repo_map/repo_map.py",
        ".harness/orchestration/coordinator.py",
    ):
        _with_script(tmp_path, path)
    runner = _RecordingRunner()

    async def scenario() -> None:
        screen = HarnessScreen(
            tmp_path, command_runner=runner, load_drift=lambda _: "clean"
        )
        app = _HostApp(screen)
        async with app.run_test(size=(160, 52)) as pilot:
            await _settle(pilot, app)
            assert len(screen._entries) == 20
            assert Counter(screen.menu_group(key) for key in screen._entries) == {
                "Проверки": 5,
                "Скиллы": 3,
                "Repo Map": 2,
                "Очистка и удаление": 7,
                "Ledger": 3,
            }
            headings = {
                group: str(screen.query_one(f"#{heading_id} Static", Static).content)
                for heading_id, group in screen._headings.items()
            }
            assert "✔ 3 ✖ 4" in headings["Очистка и удаление"]
            assert "✔ 1 ✖ 2" in headings["Ledger"]
            await pilot.click("#filter-confirmation")
            await _settle(pilot, app)
            assert all(entry.needs_confirmation for entry in screen._filtered_entries())
            await pilot.click("#filter-reversible")
            await _settle(pilot, app)
            assert all(
                not entry.needs_confirmation for entry in screen._filtered_entries()
            )
            assert runner.calls == []
            assert _entry("health-fix").reversibility is Reversibility.REVERSIBLE
            assert _entry("parser-bundle").reversibility is Reversibility.REVERSIBLE
            assert set(REVERSIBILITY_LABELS) == set(Reversibility)
            for key, word in (
                ("cleanup-hard-apply", "HARD"),
                ("uninstall-apply", "UNINSTALL"),
                ("ledger-reset", "RESET"),
            ):
                assert _entry(key).typed_confirmation == word

    asyncio.run(scenario())


def test_empty_filter_cannot_activate_and_drift_does_not_block_ui(
    tmp_path: Path,
) -> None:
    from threading import Event
    from textual.widgets import Button

    release, started = Event(), Event()
    runner = _RecordingRunner()

    def drift(_: Path) -> str:
        started.set()
        release.wait()  # The scenario releases this in finally; wall time is not the assertion.
        return "drift"

    async def scenario() -> None:
        screen = HarnessScreen(
            tmp_path, command_runner=runner, catalog=[_entry("diff")], load_drift=drift
        )
        app = _HostApp(screen)
        async with app.run_test(size=(80, 24)) as pilot:
            try:
                assert started.wait(1)
                assert "проверяется" in str(
                    screen.query_one("#harness-state", Static).content
                )
                await pilot.click("#filter-confirmation")
                await pilot.pause()
                assert screen._filtered_entries() == []
                assert screen.query_one("#command-run", Button).disabled
                screen.query_one("#command-menu", ListView).focus()
                await pilot.press("enter")
                assert runner.calls == []
                release.set()
                await _settle(pilot, app)
                assert "дрейф есть" in str(
                    screen.query_one("#harness-state", Static).content
                )
            finally:
                release.set()

    asyncio.run(scenario())
