"""Opt-in Harness execution must not change other command screens' defaults."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
from threading import Event
from typing import Mapping, Sequence

from harness.console.runner import HarnessRuns
from harness.console.screens.commands import render_result


def test_full_result_is_opt_in() -> None:
    result = subprocess.CompletedProcess(
        [], 0, "FIRST\n" + "line\n" * 300 + "LAST", "ERR"
    )
    assert "FIRST" not in render_result("probe", result)
    assert all(
        part in render_result("probe", result, full=True)
        for part in ("FIRST", "LAST", "ERR")
    )


def test_abandoned_pending_run_cannot_start_or_release_a_new_admission(
    tmp_path: Path,
) -> None:
    state = HarnessRuns()
    old = state.reserve("old", "old", ["old"], {})
    assert old is not None
    state.abandon_pending(old)
    current = state.reserve("current", "current", ["current"], {})
    assert current is not None
    calls: list[list[str]] = []

    def runner(
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "CURRENT", "")

    state.execute(old, runner, tmp_path)
    assert state.busy
    assert state.current is current
    assert calls == []
    state.execute(current, runner, tmp_path)
    assert not state.busy
    assert current.stdout == "CURRENT"


def test_running_admission_ends_only_when_runner_returns(tmp_path: Path) -> None:
    state = HarnessRuns()
    run = state.reserve("probe", "probe", ["probe"], {})
    assert run is not None
    started, release = Event(), Event()

    def runner(
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        started.set()
        assert release.wait(5)
        raise OSError("probe failure")

    with ThreadPoolExecutor() as pool:
        work = pool.submit(state.execute, run, runner, tmp_path)
        try:
            assert started.wait(1)
            state.abandon_pending(run)
            assert state.busy
            assert state.reserve("second", "second", [], {}) is None
        finally:
            release.set()
        work.result()
    assert not state.busy
    assert "probe failure" in run.error
