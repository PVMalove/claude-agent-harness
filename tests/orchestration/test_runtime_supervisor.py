"""The process supervisor of the runtime at seam 1: the coordinator CLI as its own process, the real
``harness-runtime`` binary, and a fake foreground adapter (ADR 0021, sections 4 and 6; ADR 0022).

Every test starts real processes; the fixture kills every process group the dispatch left."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol, cast

import pytest

from tests.orchestration.runtime_store_support import (
    APPROVAL,
    Supervised,
    architect_report_file,
    store_call,
    store_records,
)

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="the runtime supervises processes on Linux",
)


class Factory(Protocol):
    def __call__(
        self, *, timeout: int = ..., grace: int = ..., env: dict[str, str] | None = ...
    ) -> Supervised: ...


@pytest.fixture
def supervised(tmp_path: Path) -> Iterator[Factory]:
    made: list[Supervised] = []

    def make(
        *, timeout: int = 600, grace: int = 1, env: dict[str, str] | None = None
    ) -> Supervised:
        repo = Supervised(
            tmp_path / f"s{len(made)}", timeout=timeout, grace=grace, env=env
        )
        made.append(repo)
        return repo

    yield make
    for repo in made:
        repo.close()


def alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] not in {"Z", "X"}


def dig(value: object, *keys: str) -> object:
    """The member at ``keys`` of nested JSON objects."""
    for key in keys:
        assert isinstance(value, dict), value
        value = value[key]
    return value


def sent(result: object) -> dict[str, object]:
    assert getattr(result, "returncode") == 0, getattr(result, "stderr")
    answer: dict[str, object] = json.loads(getattr(result, "stdout"))
    return answer


def test_dispatch_send_launches_once_from_the_runtime_and_a_repeat_returns_the_same_receipt(
    supervised: Factory,
) -> None:
    repo = supervised()

    first = sent(repo.send())
    second = sent(repo.send())

    assert first["execution"] == second["execution"]
    (launch,) = repo.wait_launches(1)
    assert launch.ppid == repo.runtime.process.pid
    assert launch.pgid == launch.pid
    assert launch.operation_id == dig(repo.execution(), "execution_id")
    time.sleep(0.5)
    assert len(repo.launches()) == 1
    assert repo.status()["state"] == "dispatched"


def proc_start_time(pid: int) -> str:
    return (
        Path(f"/proc/{pid}/stat")
        .read_text(encoding="utf-8")
        .rsplit(")", 1)[1]
        .split()[19]
    )


def test_the_receipt_names_the_boot_the_pid_namespace_the_pid_and_the_start_time(
    supervised: Factory,
) -> None:
    repo = supervised()

    receipt = sent(repo.send())["execution"]

    (launch,) = repo.wait_launches(1)
    assert dig(receipt, "identity", "pid") == launch.pid == dig(receipt, "pgid")
    assert dig(receipt, "identity", "start_time") == proc_start_time(launch.pid)
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    assert dig(receipt, "identity", "boot_id") == boot_id
    assert dig(receipt, "identity", "pid_namespace") == os.readlink(
        f"/proc/{launch.pid}/ns/pid"
    )
    assert dig(receipt, "accounting") == "process-group"
    assert dig(receipt, "adopted") is False


@pytest.mark.parametrize(
    "variable", ["HARNESS_RUNTIME_TEST_BOOT_ID", "HARNESS_RUNTIME_TEST_PID_NAMESPACE"]
)
def test_an_execution_seen_from_another_boot_or_pid_namespace_stays_unknown_and_is_never_stopped(
    supervised: Factory, variable: str
) -> None:
    repo = supervised(timeout=2)
    sent(repo.send())
    (launch,) = repo.wait_launches(1)

    repo.restart_runtime({variable: "elsewhere"})

    execution = repo.wait_execution("unknown")
    assert dig(execution, "lease", "released_at") is None
    time.sleep(3)  # past the deadline: an unknown execution is never stopped
    assert repo.wait_execution("unknown")["exit"] is None
    assert alive(launch.pid)
    assert repo.execution_actions()[-1] == "execution-unknown"


def test_a_reused_pid_is_lost_and_the_live_process_is_never_signalled(
    supervised: Factory,
) -> None:
    repo = supervised()
    sent(repo.send())
    (launch,) = repo.wait_launches(1)
    repo.runtime.kill()
    # Another process with another start time holds the recorded pid now.
    with sqlite3.connect(repo.database) as db:
        db.execute("UPDATE executions SET start_time = '1'")

    repo.restart_runtime()

    execution = repo.wait_execution("lost")
    assert dig(execution, "observation", "verdict") == "owner-pid-reused"
    assert dig(execution, "lease", "released_at") is not None
    assert alive(launch.pid)


def test_the_execution_moves_from_launching_through_running_and_stopping_to_exited(
    supervised: Factory,
) -> None:
    repo = supervised(timeout=2, grace=1)
    sent(repo.send())

    repo.wait_execution("exited", timeout=20)

    assert repo.execution_actions() == [
        "execution-launching",
        "execution-running",
        "execution-stopping",
        "execution-exited",
    ]
    assert repo.rows(
        "SELECT execution_state FROM dispatch_status WHERE id = ?", repo.dispatch
    ) == [("exited",)]


@pytest.mark.parametrize("ignores_term", [False, True], ids=["obeys", "ignores"])
def test_a_deadline_stop_signals_the_group_and_releases_the_lease_only_after_the_exit(
    supervised: Factory, ignores_term: bool
) -> None:
    repo = supervised(timeout=2, grace=2)
    if ignores_term:
        repo.ignore_term()
    sent(repo.send())
    (launch,) = repo.wait_launches(1)

    if ignores_term:
        stopping = repo.wait_execution("stopping", timeout=10)
        assert dig(stopping, "lease", "released_at") is None
        assert alive(launch.pid)
    exited = repo.wait_execution("exited", timeout=20)

    assert dig(exited, "exit", "stopped_by") == "deadline"
    assert dig(exited, "exit", "signal") == (9 if ignores_term else 15)
    assert (dig(exited, "exit", "kill_sent_at") is not None) is ignores_term
    assert dig(exited, "lease", "released_at") is not None
    assert not alive(launch.pid)


def test_a_stop_begun_before_a_runtime_restart_is_driven_to_its_proof_by_reconcile(
    supervised: Factory,
) -> None:
    repo = supervised(timeout=2, grace=4)
    repo.ignore_term()
    sent(repo.send())
    (launch,) = repo.wait_launches(1)
    stopping = repo.wait_execution("stopping", timeout=10)

    # The runtime dies after SIGTERM and before SIGKILL; the worker ignores SIGTERM.
    repo.restart_runtime()

    exited = repo.wait_execution("exited", timeout=30)
    assert dig(exited, "exit", "stopped_by") == "deadline"
    assert dig(exited, "exit", "source") == "stop-observed"
    assert dig(exited, "exit", "term_sent_at") == dig(
        stopping, "observation", "term_sent_at"
    )
    assert dig(exited, "exit", "kill_sent_at") is not None
    assert dig(exited, "lease", "released_at") is not None
    assert not alive(launch.pid)
    assert repo.execution_actions() == [
        "execution-launching",
        "execution-running",
        "execution-stopping",
        "execution-exited",
    ]


def test_a_stop_resumed_within_its_grace_sends_sigterm_again(
    supervised: Factory,
) -> None:
    repo = supervised(timeout=2, grace=8)
    repo.count_term()
    sent(repo.send())
    (launch,) = repo.wait_launches(1)
    repo.wait_execution("stopping", timeout=10)
    assert repo.wait_terms(1, timeout=5) == 1

    repo.restart_runtime()

    assert repo.wait_terms(2, timeout=6) == 2
    exited = repo.wait_execution("exited", timeout=30)
    assert dig(exited, "exit", "kill_sent_at") is not None
    assert not alive(launch.pid)


def test_a_runtime_killed_after_the_sigterm_and_before_its_record_stops_the_execution_again(
    supervised: Factory,
) -> None:
    repo = supervised(
        timeout=2,
        grace=2,
        env={"HARNESS_RUNTIME_TEST_PAUSE_AT": "execution.stop:signalled"},
    )
    repo.count_term()
    sent(repo.send())
    (launch,) = repo.wait_launches(1)
    repo.runtime.wait_paused("execution.stop:signalled", timeout=15)
    assert repo.wait_terms(1, timeout=5) == 1

    # The SIGTERM went out, its record did not: the row still says running.
    assert repo.rows(
        "SELECT state FROM executions WHERE dispatch_id = ?", repo.dispatch
    ) == [("running",)]
    repo.restart_runtime()

    exited = repo.wait_execution("exited", timeout=30)
    assert repo.terms() == 2
    assert dig(exited, "exit", "stopped_by") == "deadline"
    assert not alive(launch.pid)
    assert repo.execution_actions() == [
        "execution-launching",
        "execution-running",
        "execution-stopping",
        "execution-exited",
    ]


def test_an_adopted_leader_that_exits_before_its_group_keeps_the_lease_until_the_group_ends(
    supervised: Factory,
) -> None:
    repo = supervised()
    repo.add_member()
    sent(repo.send())
    (launch,) = repo.wait_launches(1)
    member = repo.member()
    repo.restart_runtime()

    repo.release(0)
    deadline = time.monotonic() + 10
    while alive(launch.pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not alive(launch.pid)
    # Reconcile observes the adopted execution every second.
    time.sleep(2.5)

    running = repo.execution()
    assert running is not None and running["state"] == "running", running
    assert dig(running, "lease", "released_at") is None
    assert alive(member)
    repo.release_member()
    lost = repo.wait_execution("lost", timeout=20)
    assert dig(lost, "lease", "released_at") is not None
    assert repo.execution_actions() == [
        "execution-launching",
        "execution-running",
        "execution-lost",
    ]


def test_a_lost_response_after_the_receipt_is_answered_from_the_stored_receipt_with_one_launch(
    supervised: Factory,
) -> None:
    repo = supervised(env={"HARNESS_RUNTIME_TEST_FAIL_AT": "execution.launch:respond"})

    answer = sent(repo.send())

    assert dig(answer, "execution", "state") == "running"
    assert len(repo.wait_launches(1)) == 1
    time.sleep(0.5)
    assert len(repo.launches()) == 1


def test_a_runtime_killed_between_launch_and_receipt_adopts_the_process_without_a_second_launch(
    supervised: Factory,
) -> None:
    repo = supervised(
        env={"HARNESS_RUNTIME_TEST_PAUSE_AT": "execution.launch:launched"}
    )
    send = repo.start_send()
    repo.runtime.wait_paused("execution.launch:launched")
    (launch,) = repo.wait_launches(1)

    repo.restart_runtime()
    out, err = send.communicate(timeout=60)

    assert send.returncode == 0, err
    receipt = json.loads(out)["execution"]
    assert dig(receipt, "adopted") is True
    assert dig(receipt, "identity", "pid") == launch.pid
    assert len(repo.launches()) == 1
    assert (
        dig(repo.execution(), "lease", "owner", "identity", "pid")
        == repo.runtime.process.pid
    )


def test_self_report_after_an_adopted_launch_moves_the_dispatch_to_working(
    supervised: Factory,
) -> None:
    repo = supervised(
        env={"HARNESS_RUNTIME_TEST_PAUSE_AT": "execution.launch:launched"}
    )
    send = repo.start_send()
    repo.runtime.wait_paused("execution.launch:launched")
    repo.wait_launches(1)
    repo.restart_runtime()
    out, _ = send.communicate(timeout=60)
    worktree = json.loads(out)["worker_worktree"]

    reported = repo.run(
        "dispatch",
        "self-report",
        "--dispatch",
        repo.dispatch,
        "--model",
        "sonnet",
        "--worktree",
        worktree,
    )

    assert reported.returncode == 0, reported.stderr
    assert repo.status()["state"] == "working"
    assert dig(repo.execution(), "state") == "running"


def test_the_intent_is_committed_before_the_process_starts(supervised: Factory) -> None:
    repo = supervised(env={"HARNESS_RUNTIME_TEST_PAUSE_AT": "execution.launch:intent"})
    send = repo.start_send()
    repo.runtime.wait_paused("execution.launch:intent")

    assert repo.rows("SELECT state FROM executions") == [("launching",)]
    assert repo.rows(
        "SELECT state FROM operations WHERE operation = 'execution.launch'"
    ) == [("intent",)]
    assert repo.launches() == []
    repo.runtime.resume()
    _, err = send.communicate(timeout=60)
    assert send.returncode == 0, err
    assert len(repo.wait_launches(1)) == 1


def test_a_runtime_killed_before_the_launch_leaves_a_lost_execution_and_no_process(
    supervised: Factory,
) -> None:
    repo = supervised(env={"HARNESS_RUNTIME_TEST_PAUSE_AT": "execution.launch:intent"})
    send = repo.start_send()
    repo.runtime.wait_paused("execution.launch:intent")

    repo.restart_runtime()
    _, err = send.communicate(timeout=60)

    assert send.returncode == 2
    assert "never relaunched" in err
    assert dig(repo.execution(), "state") == "lost"
    assert repo.rows(
        "SELECT state FROM operations WHERE operation = 'execution.launch'"
    ) == [("failed",)]
    assert repo.launches() == []
    assert repo.status()["state"] == "blocked"


def test_a_store_failure_before_the_launch_starts_nothing_and_a_repeat_launches_once(
    supervised: Factory,
) -> None:
    repo = supervised(
        env={"HARNESS_RUNTIME_TEST_FAIL_AT": "execution.launch:in-transaction"}
    )

    failed = repo.send()

    assert failed.returncode == 2
    assert "repeat dispatch send" in failed.stderr
    assert repo.rows("SELECT COUNT(*) FROM executions") == [(0,)]
    assert repo.status()["state"] == "dispatched"
    assert repo.launches() == []
    sent(repo.send())
    assert len(repo.wait_launches(1)) == 1


def test_a_failed_receipt_write_keeps_the_intent_and_the_repeat_adopts_the_process(
    supervised: Factory,
) -> None:
    repo = supervised(env={"HARNESS_RUNTIME_TEST_FAIL_AT": "execution.launch:receipt"})

    failed = repo.send()

    assert failed.returncode == 2
    assert "repeat dispatch send" in failed.stderr
    assert repo.rows(
        "SELECT state FROM operations WHERE operation = 'execution.launch'"
    ) == [("intent",)]
    assert len(repo.wait_launches(1)) == 1
    receipt = sent(repo.send())["execution"]
    assert dig(receipt, "adopted") is True
    assert len(repo.launches()) == 1


def test_a_second_send_during_a_launch_returns_the_same_receipt(
    supervised: Factory,
) -> None:
    repo = supervised(
        env={"HARNESS_RUNTIME_TEST_PAUSE_AT": "execution.launch:launched"}
    )
    first = repo.start_send()
    repo.runtime.wait_paused("execution.launch:launched")
    second = repo.start_send()
    time.sleep(1.0)

    repo.runtime.resume()
    answers = [process.communicate(timeout=60) for process in (first, second)]

    assert [process.returncode for process in (first, second)] == [0, 0], answers
    receipts = [dig(json.loads(out), "execution") for out, _ in answers]
    assert receipts[0] == receipts[1]
    assert len(repo.launches()) == 1


def test_a_send_refused_before_the_intent_launches_once_on_repeat(
    supervised: Factory,
) -> None:
    repo = supervised(
        env={"HARNESS_RUNTIME_TEST_FAIL_AT": "execution.launch:before-begin"}
    )

    failed = repo.send()

    assert failed.returncode == 2
    assert repo.rows(
        "SELECT COUNT(*) FROM operations WHERE operation = 'execution.launch'"
    ) == [(0,)]
    sent(repo.send())
    assert len(repo.wait_launches(1)) == 1


def test_a_launch_the_os_refuses_is_a_failed_operation_and_blocks_the_dispatch(
    supervised: Factory,
) -> None:
    repo = supervised()
    # A file that is no program: the OS refuses to execute it.
    repo.adapter = repo.adapter.with_suffix("")
    repo.adapter.write_text("not a program\n", encoding="utf-8")
    repo.adapter.chmod(0o644)

    failed = repo.send()

    assert failed.returncode == 2
    assert "could not launch the worker" in failed.stderr
    status = repo.status()
    assert status["state"] == "blocked"
    assert dig(status, "runtime_failure", "worker_started") is False
    execution = repo.execution()
    assert execution is not None and execution["state"] == "lost"
    assert repo.rows(
        "SELECT state FROM operations WHERE operation = 'execution.launch'"
    ) == [("failed",)]


def test_a_cancel_of_a_running_execution_is_refused_until_its_stop_is_proven(
    supervised: Factory,
) -> None:
    repo = supervised(timeout=3, grace=1)
    sent(repo.send())
    cancel = (
        "dispatch",
        "cancel",
        "--dispatch",
        repo.dispatch,
        "--approved-by",
        APPROVAL["approved_by"],
        "--approved-at",
        APPROVAL["approved_at"],
        "--runtime-stopped",
        "--reason",
        "the work is not needed",
    )

    refused = repo.run(*cancel)

    assert refused.returncode == 2
    assert repo.status()["state"] == "dispatched"
    repo.wait_execution("exited", timeout=20)
    accepted = repo.run(*cancel)
    assert accepted.returncode == 0, accepted.stderr
    assert repo.status()["state"] == "cancelled"


def test_a_broken_audit_chain_refuses_the_launch_before_any_process_starts(
    supervised: Factory,
) -> None:
    repo = supervised(
        env={"HARNESS_RUNTIME_TEST_PAUSE_AT": "execution.launch:before-begin"}
    )
    send = repo.start_send()
    repo.runtime.wait_paused("execution.launch:before-begin")
    with sqlite3.connect(repo.database) as db:
        db.execute(
            "INSERT INTO audit (seq, at, action, details_json, record_sha256, prev_chain_sha256,"
            " chain_sha256) SELECT MAX(seq) + 1, 't', 'transition', '{}', 'x', 'y', 'z' FROM audit"
        )

    repo.runtime.resume()
    _, err = send.communicate(timeout=60)

    assert send.returncode == 2
    assert "invariant_violation" in err
    assert repo.rows("SELECT COUNT(*) FROM executions") == [(0,)]
    assert repo.launches() == []


def test_a_restart_writes_the_execution_rows_and_never_a_dispatch_state(
    supervised: Factory,
) -> None:
    repo = supervised(
        env={"HARNESS_RUNTIME_TEST_PAUSE_AT": "execution.launch:launched"}
    )
    send = repo.start_send()
    repo.runtime.wait_paused("execution.launch:launched")
    repo.wait_launches(1)
    before = repo.rows("SELECT state, revision, body_json FROM dispatch_status")
    (last,) = repo.rows("SELECT MAX(seq) FROM audit")

    repo.restart_runtime()

    assert repo.rows("SELECT state, revision, body_json FROM dispatch_status") == before
    actions = [
        a for (a,) in repo.rows("SELECT action FROM audit WHERE seq > ?", last[0])
    ]
    assert actions == ["execution-running"]
    send.communicate(timeout=60)


def test_a_restart_without_a_live_execution_changes_nothing(
    supervised: Factory,
) -> None:
    repo = supervised()
    sent(repo.send())
    repo.release(0)
    repo.wait_execution("exited")
    before = repo.rows("SELECT MAX(seq) FROM audit")

    repo.restart_runtime()

    assert repo.rows("SELECT MAX(seq) FROM audit") == before
    assert dig(repo.execution(), "state") == "exited"


def test_an_invalid_config_refuses_dispatch_send_before_any_launch_and_changes_nothing(
    supervised: Factory,
) -> None:
    repo = supervised()
    (repo.repo / ".harness" / "orchestration.json").write_text("{", encoding="utf-8")
    before = repo.rows("SELECT MAX(seq) FROM audit")

    failed = repo.send()

    assert failed.returncode == 2
    assert repo.rows("SELECT MAX(seq) FROM audit") == before
    assert repo.launches() == []


def test_an_unknown_dispatch_is_refused_and_launches_nothing(
    supervised: Factory,
) -> None:
    from harness.orchestration.runtime_client import RuntimeClientError

    repo = supervised()
    unknown = "dispatch-00000000-0000-4000-8000-000000000000"
    before = repo.rows("SELECT MAX(seq) FROM audit")

    failed = repo.run(
        "dispatch", "send", "--dispatch", unknown, "--adapter", str(repo.adapter)
    )
    with pytest.raises(RuntimeClientError) as refused:
        store_call(
            repo.repo, repo.runtime, "execution.launch", {"dispatch_id": unknown}
        )

    assert failed.returncode == 2
    assert refused.value.code == "not_found"
    assert repo.rows("SELECT MAX(seq) FROM audit") == before
    assert repo.launches() == []


def _working(repo: Supervised) -> None:
    worktree = dig(sent(repo.send()), "worker_worktree")
    reported = repo.run(
        "dispatch",
        "self-report",
        "--dispatch",
        repo.dispatch,
        "--model",
        "sonnet",
        "--worktree",
        str(worktree),
    )
    assert reported.returncode == 0, reported.stderr


def _status_of(repo: Supervised) -> object:
    viewed = repo.run("dispatch", "status", "--dispatch", repo.dispatch)
    assert viewed.returncode == 0, viewed.stderr
    return dig(json.loads(viewed.stdout), "dispatches")


def test_a_report_submitted_while_the_execution_runs_is_accepted_after_it_exits(
    supervised: Factory,
) -> None:
    repo = supervised()
    _working(repo)
    path = f"dispatches/{repo.dispatch}.json"
    brief = store_records(repo.repo, repo.runtime, path)[path]

    submitted = repo.run(
        "report",
        "submit",
        "--file",
        str(architect_report_file(repo.repo, repo.dispatch, brief)),
    )

    assert submitted.returncode == 0, submitted.stderr
    answer = json.loads(submitted.stdout)
    assert (answer["state"], answer["awaiting"]) == ("working", "execution-exit")
    assert repo.status()["state"] == "working"
    repo.release(0)
    repo.wait_execution("exited")
    _status_of(repo)
    assert repo.status()["state"] == "reported"
    assert "pending_report" not in repo.status()


def test_an_execution_that_exits_without_a_report_blocks_the_dispatch_with_its_domain(
    supervised: Factory,
) -> None:
    repo = supervised(timeout=2, grace=1)
    _working(repo)
    repo.wait_execution("exited", timeout=20)

    (entry,) = cast(list[object], _status_of(repo))

    status = repo.status()
    assert status["state"] == "blocked"
    assert dig(status, "runtime_failure", "kind") == "timeout"
    assert dig(entry, "execution", "state") == "exited"


def test_a_lost_execution_blocks_the_dispatch_as_infrastructure(
    supervised: Factory,
) -> None:
    repo = supervised()
    _working(repo)
    repo.runtime.kill()
    with sqlite3.connect(repo.database) as db:
        db.execute("UPDATE executions SET start_time = '1'")
    repo.restart_runtime()

    _status_of(repo)

    status = repo.status()
    assert status["state"] == "blocked"
    assert dig(status, "runtime_failure", "kind") == "runtime-unavailable"
    assert dig(status, "runtime_failure", "execution_state") == "lost"


def test_a_report_submitted_before_the_execution_is_lost_stays_for_the_operator(
    supervised: Factory,
) -> None:
    repo = supervised()
    _working(repo)
    path = f"dispatches/{repo.dispatch}.json"
    brief = store_records(repo.repo, repo.runtime, path)[path]
    report = architect_report_file(repo.repo, repo.dispatch, brief)
    submitted = repo.run("report", "submit", "--file", str(report))
    assert submitted.returncode == 0, submitted.stderr
    held = repo.status()["pending_report"]
    repo.runtime.kill()
    with sqlite3.connect(repo.database) as db:
        db.execute("UPDATE executions SET start_time = '1'")
    repo.restart_runtime()

    (entry,) = cast(list[object], _status_of(repo))

    status = repo.status()
    assert status["state"] == "blocked"
    assert dig(status, "runtime_failure", "execution_state") == "lost"
    assert "pending_report" not in status
    assert status["held_report"] == held
    assert dig(entry, "held_report") == held
