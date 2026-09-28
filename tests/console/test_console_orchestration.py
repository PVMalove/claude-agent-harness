"""harness.console.screens.orchestration: Pilot tests of the Orchestration section with a
recording runner, one scenario per coordinator command group (batch, decide, packet, dispatch,
risk, context-package). They assert the confirmation shown for a terminal/external-change command,
that cancelling a confirmation runs nothing, that required and choice-restricted fields block
launch, and that a repeatable field serialises one `--flag value` pair per line - all without
spawning a real coordinator process. Skipped without textual; driven with `asyncio.run` like
tests/test_console_harness.py."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

import pytest

pytest.importorskip("textual")

from textual.app import App
from textual.pilot import Pilot
from textual.widgets import ListView, Static, TextArea

from harness.console.coordinator_catalog import (
    COORDINATOR_COMMANDS,
    CoordinatorCommand,
    process_argv,
)
from harness.console.screens.orchestration import (
    CoordinatorCommandFormScreen,
    OrchestrationScreen,
)


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
    def __init__(self, screen: OrchestrationScreen) -> None:
        super().__init__()
        self._orchestration_screen = screen

    def on_mount(self) -> None:
        self.push_screen(self._orchestration_screen)


def _select(app: App[None], key: str) -> None:
    menu = app.screen.query_one("#command-menu", ListView)
    menu.index = [entry.key for entry in COORDINATOR_COMMANDS].index(key)


async def _choose(pilot: Pilot[None], app: App[None], key: str) -> None:
    _select(app, key)
    await pilot.press("enter")
    await pilot.pause()


async def _settle(pilot: Pilot[None], app: App[None]) -> None:
    await app.workers.wait_for_complete()
    await pilot.pause()


def _entry(key: str) -> CoordinatorCommand:
    return next(entry for entry in COORDINATOR_COMMANDS if entry.key == key)


async def _fill_input(pilot: Pilot[None], dest: str, text: str) -> None:
    await pilot.click(f"#input-{dest}")
    await pilot.press(*text)


async def _fill_text_area(app: App[None], dest: str, text: str) -> None:
    app.screen.query_one(f"#input-{dest}", TextArea).text = text


def _host(tmp_path: Path, runner: _RecordingRunner) -> App[None]:
    return _HostApp(OrchestrationScreen(tmp_path, command_runner=runner))


# -- batch: batch approve -------------------------------------------------------------------


def test_batch_approve_confirms_and_cancel_runs_nothing(tmp_path: Path) -> None:
    runner = _RecordingRunner()

    async def scenario() -> bool:
        app = _host(tmp_path, runner)
        async with app.run_test(size=(120, 60)) as pilot:
            await _choose(pilot, app, "batch-approve")
            asked = isinstance(app.screen, CoordinatorCommandFormScreen) and bool(
                app.screen.query("#confirmation-reason")
            )
            await pilot.click("#cancel")
            await _settle(pilot, app)
            return asked

    asked = asyncio.run(scenario())
    assert asked
    assert runner.calls == []


def test_batch_approve_confirmed_runs_with_exact_argv(tmp_path: Path) -> None:
    runner = _RecordingRunner()

    async def scenario() -> None:
        app = _host(tmp_path, runner)
        async with app.run_test(size=(120, 60)) as pilot:
            await _choose(pilot, app, "batch-approve")
            await _fill_input(pilot, "batch", "batch-1")
            await _fill_input(pilot, "approved_by", "dev")
            await _fill_input(pilot, "approved_at", "2026-09-27T00:00:00Z")
            await pilot.click("#run")
            await _settle(pilot, app)

    asyncio.run(scenario())
    expected = _entry("batch-approve").cli_argv(
        tmp_path,
        {
            "batch": "batch-1",
            "approved_by": "dev",
            "approved_at": "2026-09-27T00:00:00Z",
        },
    )
    assert runner.calls == [process_argv(expected)]


# -- decide: batch decide --------------------------------------------------------------------


def test_batch_decide_invalid_decision_value_blocks_launch(tmp_path: Path) -> None:
    runner = _RecordingRunner()

    async def scenario() -> None:
        app = _host(tmp_path, runner)
        async with app.run_test(size=(120, 60)) as pilot:
            await _choose(pilot, app, "batch-decide")
            await _fill_input(pilot, "batch", "batch-1")
            await _fill_input(pilot, "decision", "not-a-real-decision")
            await _fill_input(pilot, "approved_by", "dev")
            await _fill_input(pilot, "approved_at", "now")
            await pilot.click("#run")
            await pilot.pause()
            assert isinstance(app.screen, CoordinatorCommandFormScreen)
            assert app.screen.query_one("#form-error", Static).content

    asyncio.run(scenario())
    assert runner.calls == []


# -- packet: batch decision-packet --------------------------------------------------------------


def test_batch_decision_packet_needs_no_confirmation(tmp_path: Path) -> None:
    runner = _RecordingRunner()

    async def scenario() -> None:
        app = _host(tmp_path, runner)
        async with app.run_test(size=(120, 60)) as pilot:
            await _choose(pilot, app, "batch-decision-packet")
            assert isinstance(app.screen, CoordinatorCommandFormScreen)
            assert not app.screen.query("#confirmation-reason")
            await _fill_input(pilot, "batch", "batch-1")
            await pilot.click("#run")
            await _settle(pilot, app)

    asyncio.run(scenario())
    expected = _entry("batch-decision-packet").cli_argv(tmp_path, {"batch": "batch-1"})
    assert runner.calls == [process_argv(expected)]


# -- dispatch: dispatch create, dispatch send ----------------------------------------------------


def test_dispatch_create_confirms_and_runs_with_exact_argv(tmp_path: Path) -> None:
    runner = _RecordingRunner()

    async def scenario() -> bool:
        app = _host(tmp_path, runner)
        async with app.run_test(size=(120, 60)) as pilot:
            await _choose(pilot, app, "dispatch-create")
            asked = bool(app.screen.query("#confirmation-reason"))
            await _fill_input(pilot, "batch", "batch-1")
            await pilot.click("#run")
            await _settle(pilot, app)
            return asked

    asked = asyncio.run(scenario())
    assert asked
    # `role` and `purpose` carry a parser default, so the form pre-fills and submits them too.
    expected = _entry("dispatch-create").cli_argv(
        tmp_path, {"batch": "batch-1", "role": "developer", "purpose": "work"}
    )
    assert runner.calls == [process_argv(expected)]


def test_dispatch_send_confirmation_reason_is_external_change(tmp_path: Path) -> None:
    from harness.console.catalog import CONFIRMATION_REASONS, Reversibility

    runner = _RecordingRunner()

    async def scenario() -> str:
        app = _host(tmp_path, runner)
        async with app.run_test(size=(120, 60)) as pilot:
            await _choose(pilot, app, "dispatch-send")
            reason = str(app.screen.query_one("#confirmation-reason", Static).content)
            await pilot.click("#cancel")
            await _settle(pilot, app)
            return reason

    reason = asyncio.run(scenario())
    assert reason == CONFIRMATION_REASONS[Reversibility.EXTERNAL_CHANGE]
    assert runner.calls == []


# -- risk: risk assess ------------------------------------------------------------------------


def test_risk_assess_empty_changed_file_blocks_launch(tmp_path: Path) -> None:
    runner = _RecordingRunner()

    async def scenario() -> None:
        app = _host(tmp_path, runner)
        async with app.run_test(size=(120, 60)) as pilot:
            await _choose(pilot, app, "risk-assess")
            await _fill_input(pilot, "batch", "batch-1")
            await _fill_input(pilot, "candidate_commit", "abc123")
            await pilot.click("#run")
            await pilot.pause()
            assert isinstance(app.screen, CoordinatorCommandFormScreen)
            assert app.screen.query_one("#form-error", Static).content

    asyncio.run(scenario())
    assert runner.calls == []


def test_risk_assess_several_lines_give_several_changed_file_pairs(
    tmp_path: Path,
) -> None:
    runner = _RecordingRunner()

    async def scenario() -> None:
        app = _host(tmp_path, runner)
        async with app.run_test(size=(120, 60)) as pilot:
            await _choose(pilot, app, "risk-assess")
            await _fill_input(pilot, "batch", "batch-1")
            await _fill_input(pilot, "candidate_commit", "abc123")
            await _fill_text_area(app, "changed_file", "a.py\nb.py\n")
            await pilot.click("#run")
            await _settle(pilot, app)

    asyncio.run(scenario())
    expected = _entry("risk-assess").cli_argv(
        tmp_path,
        {
            "batch": "batch-1",
            "candidate_commit": "abc123",
            "changed_file": "a.py\nb.py",
        },
    )
    assert runner.calls == [process_argv(expected)]
    assert runner.calls[0].count("--changed-file") == 2


# -- context-package: context-package register --------------------------------------------------


def test_context_package_register_required_fields_validated_no_confirmation(
    tmp_path: Path,
) -> None:
    runner = _RecordingRunner()

    async def scenario() -> None:
        app = _host(tmp_path, runner)
        async with app.run_test(size=(120, 60)) as pilot:
            await _choose(pilot, app, "context-package-register")
            assert isinstance(app.screen, CoordinatorCommandFormScreen)
            assert not app.screen.query("#confirmation-reason")
            await pilot.click("#run")
            await pilot.pause()
            assert isinstance(app.screen, CoordinatorCommandFormScreen)
            assert app.screen.query_one("#form-error", Static).content
            await _fill_input(pilot, "batch", "batch-1")
            await _fill_input(pilot, "candidate_commit", "abc123")
            await pilot.click("#run")
            await _settle(pilot, app)

    asyncio.run(scenario())
    # `role` and `inclusion_reason` carry a parser default, so the form pre-fills and submits them.
    expected = _entry("context-package-register").cli_argv(
        tmp_path,
        {
            "batch": "batch-1",
            "candidate_commit": "abc123",
            "role": "shared",
            "inclusion_reason": "manual immutable context registration",
        },
    )
    assert runner.calls == [process_argv(expected)]


# -- batch: batch list (a value-less flag) --------------------------------------------------


def test_batch_list_open_checkbox_passes_the_flag_alone(tmp_path: Path) -> None:
    """`--open` is a store_true flag: the form shows a checkbox, and ticking it adds `--open`
    with no value (a prefilled "False" value would make the coordinator reject the command)."""
    runner = _RecordingRunner()

    async def scenario() -> None:
        from textual.widgets import Checkbox

        app = _host(tmp_path, runner)
        async with app.run_test(size=(120, 60)) as pilot:
            await _choose(pilot, app, "batch-list")
            assert not app.screen.query("#confirmation-reason")
            app.screen.query_one("#input-open", Checkbox).value = True
            await pilot.click("#run")
            await _settle(pilot, app)

    asyncio.run(scenario())
    expected = _entry("batch-list").cli_argv(tmp_path, {"open": "1"})
    assert expected[-1] == "--open"
    assert runner.calls == [process_argv(expected)]


def test_pipeline_stats_and_history_filter_by_state_and_open_a_timeline(
    tmp_path: Path,
) -> None:
    """The section opens with the pipeline statistics; choosing a state lists its batches and
    choosing a batch opens its chronology."""
    from tests.console._console_ledger_fixture import build_reports_fixture

    build_reports_fixture(tmp_path)

    async def scenario() -> tuple[str, list[str | None], list[str | None], str]:
        from harness.console.screens.reports import BatchTimelineScreen

        app = _HostApp(OrchestrationScreen(tmp_path, command_runner=_RecordingRunner()))
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            stats_text = str(app.screen.query_one("#pipeline-stats", Static).content)
            history = app.screen.query_one("#batch-history", ListView)
            everything = [item.name for item in history.children]
            states = app.screen.query_one("#state-filter", ListView)
            states.focus()
            states.index = [item.name for item in states.children].index("blocked")
            await pilot.press("enter")
            await pilot.pause()
            blocked = [item.name for item in history.children]
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, BatchTimelineScreen)
            timeline = str(app.screen.query_one("#batch-timeline", Static).content)
        return stats_text, everything, blocked, timeline

    stats_text, everything, blocked, timeline = asyncio.run(scenario())
    assert "Запусков (batch): 2" in stats_text
    assert everything == ["batch-stuck", "batch-flow"]
    assert blocked == ["batch-stuck"]
    assert "[decision] решение coordinator: block" in timeline


def test_without_a_ledger_the_section_says_so_and_keeps_its_commands(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[str, int]:
        app = _HostApp(OrchestrationScreen(tmp_path, command_runner=_RecordingRunner()))
        async with app.run_test(size=(120, 60)):
            text = str(app.screen.query_one("#pipeline-stats", Static).content)
            history = len(app.screen.query("#batch-history"))
        return text, history

    text, history = asyncio.run(scenario())
    assert "не найден или не инициализирован" in text
    assert history == 0
