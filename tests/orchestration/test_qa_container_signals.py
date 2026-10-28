"""SIGINT, SIGTERM and a reaped lane owner stop a running container QA (ADR 0022, section 3).

The coordinator's ``qa run`` runs as its own process against the real runtime and the scripted
``podman``. A signal stops the running container through ``qa.stop`` with its grace: the commands
that ran keep their evidence in the attempt record, the QA lane is free and the dispatch is
approved again. A lane owner that died leaves its run to the next admission, which stops it before
the next attempt starts.
"""

from __future__ import annotations

import signal
import sys
import time
from collections.abc import Callable

import pytest

from tests.orchestration.runtime_store_support import coordinator_popen
from tests.orchestration.test_qa_container import Make, qa  # noqa: F401

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="container QA runs only on Linux",
)


def _wait_for(condition: Callable[[], bool], timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "the condition did not hold in time"
        time.sleep(0.1)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_stops_the_running_container_and_keeps_the_partial_evidence(
    qa: Make,  # noqa: F811
    signum: signal.Signals,
) -> None:
    repo = qa()
    repo.engine.script("gate-1", stdout="lint ok\n")
    repo.engine.script("gate-2", stdout="testing\n", hang=True)
    process = coordinator_popen(repo.repo, repo.runtime, "qa", "run", "--dispatch", repo.dispatch)
    _wait_for(lambda: len(repo.engine.runs()) == 2)

    process.send_signal(signum)
    _, stderr = process.communicate(timeout=120)

    assert process.returncode == (143 if signum == signal.SIGTERM else -signal.SIGINT), stderr
    [stop] = [call for call in repo.engine.calls() if call[1] == "stop"]
    assert stop[2:4] == ["--time", "1"] and stop[-1].endswith("-gate-2")
    assert repo.engine.state()["objects"] == {}
    execution = repo.execution()
    assert (execution["state"], execution["exit"]["stopped_by"]) == ("exited", "command")
    records = execution["exit"]["commands"]
    assert [(r["result"], r["exit_code"], r["domain"]) for r in records] == [
        ("ran", 0, None), ("ran", 143, "cancelled"), ("not_run", None, None),
    ]
    [attempt] = repo.attempts()
    assert (attempt["attempt"], attempt["domain"]) == (1, "cancelled")
    assert attempt["stage_evidence"].startswith("stage-evidence: qa-artifacts/")
    assert repo.status()["state"] == "approved"
    lane = repo.lane()
    assert (lane["lease"], lane["queue"]) == (None, [])


def test_a_reaped_lane_owner_has_its_live_run_stopped_before_the_next_attempt(
    qa: Make,  # noqa: F811
) -> None:
    repo = qa(lease_ttl=2)
    repo.engine.script("gate-2", hang=True)
    process = coordinator_popen(repo.repo, repo.runtime, "qa", "run", "--dispatch", repo.dispatch)
    _wait_for(lambda: len(repo.engine.runs()) == 2)
    process.kill()
    process.communicate(timeout=30)
    _wait_for(lambda: bool(repo.lane()["lease_stale"]))
    repo.engine.script("gate-2", exit_code=0)

    code, result, err = repo.qa_run()

    assert code == 0 and result is not None, err
    assert repo.report()["outcome"] == "completed"
    [stop] = [call for call in repo.engine.calls() if call[1] == "stop"]
    assert stop[-1].endswith("-qa1-gate-2")
    assert repo.execution()["attempt"] == 2
    assert repo.engine.state()["objects"] == {}
