"""Shared helpers of the runtime-store seam tests: a 1.x state written by the real Python ledger,
and the real ``harness-runtime`` binary."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
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
