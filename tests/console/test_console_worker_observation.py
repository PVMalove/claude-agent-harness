"""Live worker observation is a local view, with no additional model turns (#716)."""

from __future__ import annotations

# ruff: noqa: E402 — optional Textual imports must follow importorskip.

import asyncio
import socket
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

pytest.importorskip("textual")
from textual.pilot import Pilot
from textual.widgets import RichLog, Static, Select, TabbedContent, Button

from harness.orchestration.core.utils import JsonObject
from harness.console.shell import Breadcrumbs
from harness.console.app import HarnessConsoleApp
from harness.console.screens.worker_observation import WorkerObservationScreen
from harness.console.shell import SECTIONS


async def settle(pilot: Pilot[None], done: Callable[[], bool]) -> None:
    """Pause until `done()`: a message bubbling between widgets can outlive one pause."""
    for _ in range(20):
        await pilot.pause()
        if done():
            return


@pytest.mark.parametrize("size", [(150, 48), (80, 24)])
@pytest.mark.parametrize("reported", [False, True])
def test_events_arrive_before_completion_without_model_requests(
    tmp_path: Path,
    size: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
    reported: bool,
) -> None:
    provider_requests: list[str] = []

    def no_network(_socket: socket.socket, address: object) -> None:
        provider_requests.append(str(address))
        raise AssertionError("observation attempted a network request")

    def no_command(
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        provider_requests.append("process:" + str(argv))
        raise AssertionError("observation attempted to launch a command")

    operation = {"text": "Read startup.py"}

    def source(_repo: Path, dispatch: str, offset: int) -> JsonObject:
        return {
            "dispatch_id": dispatch,
            "batch_id": "batch-observed",
            "role": "developer",
            "ticket": "#716",
            "state": "reported" if reported else "working",
            "worktree": "/project/very/long/worktree/path",
            "model": "observed-model",
            "startup": {
                "worker_state": "reported" if reported else "live",
                "classification": None,
                "observation": {"state": "live"},
            },
            "heartbeat_at": "separate heartbeat",
            "report_staged": False,
            "report_recorded": False,
            "launch_id": "launch-observed",
            "stream_state": "connected",
            "reset": False,
            "events": [{"kind": "tool", "text": operation["text"]}]
            if offset == 0
            else [],
            "next_offset": 1,
        }

    async def scenario() -> None:
        # Windows' event loop opens a loopback socket pair itself, so the guard
        # goes in after the loop exists.
        monkeypatch.setattr(socket.socket, "connect", no_network)
        app = HarnessConsoleApp(tmp_path, command_runner=no_command)
        async with app.run_test(size=size) as pilot:
            screen = WorkerObservationScreen(
                tmp_path, "dispatch-observed", observer=source
            )
            await app.push_screen(screen)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            log = screen.query_one("#worker-output", RichLog)
            assert "Read startup.py" in "\n".join(line.text for line in log.lines)
            status = str(screen.query_one("#worker-status", Static).content)
            assert ("ledger: reported" if reported else "ledger: working") in status
            assert "Процесс: live" in status
            assert screen.query_one("#worker-actions").region.bottom <= size[1]
            assert screen.region.contains_region(
                screen.query_one("#worker-reconnect", Button).region
            )
            assert log.region.height >= (5 if size[0] < 100 else 20)
            assert (
                str(screen.query_one("#worker-recovery", Button).label)
                == "История batch →"
            )
            screen.query_one("#worker-filter", Select).value = "error"
            await settle(pilot, lambda: not log.lines)
            assert not log.lines
            screen.query_one("#worker-filter", Select).value = "all"
            await settle(pilot, lambda: bool(log.lines))
            assert "Read startup.py" in "\n".join(line.text for line in log.lines)
            await pilot.press("f2")
            await pilot.pause()
            assert app.language == "en"
            assert (
                str(
                    screen.query_one("#worker-tabs", TabbedContent)
                    .get_tab("session")
                    .label
                )
                == "Session"
            )
            await pilot.press("f2")
            await pilot.pause()
            assert (
                str(
                    screen.query_one("#worker-tabs", TabbedContent)
                    .get_tab("session")
                    .label
                )
                == "Сессия"
            )
            await pilot.click("#worker-reconnect")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is not screen
            await app.push_screen(
                WorkerObservationScreen(tmp_path, "dispatch-observed", observer=source)
            )
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert provider_requests == []

    asyncio.run(scenario())


@pytest.mark.parametrize("choice", ["native-allow", "native-deny"])
def test_native_permission_requires_an_explicit_choice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    choice: str,
) -> None:
    from harness.orchestration.workflow import delivery
    from harness.console.screens.worker_observation import NativePermissionDialog

    choices: list[bool] = []

    def respond(
        _repo: Path, dispatch: str, request: str, digest: str, *, allow: bool
    ) -> JsonObject:
        assert (dispatch, request, digest) == (
            "dispatch-native",
            "request-native",
            "a" * 64,
        )
        choices.append(allow)
        return {"state": "queued"}

    monkeypatch.setattr(delivery, "respond_native_permission", respond)

    def source(_repo: Path, dispatch: str, offset: int) -> JsonObject:
        return {
            "dispatch_id": dispatch,
            "state": "working",
            "startup": {"worker_state": "live"},
            "stream_state": "connected",
            "events": [],
            "next_offset": offset,
            "permission_requests": [
                {
                    "request_id": "request-native",
                    "request_sha256": "a" * 64,
                    "tool_name": "Bash",
                    "input_preview": "git status --short",
                }
            ],
        }

    async def scenario() -> None:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(150, 48)) as pilot:
            await app.push_screen(
                WorkerObservationScreen(tmp_path, "dispatch-native", observer=source)
            )
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.click("#worker-permission")
            await pilot.pause()
            assert isinstance(app.screen, NativePermissionDialog)
            assert choices == []
            await pilot.press("escape")
            await pilot.pause()
            assert choices == []
            await pilot.click("#worker-permission")
            await pilot.pause()
            await pilot.click("#" + choice)
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert choices == [choice == "native-allow"]

    asyncio.run(scenario())


