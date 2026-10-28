"""The ledger lock in the Orchestration section (issue #759).

The panel reads the same read-only state as `coordinator.py ledger lock-status`, shows how long
the lock has been held and how much is left until the timeout release, and refreshes on the live
screen. The forced release is a terminal coordinator command that asks for confirmation. Pilot
scenarios run on a narrow and a wide terminal with a recording runner.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Sequence

import pytest

from harness.console import ledger_lock
from harness.json_types import JsonObject
from harness.orchestration.core.constants import LEDGER_LOCK_STALE_SECONDS

STALE = LEDGER_LOCK_STALE_SECONDS


def _status(**overrides: object) -> JsonObject:
    status: JsonObject = {
        "locked": True,
        "held_seconds": 200,
        "remaining_seconds": STALE - 200,
        "owner": {"pid": 5, "host": "fedora", "pid_namespace": "pid:[1]"},
        "operation": "report submit",
        "verdict": "owner-in-other-environment",
        "release_available": False,
    }
    status.update(overrides)
    return status


def test_a_free_lock_says_so() -> None:
    assert ledger_lock.lock_text({"locked": False}) == "Lock ledger свободен."


def test_the_store_epoch_shows_the_qa_lease_with_its_verdict() -> None:
    lease = {
        "owner": {"owner_kind": "dispatch", "owner_id": "dispatch-1"},
        "fencing_token": 7,
        "expires_at": "2026-10-28T00:05:00+00:00",
        "stale_heartbeat": True,
        "verdict": "owner-dead",
        "release_available": True,
    }

    free = ledger_lock.lock_text({"locked": False, "qa_lease": None})
    held = ledger_lock.lock_text({"locked": False, "qa_lease": lease})
    english = ledger_lock.lock_text({"locked": False, "qa_lease": lease}, "en")
    reused = ledger_lock.lock_text(
        {"locked": False, "qa_lease": {**lease, "verdict": "owner-pid-reused"}}
    )
    broken = ledger_lock.lock_text({"locked": False, "qa_lease": {"error": "x: y"}})

    assert free == "Lock ledger свободен.\nLease QA-lane свободен."
    assert "Lease QA-lane: dispatch dispatch-1 · fencing 7" in held
    assert "heartbeat пропущен" in held
    assert "owner-dead" in held
    assert "QA lease: dispatch dispatch-1 · fencing 7" in english
    assert "heartbeat missed" in english
    assert "не доказывает смерть" in reused
    assert "состояние недоступно: x: y" in broken
    # A file-lock panel of the file epoch has no lease line.
    assert "Lease" not in ledger_lock.lock_text(_status())


def test_the_panel_names_the_holder_its_age_and_the_time_left() -> None:
    text = ledger_lock.lock_text(_status())

    assert "Lock ledger занят: report submit" in text
    assert "pid 5 · fedora · pid:[1]" in text
    assert "Держится: 3 мин 20 с" in text
    assert f"до снятия по таймауту: {ledger_lock.duration(STALE - 200)}" in text
    assert "owner-in-other-environment" in text


def test_an_available_release_says_available() -> None:
    for verdict in ("owner-dead", "owner-unknown-stale"):
        text = ledger_lock.lock_text(
            _status(verdict=verdict, remaining_seconds=0, release_available=True)
        )
        assert "до снятия по таймауту: доступно" in text


def test_a_live_owner_is_never_announced_as_released_by_the_timeout() -> None:
    text = ledger_lock.lock_text(
        _status(verdict="owner-alive", remaining_seconds=0, release_available=False)
    )

    assert "доступно" not in text
    assert "таймаут не снимает живого владельца" in text


def test_an_owner_on_another_host_has_no_timeout_countdown() -> None:
    text = ledger_lock.lock_text(_status(verdict="owner-on-another-host"))

    assert "до снятия по таймауту: не применяется (владелец на другом хосте)" in text


def test_the_english_console_gets_an_english_panel() -> None:
    text = ledger_lock.lock_text(_status(), "en")

    assert "Ledger lock held: report submit" in text
    assert "Held for: 3 min 20 s" in text
    assert "until the timeout release:" in text
    assert "another PID namespace" in text
    assert not any("а" <= char <= "я" for char in text.lower())
    assert ledger_lock.lock_text({"locked": False}, "en") == "The ledger lock is free."


def test_durations_read_in_hours_minutes_and_seconds() -> None:
    assert ledger_lock.duration(45) == "45 с"
    assert ledger_lock.duration(200) == "3 мин 20 с"
    assert ledger_lock.duration(3900) == "1 ч 05 мин"
    assert ledger_lock.duration(-3) == "0 с"


def test_the_real_loader_shows_an_orphaned_worker_lock(tmp_path: Path) -> None:
    lock = tmp_path / ".harness/orchestration/state/.coordinator.lock"
    lock.mkdir(parents=True)
    (lock / "owner.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "acquired_at": datetime.now(UTC).isoformat(),
                "pid_namespace": "pid:[1]",
                "boot_id": "boot-of-another-sandbox",
                "operation": "dispatch heartbeat",
            }
        ),
        encoding="utf-8",
    )

    text = ledger_lock.current_lock_text(tmp_path)

    assert "Lock ledger занят: dispatch heartbeat" in text
    assert "owner-in-other-environment" in text


def test_an_unreadable_state_is_shown_not_raised(tmp_path: Path) -> None:
    def broken(_repo: Path) -> JsonObject:
        raise OSError("state is unreadable")

    text = ledger_lock.current_lock_text(tmp_path, broken)

    assert text == "Lock ledger: состояние недоступно: state is unreadable"


textual = pytest.importorskip("textual")

from textual.app import App  # noqa: E402
from textual.widgets import Checkbox, ListItem, ListView, Static  # noqa: E402

from harness.console.coordinator_catalog import (  # noqa: E402
    COORDINATOR_COMMANDS,
    process_argv,
)
from harness.console.screens.orchestration import (  # noqa: E402
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


class _Ticking:
    """A lock whose age grows by one second on every read, like a lock still held."""

    def __init__(self) -> None:
        self.held = 200
        self.served: list[int] = []

    def __call__(self, _repo: Path) -> JsonObject:
        self.held += 1
        self.served.append(self.held)
        return _status(held_seconds=self.held, remaining_seconds=STALE - self.held)

    def held_shown_in(self, text: str) -> int:
        """The age a panel text shows, among the ages this loader served."""
        shown = [
            held
            for held in self.served
            if f"Держится: {ledger_lock.duration(held)} · до снятия по таймауту: "
            f"{ledger_lock.duration(STALE - held)}" in text
        ]
        assert shown, text
        return shown[-1]


@pytest.mark.parametrize("size", [(64, 30), (160, 50)], ids=["narrow", "wide"])
def test_the_lock_panel_counts_on_the_live_screen(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    ticking = _Ticking()

    async def scenario() -> tuple[str, str]:
        screen = OrchestrationScreen(
            tmp_path,
            command_runner=_RecordingRunner(),
            load_lock=ticking,
            lock_refresh_seconds=0.05,
        )
        app = _HostApp(screen)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            first = str(app.screen.query_one("#ledger-lock", Static).content)
            await pilot.pause(0.3)
            later = str(app.screen.query_one("#ledger-lock", Static).content)
        return first, later

    first, later = asyncio.run(scenario())
    assert "Lock ledger занят: report submit" in first
    # The timer may read the lock again after a snapshot, so each text is matched against the
    # ages the loader actually served rather than its latest one.
    assert ticking.held_shown_in(later) > ticking.held_shown_in(first)


@pytest.mark.parametrize("size", [(64, 40), (160, 50)], ids=["narrow", "wide"])
def test_the_forced_release_asks_and_runs_with_the_exact_argv(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    runner = _RecordingRunner()
    values = {
        "force": "1",
        "approved_by": "dev",
        "approved_at": "2026-10-18T00:00:00Z",
        "reason": "orphaned worker",
    }

    async def scenario() -> bool:
        screen = OrchestrationScreen(
            tmp_path,
            command_runner=runner,
            load_lock=lambda _repo: _status(),
            lock_refresh_seconds=60,
        )
        app = _HostApp(screen)
        async with app.run_test(size=size) as pilot:
            menu = app.screen.query_one("#command-menu", ListView)
            menu.index = [item.name for item in menu.query(ListItem)].index(
                "ledger-release-lock"
            )
            await pilot.press("enter")
            await pilot.pause()
            asked = isinstance(app.screen, CoordinatorCommandFormScreen) and bool(
                app.screen.query("#confirmation-reason")
            )
            app.screen.query_one("#input-force", Checkbox).value = True
            for dest in ("approved_by", "approved_at", "reason"):
                app.screen.query_one(f"#input-{dest}").value = values[dest]  # type: ignore[attr-defined]
            app.screen.query_one("#run").press()  # type: ignore[attr-defined]
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
        return asked

    asked = asyncio.run(scenario())
    entry = next(e for e in COORDINATOR_COMMANDS if e.key == "ledger-release-lock")
    assert asked
    assert runner.calls == [process_argv(entry.cli_argv(tmp_path, values))]
