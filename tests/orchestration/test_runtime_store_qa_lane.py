"""The QA lane in the runtime-store epoch on seam 1: real holder processes, the coordinator CLI and
a real runtime (ADR 0020, section 7; ADR 0021, section 5).

A holder is a coordinator process that takes the QA lane through ``qa_lane.acquire`` in its store
unit, as ``qa run`` and ``integration local-qa`` do, holds it until the test writes a line to its
standard input and then releases it. The runtime records the identity of the holder process from
the kernel (``SO_PEERCRED``); a lease whose owner is provably dead is reaped by the next
admission without ``--force``, and a live owner is never released.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness.json_types import JsonObject

from .runtime_store_support import ROOT, STATE_REL, Store, store_call

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the seam tests drive a Unix-socket runtime and signals",
)

HOLDER = """
import json, sys
from pathlib import Path
from harness.errors import HarnessError, print_and_exit
from harness.orchestration import qa_lane, qa_lease
from harness.orchestration.core.constants import STATE_REL, QaOwnerKind
from harness.orchestration.ledger.ledger_ops import _ledger_lock
from harness.orchestration.ledger.lifecycle import LifecycleLedger
from harness.orchestration.workflow import qa_integration

repo, owner, seconds = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
beating = sys.argv[4] == "heartbeat"
ledger = LifecycleLedger(repo / STATE_REL)
try:
    qa_lane.reap_dead_owner(ledger, ledger.root, qa_integration._ops())
    with _ledger_lock(ledger):
        admission = qa_lane.acquire(
            ledger, owner, owner_kind=QaOwnerKind.LOCAL_QA, lease_seconds=seconds
        )
    print(json.dumps(admission), flush=True)
    if admission["state"] != "acquired":
        sys.exit(0)
    if beating:
        with qa_lease.heartbeat(ledger, admission["lease"], seconds) as beat:
            sys.stdin.readline()
        print(json.dumps({"lost": beat.lost, "beats": beat.beats}), flush=True)
    else:
        sys.stdin.readline()
    with _ledger_lock(ledger):
        qa_lane.release(ledger, admission["lease"])
    print("released", flush=True)
except HarnessError as exc:
    sys.exit(print_and_exit(exc))
