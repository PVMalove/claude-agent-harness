"""The cost of ``dispatch status`` and ``dispatch heartbeat`` before and after the cutover, on a ledger
the size of this repository's (ADR 0020; criterion carried from #778).

The test is optional: it runs only when ``HARNESS_MEASURE_STATE_DIR`` names a copy of a state
directory that was taken while no dispatch was live. The directory is read, never changed: it is
copied twice into the temporary directory of the test. In the first copy the file ledger answers; the
second is cut over with ``harness-runtime migrate`` and served by a real runtime. Each command runs
``HARNESS_MEASURE_RUNS`` times (30 by default; ``HARNESS_MEASURE_FILE_RUNS`` sets the runs of the
file ledger, whose commands read the whole history and take minutes) as the CLI process that a user
starts, and the wall clock of each run gives the percentiles. The result is printed as one JSON
document, and written to the file named by ``HARNESS_MEASURE_OUT`` when it is set.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

import pytest

from .runtime_store_support import (
    STATE_REL,
    ServingRuntime,
    coordinator_with,
    migrate,
    write_coordinator_state,
)

STATE_DIR = os.environ.get("HARNESS_MEASURE_STATE_DIR")
RUNS = int(os.environ.get("HARNESS_MEASURE_RUNS", "30"))
# A command of the file ledger reads the whole history: minutes on a ledger of this size, so its
# runs may be fewer than those of the runtime store.
FILE_RUNS = int(os.environ.get("HARNESS_MEASURE_FILE_RUNS", str(RUNS)))

pytestmark = [
    pytest.mark.skipif(not STATE_DIR, reason="set HARNESS_MEASURE_STATE_DIR to measure"),
    pytest.mark.skipif(sys.platform == "win32", reason="the seam tests drive a Unix-socket runtime"),
]


def _percentile(sorted_samples: list[float], percent: float) -> float:
    """The nearest-rank percentile of samples that are sorted."""
    rank = max(1, math.ceil(percent / 100 * len(sorted_samples)))
    return sorted_samples[rank - 1]


def _summary(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "p50": round(_percentile(ordered, 50), 3),
        "p95": round(_percentile(ordered, 95), 3),
        "p99": round(_percentile(ordered, 99), 3),
        "min": round(ordered[0], 3),
        "max": round(ordered[-1], 3),
    }


def _working_dispatch(repo: Path, runtime: ServingRuntime | None, dispatch: str) -> None:
    """Send the approved dispatch and let its worker report itself: the dispatch is working."""
    sent = coordinator_with(repo, runtime, "dispatch", "send", "--dispatch", dispatch)
    assert sent.returncode == 0, sent.stderr
    reported = coordinator_with(
        repo,
        runtime,
        "dispatch",
        "self-report",
        "--dispatch",
        dispatch,
        "--model",
        "sonnet",
        "--worktree",
        json.loads(sent.stdout)["worker_worktree"],
    )
    assert reported.returncode == 0, reported.stderr


def _measure(
    repo: Path, runtime: ServingRuntime | None, dispatch: str, runs: int
) -> dict[str, dict[str, float]]:
    """The wall clock of each command, ``runs`` times. A runtime-store command runs once before
    the measured runs, so the first request of the runtime does not count; the file ledger has no
    process to warm up, and each of its runs reads the whole history anyway."""
    commands = {
        "dispatch status": ["dispatch", "status"],
        "dispatch heartbeat": ["dispatch", "heartbeat", "--dispatch", dispatch],
    }
    found: dict[str, dict[str, float]] = {}
    for name, arguments in commands.items():
        if runtime is not None:
            warm = coordinator_with(repo, runtime, *arguments)
            assert warm.returncode == 0, warm.stderr
        samples = []
        for _ in range(runs):
            started = time.perf_counter()
            result = coordinator_with(repo, runtime, *arguments)
            samples.append(time.perf_counter() - started)
            assert result.returncode == 0, result.stderr
        found[name] = {**_summary(samples), "runs": float(runs)}
    return found


def _size(state: Path) -> dict[str, int]:
    """What the ledger holds: the records of the selected generation and the bytes of the state."""
    pointer = json.loads((state / "ledger.json").read_text(encoding="utf-8"))
    generation = state / "generations" / pointer["generation"]

    def count(directory: str) -> int:
        return sum(1 for _ in (generation / directory).glob("*.json"))

    return {
        "batches": count("batches"),
        "dispatches": count("dispatches"),
        "audit_files": count("audit"),
        "reports": count("reports"),
        "bytes": sum(path.stat().st_size for path in state.rglob("*") if path.is_file()),
    }


def test_dispatch_status_and_heartbeat_before_and_after_the_cutover(tmp_path: Path) -> None:
    assert STATE_DIR is not None
    source = Path(STATE_DIR)
    (tmp_path / "file").mkdir()
    (tmp_path / "store").mkdir()
    file_repo, file_ids = write_coordinator_state(
        tmp_path / "file", report=False, ticket="#99901", state_from=source
    )
    store_repo, store_ids = write_coordinator_state(
        tmp_path / "store", report=False, ticket="#99902", state_from=source
    )
    size = _size(file_repo / STATE_REL)
    result: dict[str, object] = {
        "runs": {"file_ledger": FILE_RUNS, "runtime_store": RUNS},
        "ledger": size,
    }

    started = time.perf_counter()
    cut_over = migrate(store_repo)
    result["migrate_seconds"] = round(time.perf_counter() - started, 1)
    assert cut_over.returncode == 0, cut_over.stderr
    runtime = ServingRuntime(store_repo)
    try:
        _working_dispatch(store_repo, runtime, store_ids["dispatch_id"])
        after = _measure(store_repo, runtime, store_ids["dispatch_id"], RUNS)
    finally:
        runtime.stop()
    result["seconds"] = {"runtime_store": after}
    _write(result)

    _working_dispatch(file_repo, None, file_ids["dispatch_id"])
    before = _measure(file_repo, None, file_ids["dispatch_id"], FILE_RUNS)
    result["seconds"] = {"file_ledger": before, "runtime_store": after}
    _write(result)
    print(json.dumps(result, indent=2, sort_keys=True))


def _write(document: dict[str, object]) -> None:
    """The result so far, in the file named by ``HARNESS_MEASURE_OUT``: a run of the file ledger
    takes minutes, so what is measured is kept as soon as it is known."""
    destination = os.environ.get("HARNESS_MEASURE_OUT")
    if destination:
        Path(destination).write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
