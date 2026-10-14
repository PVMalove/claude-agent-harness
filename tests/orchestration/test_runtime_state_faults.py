"""Runtime state store under storage faults and platform limits of a durable write."""

from __future__ import annotations

import errno
import os
import subprocess
from pathlib import Path

import pytest

from harness.json_types import JsonObject
from harness.orchestration import runtime_state_store as store
from harness.orchestration.core.runtime_state_contract import RUNTIME_STATE_CONTRACT
from harness.orchestration.runtime_state_store import RuntimeStateStore

BATCH = "batch-0123abcd"


@pytest.fixture
def runtime(tmp_path: Path) -> RuntimeStateStore:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return RuntimeStateStore(tmp_path)


def _record(
    runtime: RuntimeStateStore, revision: int = 1, state: str = "ready"
) -> JsonObject:
    return {
        "contract": RUNTIME_STATE_CONTRACT,
        "identity": runtime.location.identity(BATCH),
        "revision": revision,
        "updated_at": "2026-10-14T09:00:00+00:00",
        "state": state,
        "stage": None,
        "dispatch_id": None,
        "operation": None,
        "ledger": {"generation": None, "record_hashes": {}},
        "policy_snapshot_sha256": None,
        "input_refs": [],
        "output_refs": [],
        "last_confirmed_boundary": None,
        "failure": None,
    }


# --- Platform limits of the durable write -----------------------------------------------------


def test_the_record_and_its_directory_are_fsynced(
    runtime: RuntimeStateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced_files: list[int] = []
    synced_directories: list[Path] = []
    real_fsync = os.fsync

    def spy_fsync(fd: int) -> None:
        synced_files.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(store, "WINDOWS", False)
    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(store, "_fsync_directory", synced_directories.append)

    result = runtime.write(_record(runtime), expected_revision=0)

    assert result == store.WriteResult(1, "done")
    assert len(synced_files) == 1
    assert synced_directories == [runtime.location.directory]


def test_windows_reports_the_directory_fsync_as_unsupported(
    runtime: RuntimeStateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[Path] = []
    monkeypatch.setattr(store, "WINDOWS", True)
    monkeypatch.setattr(store, "_fsync_directory", called.append)

    result = runtime.write(_record(runtime), expected_revision=0)

    assert result.directory_fsync == "unsupported"
    assert called == []
    assert runtime.read(BATCH) == _record(runtime)
    assert "windows" in store.DURABILITY_LIMITS


@pytest.mark.parametrize("code", [errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP])
def test_a_filesystem_without_directory_fsync_is_reported_not_hidden(
    runtime: RuntimeStateStore, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    def refuse(directory: Path) -> None:
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(store, "WINDOWS", False)
    monkeypatch.setattr(store, "_fsync_directory", refuse)

    result = runtime.write(_record(runtime), expected_revision=0)

    assert result.directory_fsync == "unsupported"
    assert runtime.read(BATCH) == _record(runtime)


@pytest.mark.skipif(os.name == "nt", reason="directory fsync is a POSIX facility")
def test_the_real_directory_fsync_runs_on_posix(tmp_path: Path) -> None:
    store._fsync_directory(tmp_path)
    assert store._directory_durability(tmp_path) in {"done", "unsupported"}