def test_confirmed_handoff_mounts_monitor_first_and_shows_the_refusal(
    tmp_path: Path,
) -> None:
    calls: list[Sequence[str]] = []

    def source(_repo: Path, dispatch: str, offset: int) -> JsonObject:
        return {
            "dispatch_id": dispatch,
            "state": "approved",
            "startup": {},
            "stream_state": "unavailable",
            "events": [],
            "next_offset": offset,
        }

    async def scenario() -> None:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(150, 48)) as pilot:
            screen = WorkerObservationScreen(
                tmp_path, "dispatch-handoff", observer=source
            )
            await app.push_screen(screen)
            await pilot.pause()

            def runner(
                argv: Sequence[str],
                *,
                env: Mapping[str, str] | None = None,
                cwd: Path | None = None,
            ) -> subprocess.CompletedProcess[str]:
                assert env is not None
                assert screen.is_mounted
                assert env["HARNESS_CONSOLE_ATTACHED"] == "1"
                calls.append(argv)
                return subprocess.CompletedProcess(
                    argv, 2, "", "ERROR: EROFS /project/ledger; token=fake-716-secret"
                )

            screen.launch(["already-confirmed-send"], runner)
            await app.workers.wait_for_complete()
            await pilot.pause()
            text = "\n".join(
                line.text for line in screen.query_one("#worker-output", RichLog).lines
            )
            assert "EROFS /project/ledger" in text
            assert "fake-716-secret" not in text
            await pilot.click("#worker-reconnect")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert len(calls) == 1

    asyncio.run(scenario())


def test_worker_matches_the_approved_three_panel_workspace(tmp_path: Path) -> None:
    from textual.widgets import TabbedContent, ListView

    async def scenario() -> None:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(180, 52)) as pilot:
            screen = WorkerObservationScreen(
                tmp_path,
                "dispatch-design",
                observer=lambda *_: {
                    "dispatch_id": "dispatch-design",
                    "ticket": "#716",
                    "role": "architect",
                    "state": "working",
                    "startup": {"worker_state": "live"},
                    "events": [],
                    "next_offset": 0,
                },
            )
            await app.push_screen(screen)
            await pilot.pause()
            await app.workers.wait_for_complete()
            sessions = screen.query_one("#worker-picker", ListView)
            stream = screen.query_one("#worker-output", RichLog)
            context = screen.query_one("#worker-context")
            navigation = screen.query_one("#worker-navigation")
            assert navigation.region.right <= sessions.region.x
            assert sessions.region.right <= stream.region.x
            assert stream.region.right <= context.region.x
            assert context.region.width >= 25
            assert screen.query_one("#worker-tabs", TabbedContent)
            assert screen.query_one("#worker-pipeline").region.y < stream.region.y
            assert (
                screen.query_one("#worker-breadcrumbs").region.y
                > screen.query_one("#topbar-left").region.y
            )
            await pilot.click("#worker-focus")
            await pilot.pause()
            assert stream.region.width > 140

    asyncio.run(scenario())


