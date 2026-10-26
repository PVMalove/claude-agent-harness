"""The failures of a write on seam 1: a real runtime process and the coordinator CLI (ADR 0020,
section 3; ADR 0021, sections 3 and 4).

A debug build of ``harness-runtime`` stops at the step boundaries of ``state.write`` when
``HARNESS_RUNTIME_TEST_PAUSE_AT`` names one (``state.write:before-begin``, ``state.write:in-
transaction``), fails there with ``HARNESS_RUNTIME_TEST_FAIL_AT``, and drops the response of a
committed write at ``state.write:respond``. Each hook fires once for a runtime process, so the setup
of a test, which writes too, runs before the hook is armed.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness.json_types import JsonObject
from harness.orchestration.runtime_client import RuntimeClient, RuntimeClientError

from .runtime_store_support import SHORT_TMP, Store, store_call

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the seam tests drive a Unix-socket runtime"
)

BEFORE_BEGIN = "state.write:before-begin"
IN_TRANSACTION = "state.write:in-transaction"
RESPOND = "state.write:respond"
PAUSE = "HARNESS_RUNTIME_TEST_PAUSE_AT"
FAIL = "HARNESS_RUNTIME_TEST_FAIL_AT"
BUSY = "HARNESS_RUNTIME_TEST_BUSY_TIMEOUT_MS"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    store = Store(tmp_path)
    try:
        yield store
    finally:
        store.stop_runtime()


def _nowhere(store: Store) -> RuntimeClient:
    """A client whose runtime directory holds no runtime (short enough for a socket path)."""
    empty = tempfile.mkdtemp(prefix="hrn", dir=SHORT_TMP)
    return RuntimeClient(store.repo, environ={"XDG_RUNTIME_DIR": empty})


def _remedy(stderr: str) -> str:
    return next(line for line in stderr.splitlines() if line.startswith("REMEDY:"))


def _audit_rows(store: Store) -> int:
    connection = sqlite3.connect(store.database, timeout=10)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM audit").fetchone()[0])
    finally:
        connection.close()


def _operations(store: Store) -> list[tuple[str, str]]:
    connection = sqlite3.connect(store.database, timeout=10)
    try:
        return [
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT state, operation FROM operations ORDER BY created_at"
            )
        ]
    finally:
        connection.close()


def test_two_commands_that_read_the_same_revision_have_exactly_one_winner(store: Store) -> None:
    store.restart_runtime({PAUSE: BEFORE_BEGIN})
    before = store.revisions(store.status_path)[store.status_path]
    loser = store.start("dispatch", "heartbeat", "--dispatch", store.dispatch)
    store.runtime.wait_paused(BEFORE_BEGIN)

    winner = store.heartbeat()
    store.runtime.resume()
    stdout, stderr = loser.communicate(timeout=60)

    assert winner.returncode == 0, winner.stderr
    assert loser.returncode == 2, stdout
    assert _remedy(stderr).startswith("REMEDY: [revision_conflict] ")
    assert "dispatch-status" in stderr
    assert store.revisions(store.status_path)[store.status_path] == before + 1
    assert store.validate()["valid"] is True


def test_a_locked_store_is_a_bounded_store_busy_that_a_repeat_overcomes(store: Store) -> None:
    store.restart_runtime({PAUSE: IN_TRANSACTION, BUSY: "300"})
    before = store.revisions(store.status_path)[store.status_path]
    holder = store.start("dispatch", "heartbeat", "--dispatch", store.dispatch)
    store.runtime.wait_paused(IN_TRANSACTION)

    started = time.monotonic()
    busy = store.heartbeat()
    elapsed = time.monotonic() - started
    status = store.run("dispatch", "status")
    store.runtime.resume()
    holder.communicate(timeout=60)
    repeated = store.heartbeat()

    assert busy.returncode == 2
    assert _remedy(busy.stderr).startswith("REMEDY: [store_busy] ")
    assert elapsed < 20
    # The reader sees the state before the transaction in flight, not half of it.
    assert status.returncode == 0, status.stderr
    assert holder.returncode == 0
    assert repeated.returncode == 0, repeated.stderr
    assert store.revisions(store.status_path)[store.status_path] == before + 2


def test_the_three_failures_have_different_remedies_that_start_with_their_code(
    store: Store,
) -> None:
    store.restart_runtime({PAUSE: IN_TRANSACTION, BUSY: "300"})
    holder = store.start("dispatch", "heartbeat", "--dispatch", store.dispatch)
    store.runtime.wait_paused(IN_TRANSACTION)
    write: JsonObject = {
        "mutations": [
            {
                "op": "replace",
                "path": store.status_path,
                "content": "{}\n",
                "expected_revision": 999,
            }
        ]
    }
    with pytest.raises(RuntimeClientError) as busy:
        store_call(store.repo, store.runtime, "state.write", write)
    store.runtime.resume()
    holder.communicate(timeout=60)
    with pytest.raises(RuntimeClientError) as conflict:
        store_call(store.repo, store.runtime, "state.write", write)
    with pytest.raises(RuntimeClientError) as unavailable:
        _nowhere(store).call("status", operation_id="probe", timeout_seconds=0.3)

    remedies = [busy.value.remedy, conflict.value.remedy, unavailable.value.remedy]
    assert [error.value.code for error in (busy, conflict, unavailable)] == [
        "store_busy",
        "revision_conflict",
        "runtime_unavailable",
    ]
    assert len(set(remedies)) == 3
    for code, remedy in zip(("store_busy", "revision_conflict", "runtime_unavailable"), remedies):
        assert remedy.startswith(f"[{code}] ")


def test_a_lost_response_after_the_commit_is_answered_from_the_stored_result(store: Store) -> None:
    store.restart_runtime({FAIL: RESPOND})
    before = store.revisions(store.status_path)[store.status_path]
    audit = _audit_rows(store)

    result = store.heartbeat()

    assert result.returncode == 0, result.stderr
    assert store.revisions(store.status_path)[store.status_path] == before + 1
    assert _audit_rows(store) == audit + 1
    assert [state for state, _ in _operations(store)].count("committed") >= 1


def test_a_failure_inside_the_transaction_rolls_it_back_and_a_repeat_succeeds(
    store: Store,
) -> None:
    store.restart_runtime({FAIL: IN_TRANSACTION})
    before = store.revisions(store.status_path)[store.status_path]
    audit = _audit_rows(store)

    failed = store.heartbeat()

    assert failed.returncode == 2
    assert _remedy(failed.stderr).startswith("REMEDY: [internal] ")
    assert store.revisions(store.status_path)[store.status_path] == before
    assert _audit_rows(store) == audit
    repeated = store.heartbeat()
    assert repeated.returncode == 0, repeated.stderr
    assert store.revisions(store.status_path)[store.status_path] == before + 1


def test_a_runtime_killed_inside_the_transaction_leaves_nothing_and_the_client_commits_once(
    store: Store,
) -> None:
    store.restart_runtime({PAUSE: IN_TRANSACTION})
    before = store.revisions(store.status_path)[store.status_path]
    audit = _audit_rows(store)
    client = store.start("dispatch", "heartbeat", "--dispatch", store.dispatch)
    store.runtime.wait_paused(IN_TRANSACTION)

    store.restart_runtime()
    stdout, stderr = client.communicate(timeout=90)

    # The client repeated the same request until the new runtime answered it.
    assert client.returncode == 0, stderr
    assert json.loads(stdout)["dispatch_id"] == store.dispatch
    assert store.revisions(store.status_path)[store.status_path] == before + 1
    assert _audit_rows(store) == audit + 1
    assert store.validate()["valid"] is True


def test_the_runtime_that_is_not_running_is_runtime_unavailable_and_changes_nothing(
    store: Store,
) -> None:
    store.runtime.kill()
    audit = _audit_rows(store)

    with pytest.raises(RuntimeClientError) as unavailable:
        _nowhere(store).call(
            "state.write",
            operation_id="probe",
            payload={"mutations": []},
            timeout_seconds=0.3,
        )

    assert unavailable.value.code == "runtime_unavailable"
    assert _audit_rows(store) == audit