"""


def _owner() -> str:
    return f"local-qa-{uuid.uuid4().hex}"


class Holder:
    """A coordinator process that holds the QA lane of ``store`` until it is told to release."""

    def __init__(
        self, store: Store, owner: str, seconds: int, *, heartbeat: bool = False
    ) -> None:
        self.owner = owner
        mode = "heartbeat" if heartbeat else "hold"
        self.process = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(store.repo), owner, str(seconds), mode],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONPATH": str(ROOT), **store.runtime.environ},
        )

    def admission(self) -> JsonObject:
        stdout = self.process.stdout
        assert stdout is not None
        line = stdout.readline()
        assert line, self.finish()[1]
        admission: JsonObject = json.loads(line)
        return admission

    def signal(self, number: int) -> None:
        os.kill(self.process.pid, number)

    def kill(self) -> None:
        self.process.kill()
        self.process.wait(timeout=30)

    def finish(self) -> tuple[str, str]:
        """Tell the holder to release, and wait for it: its standard output and error."""
        if self.process.poll() is None and self.process.stdin is not None:
            try:
                self.process.stdin.write("\n")
                self.process.stdin.flush()
            except BrokenPipeError:
                pass
        stdout, stderr = self.process.communicate(timeout=60)
        return stdout, stderr


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    store = Store(tmp_path)
    try:
        yield store
    finally:
        store.stop_runtime()


def _qa_status(store: Store) -> JsonObject:
    shown = store.run("qa", "status")
    assert shown.returncode == 0, shown.stderr
    status: JsonObject = json.loads(shown.stdout)
    return status


def _remedy(stderr: str) -> str:
    return next(line for line in stderr.splitlines() if line.startswith("REMEDY:"))


def _audit(store: Store, lease_op: str) -> list[JsonObject]:
    """The audit records of the store with ``lease_op``, each with its time as ``at``."""
    connection = sqlite3.connect(store.database, timeout=10)
    try:
        rows = connection.execute(
            "SELECT action, details_json, at FROM audit ORDER BY seq"
        ).fetchall()
    finally:
        connection.close()
    found: list[JsonObject] = []
    for action, details, at in rows:
        record = json.loads(details)
        if record.get("lease_op") == lease_op:
            found.append({**record, "action": action, "at": at})
    return found


def _wait_expired(admission: JsonObject) -> None:
    lease = admission["lease"]
    assert isinstance(lease, dict)
    from datetime import UTC, datetime

    expires = datetime.fromisoformat(str(lease["expires_at"]))
    while datetime.now(UTC) <= expires:
        time.sleep(0.1)


def test_a_holder_killed_by_sigkill_is_reaped_by_the_next_admission_without_force(
    store: Store,
) -> None:
    killed = Holder(store, _owner(), 1)
    held = killed.admission()
    assert held["state"] == "acquired"
    lease = held["lease"]
    assert isinstance(lease, dict)
    assert lease["pid"] == killed.process.pid
    killed.kill()
    _wait_expired(held)
    assert _qa_status(store)["stale_heartbeat"] is True

    following = Holder(store, _owner(), 60)
    admitted = following.admission()
    stdout, stderr = following.finish()

    assert admitted["state"] == "acquired", stderr
    assert following.process.returncode == 0, stderr
    assert "released" in stdout
    reaped = _audit(store, "reap")
    assert len(reaped) == 1
    evidence = reaped[0]
    assert evidence["action"] == "deletion"
    assert evidence["verdict"] == "owner-dead"
    assert evidence["owner"] == {"owner_kind": "local-qa", "owner_id": killed.owner}
    assert evidence["identity"]["pid"] == killed.process.pid
    assert evidence["observed"]["process"]["state"] == "absent"
    assert evidence["at"]
    assert _qa_status(store)["lease"] is None
    assert store.validate()["valid"] is True


def test_a_stopped_owner_keeps_its_expired_lease_and_the_next_admission_is_lease_held(
    store: Store,
) -> None:
    stopped = Holder(store, _owner(), 1)
    held = stopped.admission()
    assert held["state"] == "acquired"
    stopped.signal(signal.SIGSTOP)
    try:
        _wait_expired(held)

        refused = Holder(store, _owner(), 60)
        _, stderr = refused.finish()
        status = _qa_status(store)
        shown = store.run("dispatch", "status")
    finally:
        stopped.signal(signal.SIGCONT)
    stdout, released = stopped.finish()

    assert refused.process.returncode == 2, stderr
    remedy = _remedy(stderr)
    assert remedy.startswith(
        "REMEDY: [lease_held] stale_heartbeat=true verdict=owner-alive"
    )
    assert status["stale_heartbeat"] is True
    assert status["lease"]["owner_id"] == stopped.owner
    assert shown.returncode == 0, shown.stderr
    lane = json.loads(shown.stdout)["qa_lane"]
    assert lane["owner_id"] == stopped.owner
    assert lane["stale_heartbeat"] is True
    assert _audit(store, "reap") == []
    assert stopped.process.returncode == 0, released
    assert "released" in stdout


def test_a_lease_cleared_by_an_operator_makes_the_old_owner_release_a_fencing_mismatch(
    store: Store,
) -> None:
    owner = Holder(store, _owner(), 1)
    held = owner.admission()
    _wait_expired(held)
    lease = held["lease"]
    assert isinstance(lease, dict)

    cleared = store.run(
        "qa",
        "clear-stale-lease",
        "--approved-by",
        "Malove",
        "--approved-at",
        "2026-10-28T00:00:00+00:00",
        "--expected-host",
        str(lease["host"]),
        "--expected-pid",
        str(lease["pid"]),
        "--expected-expiry",
        str(lease["expires_at"]),
        "--reason",
        "the worker sandbox stopped",
    )
    _, stderr = owner.finish()

    assert cleared.returncode == 0, cleared.stderr
    assert owner.process.returncode == 2
    assert _remedy(stderr).startswith("REMEDY: [fencing_mismatch] ")
    clear = _audit(store, "clear")
    assert len(clear) == 1
    assert clear[0]["decision"]["approved_by"] == "Malove"
    assert (store.repo / STATE_REL / "runtime.db").is_file()


def test_a_heartbeating_owner_never_shows_a_stale_heartbeat_past_its_lifetime(
    store: Store,
) -> None:
    owner = Holder(store, _owner(), 2, heartbeat=True)
    held = owner.admission()
    assert held["state"] == "acquired"
    time.sleep(3.5)

    status = _qa_status(store)
    stdout, stderr = owner.finish()

    assert status["stale_heartbeat"] is False
    assert status["lease"]["owner_id"] == owner.owner
    assert owner.process.returncode == 0, stderr
    beat = json.loads(stdout.splitlines()[0])
    assert beat["lost"] is False
    assert beat["beats"] >= 2
    assert _audit(store, "heartbeat")


def test_a_lease_cleared_under_a_heartbeat_stops_it_and_marks_it_lost(
    store: Store,
) -> None:
    owner = Holder(store, _owner(), 3, heartbeat=True)
    held = owner.admission()
    lease = held["lease"]
    assert isinstance(lease, dict)
    store_call(
        store.repo,
        store.runtime,
        "state.write",
        {
            "mutations": [
                {
                    "op": "lease_clear",
                    "path": "qa-lane/lease.json",
                    "reason": "the operator took the lane back",
                    "lease": {
                        "owner_kind": "local-qa",
                        "owner_id": owner.owner,
                        "fencing_token": lease["fencing_token"],
                    },
                    "decision": {
                        "approved_by": "Malove",
                        "approved_at": "2026-10-28T00:00:00+00:00",
                    },
                }
            ]
        },
    )
    time.sleep(2.5)

    stdout, stderr = owner.finish()

    beat = json.loads(stdout.splitlines()[0])
    assert beat["lost"] is True
    assert owner.process.returncode == 2
    assert _remedy(stderr).startswith("REMEDY: [fencing_mismatch] ")


def _lock_status(store: Store) -> JsonObject:
    shown = store.run("ledger", "lock-status")
    assert shown.returncode == 0, shown.stderr
    status: JsonObject = json.loads(shown.stdout)
    return status


def test_a_live_owner_is_released_neither_automatically_nor_by_release_lock_without_force(
    store: Store,
) -> None:
    stopped = Holder(store, _owner(), 1)
    held = stopped.admission()
    stopped.signal(signal.SIGSTOP)
    try:
        _wait_expired(held)
        status = _lock_status(store)
        refused = store.run("ledger", "release-lock")
        still = _qa_status(store)
        forced = store.run(
            "ledger",
            "release-lock",
            "--force",
            "--approved-by",
            "Malove",
            "--approved-at",
            "2026-10-28T00:00:00+00:00",
            "--reason",
            "the owner sandbox is stopped for good",
        )
    finally:
        stopped.signal(signal.SIGCONT)
    _, stderr = stopped.finish()

    assert status["locked"] is False
    lease = status["qa_lease"]
    assert lease["verdict"] == "owner-alive"
    assert lease["stale_heartbeat"] is True
    assert lease["release_available"] is False
    assert lease["owner"] == {"owner_kind": "local-qa", "owner_id": stopped.owner}
    assert refused.returncode == 2
    assert _remedy(refused.stderr).startswith(
        "REMEDY: [lease_held] stale_heartbeat=true verdict=owner-alive"
    )
    assert still["lease"]["owner_id"] == stopped.owner
    assert forced.returncode == 0, forced.stderr
    assert json.loads(forced.stdout)["reason"] == "forced"
    assert _audit(store, "clear")[0]["verdict"] == "owner-alive"
    assert _audit(store, "reap") == []
    # The old owner's release meets the operator's decision.
    assert _remedy(stderr).startswith("REMEDY: [fencing_mismatch] ")


def test_lock_status_shows_owner_dead_for_a_killed_owner_and_release_lock_reaps_it(
    store: Store,
) -> None:
    killed = Holder(store, _owner(), 1)
    held = killed.admission()
    killed.kill()
    _wait_expired(held)

    status = _lock_status(store)
    released = store.run("ledger", "release-lock")

    lease = status["qa_lease"]
    assert lease["verdict"] == "owner-dead"
    assert lease["release_available"] is True
    assert lease["identity"]["pid"] == killed.process.pid
    assert released.returncode == 0, released.stderr
    assert json.loads(released.stdout)["reason"] == "owner-dead"
    assert _audit(store, "reap")[0]["verdict"] == "owner-dead"
    assert _lock_status(store)["qa_lease"] is None