def test_session_list_prefers_recent_active_work_and_preserves_explicit_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.console import reports

    stages = [
        reports.StageEntry(
            "old-reported", "batch-old", "#422", "architect", "reported", "2026-09-01"
        ),
        reports.StageEntry(
            "older-active", "batch-active", "#704", "developer", "working", "2026-10-14"
        ),
        reports.StageEntry(
            "new-reported", "batch-done", "#555", "architect", "reported", "2026-10-16"
        ),
        reports.StageEntry(
            "newest-active", "batch-new", "#716", "architect", "working", "2026-10-15"
        ),
    ]
    monkeypatch.setattr(
        reports, "load_ledger_view", lambda _: reports.LedgerView(stages=stages)
    )

    async def scenario() -> None:
        from textual.widgets import ListView

        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(160, 52)) as pilot:
            screen = WorkerObservationScreen(tmp_path, observer=lambda *_: {})
            await app.push_screen(screen)
            await pilot.pause()
            assert screen.dispatch == "newest-active"
            assert [
                item.name
                for item in screen.query_one("#worker-picker", ListView).children
            ] == ["newest-active", "older-active", "new-reported", "old-reported"]
            await pilot.press("alt+2")
            await pilot.pause()
            assert screen.dispatch == "older-active"
            explicit = WorkerObservationScreen(
                tmp_path, "old-reported", observer=lambda *_: {}
            )
            await app.push_screen(explicit)
            await pilot.pause()
            assert explicit.dispatch == "old-reported"

    asyncio.run(scenario())


_LONG_ID = "dispatch-e98a4a72-50ac-4fd8-b421-1db55c17f287"
_LONG_WORKTREE = "/home/user/Projects/Code/some-project/.harness/.sandboxes/worktrees/issue-721-console-design-system"


def _blocked_snapshot(_repo: Path, dispatch: str, _offset: int) -> JsonObject:
    return {
        "dispatch_id": _LONG_ID,
        "ticket": "#721",
        "role": "developer",
        "state": "blocked",
        "worktree": _LONG_WORKTREE,
        "failure": {"error": "tooling blocked the commit: hook refused the message"},
        "startup": {"worker_state": "live"},
        "events": [],
        "next_offset": 0,
    }


@pytest.mark.parametrize("size", [(135, 44), (100, 40), (70, 30)])
def test_context_panel_keeps_full_id_path_and_block_reason_at_every_width(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    from textual.widgets import ListView

    async def scenario() -> None:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=size) as pilot:
            screen = WorkerObservationScreen(
                tmp_path, _LONG_ID, observer=_blocked_snapshot
            )
            await app.push_screen(screen)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            context = screen.query_one("#worker-context")
            facts = screen.query_one("#worker-context-facts", Static)
            # Never hidden below 140 columns: beside the stream when wide, under it when narrow.
            assert context.display and context.region.width >= 25
            text = str(facts.content)
            for needed in (
                _LONG_ID,
                _LONG_WORKTREE,
                "tooling blocked the commit: hook refused the message",
            ):
                assert needed in text
            assert facts.styles.text_overflow != "ellipsis"
            assert facts.region.width <= context.region.width
            assert _LONG_ID in str(
                screen.query_one("#worker-selection", Static).content
            )
            navigation = screen.query_one("#worker-navigation")
            menu = screen.query_one("#worker-sections", ListView)
            assert menu.index == SECTIONS.index("Orchestration")  # the open section
            if size[0] < 130:
                assert not navigation.display
                assert context.region.y > screen.query_one("#worker-output").region.y
                await pilot.click("#worker-menu")
                await pilot.pause()
                assert navigation.display
            else:
                assert navigation.display
                assert (
                    context.region.x >= screen.query_one("#worker-output").region.right
                )
            assert screen.region.contains_region(
                screen.query_one("#worker-actions").region
            )

    asyncio.run(scenario())


