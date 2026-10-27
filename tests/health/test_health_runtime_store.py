"""Health reads migrated orchestration through the real runtime IPC, without a file fallback."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from harness.health.checks import orchestration as checks
from harness.health.context import HealthContext
from harness.json_types import JsonObject
from harness.orchestration.core import state_view
from harness.orchestration.core.constants import DispatchState
from harness.orchestration.ledger import ledger_ops
from harness.orchestration.runtime_client import RuntimeClient, RuntimeClientError
from tests.orchestration.runtime_store_support import (
    BATCH_ID,
    DISPATCH_IDS,
    ServingRuntime,
    build_state,
    migrate,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the seam tests drive a Unix-socket runtime"
)


@pytest.fixture
def store_context(tmp_path: Path) -> HealthContext:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    build_state(tmp_path)
    migrated = migrate(tmp_path)
    assert migrated.returncode == 0, migrated.stderr
    return HealthContext(
        repo=tmp_path, lock={"capabilities": ["backend-orchestration"]}, online=False
    )


@pytest.fixture
def runtime(
    store_context: HealthContext, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    serving = ServingRuntime(store_context.repo)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(serving.xdg))
    try:
        yield
    finally:
        serving.stop()


def test_migrated_health_keeps_the_existing_batches(
    store_context: HealthContext, runtime: None
) -> None:
    summary = checks.check_ledger_summary(store_context)
    assert summary.status == "ok"
    assert "runtime-store" in summary.message
    assert "schema_version=4" in summary.message
    assert "active=1" in summary.message
    assert BATCH_ID in checks.check_unfinished_batches(store_context).message
    assert checks.check_blocked_batches(store_context).status == "ok"
    assert checks.check_stale_dispatches(store_context).status == "ok"


def test_stale_dispatch_status_is_read_before_the_ipc_view_closes(
    store_context: HealthContext, runtime: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ledger_ops._runtime_fetch

    def with_stale_worker(repo: Path) -> state_view.Fetch:
        fetch = original(repo)

        def read(
            collections: Sequence[str], paths: Sequence[str], revision: int | None
        ) -> tuple[dict[str, JsonObject], int | None, dict[str, int]]:
            records, snapshot, revisions = fetch(collections, paths, revision)
            key = f"dispatch-status/{DISPATCH_IDS[1]}.json"
            if key in records:
                records[key] = {
                    **records[key],
                    "state": DispatchState.WORKING,
                    "heartbeat_at": "2000-01-01T00:00:00+00:00",
                }
            return records, snapshot, revisions

        return read

    monkeypatch.setattr(ledger_ops, "_runtime_fetch", with_stale_worker)

    result = checks.check_stale_dispatches(store_context)

    assert result.status == "warn"
    assert DISPATCH_IDS[1] in result.message


def test_unavailable_runtime_is_reported_without_reading_the_file_ledger(
    store_context: HealthContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*args: object, **kwargs: object) -> None:
        raise RuntimeClientError(
            "runtime_unavailable",
            "harness-runtime does not answer",
            remedy="start the runtime",
            domain="infrastructure",
        )

    monkeypatch.setattr(RuntimeClient, "call", unavailable)

    result = checks.check_ledger_summary(store_context)

    assert result.status == "fail"
    assert "недоступен" in result.message
    assert "runtime" in result.message
    assert "file ledger is closed" not in result.message
