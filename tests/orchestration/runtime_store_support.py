"""Shared helpers of the runtime-store seam tests: a 1.x state written by the real Python ledger,
and the real ``harness-runtime`` binary."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from harness.orchestration.core.constants import BatchState, DispatchState
from harness.orchestration.ledger.lifecycle import (
    BatchRecord,
    CheckpointRecord,
    DispatchRecord,
    DispatchStatusRecord,
    JsonObject,
    JsonValue,
    LifecycleLedger,
    PlanRecord,
    _canonical,
)

ROOT = Path(__file__).resolve().parents[2]
STATE_REL = Path(".harness/orchestration/state")
BATCH_ID = "batch-aaaaaaaa-0000-4000-8000-000000000001"
DISPATCH_IDS = (
    "dispatch-11111111-0000-4000-8000-000000000001",
    "dispatch-22222222-0000-4000-8000-000000000002",
)


def runtime_binary() -> Path:
    configured = os.environ.get("HARNESS_RUNTIME_BIN")
    name = "harness-runtime.exe" if sys.platform == "win32" else "harness-runtime"
    binary = (
        Path(configured)
        if configured
        else ROOT / "native/harness-runtime/target/debug" / name
    )
    if binary.is_file():
        return binary
    message = (
        f"build harness-runtime first (cargo build in native/harness-runtime): {binary}"
    )
    if os.environ.get("HARNESS_REQUIRE_RUNTIME") == "1":
        pytest.fail(message)
    pytest.skip(message)


def _digest(value: JsonObject) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def build_state(
    repo: Path,
    *,
    second_status: DispatchState = DispatchState.APPROVED,
    owes_reassessment_without_candidate: bool = False,
) -> LifecycleLedger:
    """A repository whose state directory a 1.x installation wrote: one batch of two dispatches,
    the first reported."""
    ledger = LifecycleLedger(repo / STATE_REL)
    ledger.ensure()
    generation = ledger.records_root()
    first, second = DISPATCH_IDS
    dispatches = [
        DispatchRecord(
            dispatch_id,
            BATCH_ID,
            DispatchState.APPROVED,
            {"approved_at": "2026-10-01T00:00:00+00:00"},
            {"role": role, "note": "café \U0001f600", "ratio": 1e-05},
        )
        for dispatch_id, role in ((first, "developer"), (second, "qa"))
    ]
    report: JsonObject = {"dispatch_id": first, "outcome": "completed", "output": "done"}
    ledger.write_immutable(generation / "reports" / f"{first}.json", report, artifact=True)
    entries: list[JsonValue] = [
        {
            "dispatch_id": first,
            "state": "reported",
            "brief_sha256": _digest(dispatches[0].to_dict()),
            "report": f"reports/{first}.json",
            "report_sha256": _digest(report),
        },
        {
            "dispatch_id": second,
            "state": "approved",
            "brief_sha256": _digest(dispatches[1].to_dict()),
        },
    ]
    batch_fields: JsonObject = {"ticket": "#1", "write_paths": ["a/**"]}
    reassessment: JsonObject = (
        {"risk_reassessment_required": True}
        if owes_reassessment_without_candidate
        else {
            "risk_reassessment_required": True,
            "risk_reassessment_candidate": "a" * 40,
            "risk_reassessment_triggers": ["transactions"],
        }
    )
    ledger.write_record(PlanRecord(BATCH_ID, dict(batch_fields)))
    ledger.write_record(
        BatchRecord(
            BATCH_ID, BatchState.ACTIVE, entries, None, {**batch_fields, **reassessment}
        )
    )
    for record in dispatches:
        ledger.write_record(record)
    ledger.write_record(DispatchStatusRecord(first, DispatchState.REPORTED, {}))
    ledger.write_record(DispatchStatusRecord(second, second_status, {}))
    ledger.write_record(CheckpointRecord("checkpoint-0001", {"dispatch_id": first}))
    return ledger


def migrate(repo: Path, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """``harness-runtime migrate --repo <repo>`` as its own process."""
    return subprocess.run(
        [str(runtime_binary()), "migrate", "--repo", str(repo)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


def tree(root: Path, *, skip: tuple[str, ...] = ()) -> dict[str, bytes]:
    """Every file below ``root`` and its bytes, keyed by ``/`` path."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.relative_to(root).as_posix().startswith(skip)
    }


def coordinator(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """The 1.x coordinator CLI against ``repo``."""
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "harness/orchestration/coordinator.py"),
            "--repo",
            str(repo),
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )


def load(path: Path) -> JsonObject:
    loaded: JsonObject = json.loads(path.read_text(encoding="utf-8"))
    return loaded


class PausedMigrate:
    """A debug-build ``harness-runtime migrate`` that waits at one step boundary.

    ``HARNESS_RUNTIME_TEST_PAUSE_AT`` makes the process print ``PAUSED:<step>`` and wait for a line
    on stdin; a test kills it there, or changes the state first and then resumes it."""

    def __init__(self, repo: Path, step: str, *, env: dict[str, str] | None = None) -> None:
        self.step = step
        self.process = subprocess.Popen(
            [str(runtime_binary()), "migrate", "--repo", str(repo)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "HARNESS_RUNTIME_TEST_PAUSE_AT": step, **(env or {})},
        )
        self._lines: list[str] = []
        self._paused = threading.Event()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        stderr = self.process.stderr
        assert stderr is not None
        for line in stderr:
            self._lines.append(line)
            if line.strip() == f"PAUSED:{self.step}":
                self._paused.set()

    def wait_paused(self, timeout: float = 60.0) -> None:
        if not self._paused.wait(timeout):
            self.process.kill()
            pytest.fail(
                f"migrate did not pause at {self.step!r}: {''.join(self._lines)!r}"
            )

    def kill(self) -> None:
        """SIGKILL: the process cannot clean up anything."""
        self.process.kill()
        self.process.wait(timeout=30)
        self._reader.join(timeout=30)

    def resume(self) -> tuple[int, str, str]:
        """Let the process go on and return its exit code, stdout and stderr."""
        stdin = self.process.stdin
        assert stdin is not None
        stdin.write("\n")
        stdin.flush()
        stdin.close()
        stdout = self.process.stdout
        assert stdout is not None
        text = stdout.read()
        code = self.process.wait(timeout=60)
        self._reader.join(timeout=30)
        return code, text, "".join(self._lines)


def authorities(repo: Path) -> dict[str, bool]:
    """Which store is the authority of the state directory: the pointer decides."""
    state = repo / STATE_REL
    pointer = load(state / "ledger.json")
    marker = state / "AUTHORITY.json"
    return {
        "file-ledger": pointer["version"] == 3,
        "runtime-store": pointer["version"] == 4
        and marker.is_file()
        and load(marker)["authority"] == "runtime-store"
        and (state / "runtime.db").is_file(),
    }