def test_worker_menu_navigation_runs_no_command_and_back_keys_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.console.screens.dashboard import DashboardScreen

    opened: list[int] = []

    async def record(_self: DashboardScreen, index: int) -> None:
        opened.append(index)

    monkeypatch.setattr(DashboardScreen, "action_open_section", record)

    async def scenario() -> None:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(160, 48)) as pilot:
            screen = WorkerObservationScreen(
                tmp_path, _LONG_ID, observer=_blocked_snapshot
            )
            await app.push_screen(screen)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.press("3")
            await pilot.pause()
            assert opened == [SECTIONS.index("Diagnostics")]
            await pilot.press("f2")
            await pilot.pause()
            await pilot.pause()
            crumbs = screen.query_one("#worker-breadcrumbs", Breadcrumbs).text
            assert crumbs.startswith("Home  ›  Orchestration")
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is not screen

    asyncio.run(scenario())


def test_worker_session_list_uses_the_shared_role_label_and_caption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rich.text import Text
    from textual.widgets import ListView

    from harness.console import brand, reports

    stages = [
        reports.StageEntry(
            "dispatch-a", "batch-a", "#721", "developer", "working", "2026-10-16"
        ),
        reports.StageEntry(
            "dispatch-b", "batch-a", "#722", "code-review", "blocked", "2026-10-15"
        ),
    ]
    monkeypatch.setattr(
        reports, "load_ledger_view", lambda _repo: reports.LedgerView(stages=stages)
    )

    async def scenario() -> list[Text]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(160, 48)) as pilot:
            screen = WorkerObservationScreen(tmp_path, observer=lambda *_: {})
            await app.push_screen(screen)
            await pilot.pause()
            picker = screen.query_one("#worker-picker", ListView)
            return [
                card
                for item in picker.children
                for card in [item.query_one(Static).content]
                if isinstance(card, Text)
            ]

    cards = asyncio.run(scenario())
    assert [card.plain.splitlines()[0] for card in cards] == [
        "#721  ·  developer",
        "#722  ·  code-review",
    ]
    assert cards[0].plain.splitlines()[-1] == "▶ Выполняется"
    assert cards[1].plain.splitlines()[-1] == "✖ Работа заблокирована"
    assert f"bold {brand.ROLE_COLORS['code-review']}" in {
        str(span.style) for span in cards[1].spans
    }


def test_worker_breadcrumbs_link_back_to_orchestration_and_home(tmp_path: Path) -> None:
    from harness.console.screens.dashboard import DashboardScreen
    from harness.console.screens.orchestration import OrchestrationScreen
    from harness.console.shell import CrumbLink

    async def scenario() -> tuple[list[str], bool, bool, int]:
        app = HarnessConsoleApp(tmp_path)
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause()
            orchestration = OrchestrationScreen(tmp_path)
            await app.push_screen(orchestration)
            worker = WorkerObservationScreen(
                tmp_path, _LONG_ID, observer=_blocked_snapshot
            )
            await app.push_screen(worker)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            links = [str(link.content) for link in worker.query(CrumbLink)]
            orchestration_link = list(worker.query(CrumbLink))[1]
            orchestration_link.focus()
            await pilot.press("enter")
            for _ in range(4):
                await pilot.pause()
            back_on_orchestration = app.screen is orchestration
            await app.push_screen(
                WorkerObservationScreen(tmp_path, _LONG_ID, observer=_blocked_snapshot)
            )
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            home_link = list(app.screen.query(CrumbLink))[0]
            await pilot.click(CrumbLink, offset=(1, 0))
            for _ in range(4):
                await pilot.pause()
            assert home_link
            return (
                links,
                back_on_orchestration,
                isinstance(app.screen, DashboardScreen),
                len(app.screen_stack),
            )

    links, back_on_orchestration, on_dashboard, depth = asyncio.run(scenario())
    assert links[:2] == ["Главная", "Оркестрация"]
    assert len(links) == 3  # the observed batch links too
    assert back_on_orchestration and on_dashboard and depth == 2
