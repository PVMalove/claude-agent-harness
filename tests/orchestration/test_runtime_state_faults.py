"""Runtime state store under storage faults and platform limits of a durable write."""

from __future__ import annotations

import errno
import os
import subprocess
from pathlib import Path
from typing import BinaryIO

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


# --- Honest, sanitized storage failures --------------------------------------------------------

SECRET = "ghp_" + "s" * 30
LEAKY = f"device said token={SECRET} at /home/someone/private/disk"


def _leaky_error(code: int = errno.EIO) -> OSError:
    return OSError(code, LEAKY, "/home/someone/private/file")


def _assert_honest(
    error: store.RuntimeStateError, code: str, step: str, durable: int | None
) -> None:
    rendered = f"{error.message} {error.remedy} {error.diagnostics}"
    assert error.code == code and error.step == step
    assert error.progress_saved is False
    assert error.durable_revision == durable
    assert error.diagnostics["code"] == code and error.diagnostics["step"] == step
    assert error.diagnostics["errno"] in {"EIO", "EACCES", "ENOSPC", None}
    assert SECRET not in rendered and "/home/someone" not in rendered
    assert "saved" not in error.message.casefold()
    assert error.__cause__ is None and error.__suppress_context__


def _written(runtime: RuntimeStateStore) -> bytes:
    runtime.write(_record(runtime), expected_revision=0)
    return runtime.path(BATCH).read_bytes()


def _no_temporaries(runtime: RuntimeStateStore) -> bool:
    return list(runtime.location.directory.glob(".*.tmp")) == []


@pytest.mark.parametrize(
    ("target", "step"),
    [("open", "write-temp"), ("fsync", "fsync-file"), ("replace", "replace")],
)
def test_a_failure_before_the_replace_keeps_the_previous_revision(
    runtime: RuntimeStateStore,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    step: str,
) -> None:
    before = _written(runtime)
    real = getattr(os, target)

    def fail_on_record(*arguments: object, **options: object) -> object:
        # os.open also opens the ignore and lock files; fail only on the temporary record.
        if target == "open" and not str(arguments[0]).endswith(".tmp"):
            return real(*arguments, **options)
        raise _leaky_error(errno.ENOSPC)

    monkeypatch.setattr(os, target, fail_on_record)
    with pytest.raises(store.RuntimeStateError) as caught:
        runtime.write(_record(runtime, 2, "running"), expected_revision=1)
    monkeypatch.undo()

    _assert_honest(caught.value, "state-persist-failed", step, 1)
    assert caught.value.diagnostics["durability"] == "unchanged"
    assert "new progress was not persisted" in caught.value.message
    assert runtime.path(BATCH).read_bytes() == before
    assert _no_temporaries(runtime)


def test_a_failed_directory_fsync_does_not_claim_durability(
    runtime: RuntimeStateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _written(runtime)

    def fail(directory: Path) -> None:
        raise _leaky_error()

    monkeypatch.setattr(store, "WINDOWS", False)
    monkeypatch.setattr(store, "_fsync_directory", fail)
    with pytest.raises(store.RuntimeStateError) as caught:
        runtime.write(_record(runtime, 2, "running"), expected_revision=1)

    _assert_honest(caught.value, "state-persist-failed", "fsync-directory", 1)
    assert caught.value.diagnostics["durability"] == "unconfirmed"
    assert caught.value.diagnostics["path"] == "orchestration/runtime-state"
    assert "not confirmed durable" in caught.value.message
    assert "already holds revision 2" in caught.value.remedy
    assert "instead of retrying against revision 1" in caught.value.remedy

    # Following the remedy works; the generic retry against revision 1 would conflict.
    monkeypatch.undo()
    assert runtime.read(BATCH) == _record(runtime, 2, "running")
    with pytest.raises(store.RuntimeStateError, match="concurrency-conflict"):
        runtime.write(_record(runtime, 2, "running"), expected_revision=1)
    assert (
        runtime.write(_record(runtime, 3, "completed"), expected_revision=2).revision
        == 3
    )


def test_a_failure_before_the_replace_keeps_the_generic_retry_remedy(
    runtime: RuntimeStateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _written(runtime)

    def fail(fd: int) -> None:
        raise _leaky_error(errno.ENOSPC)

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(store.RuntimeStateError) as caught:
        runtime.write(_record(runtime, 2, "running"), expected_revision=1)

    assert caught.value.remedy == store._STEP_REMEDIES["state-persist-failed"]


def test_an_unreadable_record_is_storage_unavailable(
    runtime: RuntimeStateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _written(runtime)

    def deny(self: Path) -> bytes:
        raise _leaky_error(errno.EACCES)

    monkeypatch.setattr(Path, "read_bytes", deny)
    with pytest.raises(store.RuntimeStateError) as caught:
        runtime.read(BATCH)

    _assert_honest(caught.value, "storage-unavailable", "read", None)
    assert caught.value.diagnostics == {
        "code": "storage-unavailable",
        "step": "read",
        "errno": "EACCES",
        "path": f"orchestration/runtime-state/{BATCH}.json",
        "detail": "device said token=<redacted> at <path>",
        "durability": "unchanged",
    }


@pytest.mark.parametrize(("target", "step"), [("mkdir", "prepare"), ("lock", "lock")])
def test_a_store_that_cannot_be_prepared_or_locked_is_unavailable(
    runtime: RuntimeStateStore,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    step: str,
) -> None:
    def fail(*arguments: object, **options: object) -> None:
        raise _leaky_error()

    if target == "mkdir":
        monkeypatch.setattr(Path, "mkdir", fail)
    else:
        monkeypatch.setattr(store, "_guard_byte", fail)
    with pytest.raises(store.RuntimeStateError) as caught:
        runtime.write(_record(runtime), expected_revision=0)
    monkeypatch.undo()

    _assert_honest(caught.value, "storage-unavailable", step, None)
    assert not runtime.path(BATCH).exists()


def test_the_lock_is_held_until_the_record_is_replaced(
    runtime: RuntimeStateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int | None] = []
    replace = store._replace_atomically

    def probe(source: Path, target: Path) -> None:
        with (runtime.location.directory / f"{BATCH}.lock").open("r+b") as other:
            try:
                store_guard(other)
            except OSError as exc:
                seen.append(exc.errno)
        replace(source, target)

    monkeypatch.setattr(store, "_replace_atomically", probe)
    runtime.write(_record(runtime), expected_revision=0)

    assert len(seen) == 1 and seen[0] in store._LOCK_BUSY


def store_guard(handle: BinaryIO) -> None:
    store._guard_byte(handle, acquire=True)
    store._guard_byte(handle, acquire=False)


def test_a_failure_code_outside_the_registry_is_an_internal_error() -> None:
    with pytest.raises(ValueError, match="runtime failure code"):
        store.RuntimeStateError("disk-full", "write", "x", remedy="y")
