"""Operational diagnostics and the independent emergency record of a command that stopped before
it could reach its batch: invalid config, a busy or unreadable ledger, and bootstrap failures."""

from __future__ import annotations

import copy
import errno
import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from harness.json_types import JsonObject
from harness.orchestration import coordinator, runtime_emergency, runtime_state_store
from harness.orchestration.ledger.lifecycle import LifecycleLedger
from harness.orchestration.runtime_state_store import (
    RuntimeStateError,
    RuntimeStateStore,
    emergency_problems,
)
from tests.orchestration import test_recovery
from tests.orchestration.test_coordinator import _git


def approved(fixture: test_recovery.RecoveryTests) -> JsonObject:
    brief: JsonObject = fixture._dispatch(
        fixture._create_batch()["batch_id"], "architect"
    )["brief"]
    return brief


def run(
    fixture: test_recovery.RecoveryTests,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *argv: str,
) -> tuple[int, str, JsonObject | None]:
    """Run the CLI on the fixture's repository; the exit code, stderr and the diagnostics."""
    common = ["--repo", str(fixture.repo), "--state-dir", str(fixture.state_dir)]
    monkeypatch.setattr(sys, "argv", ["coordinator.py", *argv[:2], *common, *argv[2:]])
    monkeypatch.setattr(coordinator, "pinned_runtime_command", lambda args: None)
    capsys.readouterr()
    code = coordinator.main()
    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if line.startswith("DIAGNOSTICS: ")]
    assert len(lines) <= 1
    diagnostics = json.loads(lines[0].removeprefix("DIAGNOSTICS: ")) if lines else None
    return code, err, diagnostics


def store(fixture: test_recovery.RecoveryTests) -> RuntimeStateStore:
    return RuntimeStateStore(fixture.repo, fixture.state_dir)


def files(directory: Path, *, skip: tuple[str, ...] = ()) -> dict[str, bytes]:
    if not directory.is_dir():
        return {}
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and not path.name.startswith(skip)
    }


def batch_state(fixture: test_recovery.RecoveryTests) -> dict[str, bytes]:
    """The ledger and every runtime state record but the emergency record and the locks."""
    return {
        **files(fixture.state_dir),
        **files(store(fixture).location.directory, skip=("emergency", ".gitignore")),
    }


def invalidate_config(fixture: test_recovery.RecoveryTests) -> None:
    (fixture.repo / ".harness/orchestration.json").write_text(
        '{"concurrency_budget": "many"', encoding="utf-8"
    )


def hold_ledger(fixture: test_recovery.RecoveryTests) -> Path:
    owner = fixture.state_dir / ".coordinator.lock" / "owner.json"
    owner.parent.mkdir(parents=True)
    owner.write_text('{"pid": 1, "host": "elsewhere", "acquired_at": "x"}', "utf-8")
    return owner


def test_an_invalid_config_is_recorded_and_the_ledger_stays_unchanged(
    recovery: test_recovery.RecoveryTests,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brief = approved(recovery)
    invalidate_config(recovery)
    before = batch_state(recovery)

    code, err, diagnostics = run(
        recovery,
        monkeypatch,
        capsys,
        "dispatch",
        "send",
        "--dispatch",
        brief["dispatch_id"],
    )

    assert code == 2
    assert err.index("DIAGNOSTICS: ") < err.index("ERROR: ") < err.index("REMEDY: ")
    assert diagnostics is not None
    assert (diagnostics["code"], diagnostics["step"], diagnostics["command"]) == (
        "config-invalid",
        "config",
        "dispatch send",
    )
    runtime = store(recovery)
    assert diagnostics["emergency_record"] == {
        "written": True,
        "path": runtime.location.relative(runtime.emergency_path),
        "sequence": 1,
    }
    record = store(recovery).read_emergency()
    assert record is not None
    assert (record["state"], record["command"], record["failure"]["code"]) == (
        "suspended",
        "dispatch send",
        "config-invalid",
    )
    assert record["subject"] == {"batch_id": None, "dispatch_id": brief["dispatch_id"]}
    assert batch_state(recovery) == before


def test_a_busy_ledger_is_recorded_without_touching_its_lock(
    recovery: test_recovery.RecoveryTests,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brief = approved(recovery)
    owner = hold_ledger(recovery)
    held = owner.read_bytes()
    before = batch_state(recovery)

    code, _, diagnostics = run(
        recovery,
        monkeypatch,
        capsys,
        "dispatch",
        "heartbeat",
        "--dispatch",
        brief["dispatch_id"],
    )

    assert code == 2
    assert diagnostics is not None
    assert (diagnostics["code"], diagnostics["step"]) == (
        "concurrency-conflict",
        "ledger-lock",
    )
    assert diagnostics["emergency_record"]["written"] is True
    assert owner.read_bytes() == held
    assert batch_state(recovery) == before
    # The next failure overwrites the record with the next sequence.
    run(
        recovery,
        monkeypatch,
        capsys,
        "dispatch",
        "heartbeat",
        "--dispatch",
        brief["dispatch_id"],
    )
    record = store(recovery).read_emergency()
    assert record is not None and record["sequence"] == 2


def test_an_unreadable_ledger_invents_no_state(
    recovery: test_recovery.RecoveryTests,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brief = approved(recovery)
    (recovery.state_dir / "ledger.json").write_text("{", encoding="utf-8")
    before = batch_state(recovery)

    code, _, diagnostics = run(
        recovery,
        monkeypatch,
        capsys,
        "dispatch",
        "self-report",
        "--dispatch",
        brief["dispatch_id"],
        "--model",
        "sonnet",
    )

    assert code == 2
    assert diagnostics is not None
    assert (diagnostics["code"], diagnostics["step"]) == (
        "integrity-failure",
        "ledger-read",
    )
    assert diagnostics["emergency_record"]["written"] is True
    assert batch_state(recovery) == before


def test_a_corrupt_record_is_an_unreadable_ledger_and_an_unknown_id_is_not(
    recovery: test_recovery.RecoveryTests,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brief = approved(recovery)
    unknown = brief["dispatch_id"][:-4] + "0000"
    code, err, diagnostics = run(
        recovery, monkeypatch, capsys, "dispatch", "heartbeat", "--dispatch", unknown
    )
    assert (code, diagnostics) == (2, None)
    assert "ERROR: " in err
    records = LifecycleLedger(recovery.state_dir).records_root()
    (records / "batches" / f"{brief['batch_id']}.json").write_text("{", "utf-8")
    before = batch_state(recovery)

    code, _, diagnostics = run(
        recovery,
        monkeypatch,
        capsys,
        "dispatch",
        "heartbeat",
        "--dispatch",
        brief["dispatch_id"],
    )

    assert code == 2
    assert diagnostics is not None
    assert (diagnostics["code"], diagnostics["step"]) == (
        "integrity-failure",
        "ledger-read",
    )
    assert diagnostics["emergency_record"]["written"] is True
    assert batch_state(recovery) == before


def corrupt_ledger(fixture: test_recovery.RecoveryTests) -> None:
    (fixture.state_dir / "ledger.json").write_text("{", encoding="utf-8")


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        (invalidate_config, "config-invalid"),
        (hold_ledger, "concurrency-conflict"),
        (corrupt_ledger, "integrity-failure"),
    ],
)
def test_a_failed_bootstrap_prints_diagnostics_and_records_nothing(
    recovery: test_recovery.RecoveryTests,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fault: Callable[[test_recovery.RecoveryTests], object],
    code: str,
) -> None:
    LifecycleLedger(recovery.state_dir).ensure()
    _git(
        recovery.repo,
        "worktree",
        "add",
        "-b",
        recovery.branch,
        str(recovery.worktree),
        "master",
    )
    records = LifecycleLedger(recovery.state_dir).records_root()
    fault(recovery)
    pointer = (recovery.state_dir / "ledger.json").read_bytes()
    before = (files(records), pointer, files(store(recovery).location.base))
    argv = [
        "batch",
        "create",
        "--ticket",
        "#662",
        "--branch",
        recovery.branch,
        "--worktree",
        str(recovery.worktree),
        "--allowed-path",
        "**",
        "--integration-ref",
        "master",
        "--definition-of-done",
        "route retries by cause",
        "--prohibited-change",
        "secrets",
        "--expected-file",
        "services/x.py",
        "--expected-service",
        "core",
        "--expected-changed-lines",
        "10",
    ]

    exit_code, _, diagnostics = run(recovery, monkeypatch, capsys, *argv)

    assert exit_code == 2
    assert diagnostics is not None
    assert (diagnostics["code"], diagnostics["command"]) == (code, "batch create")
    assert diagnostics["subject"] == {"batch_id": None, "dispatch_id": None}
    assert diagnostics["emergency_record"] is None
    pointer = (recovery.state_dir / "ledger.json").read_bytes()
    assert (files(records), pointer, files(store(recovery).location.base)) == before


def test_a_held_emergency_lock_leaves_the_record_unwritten(
    recovery: test_recovery.RecoveryTests,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brief = approved(recovery)
    invalidate_config(recovery)
    runtime = store(recovery)
    runtime.location.directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(runtime_state_store, "LOCK_TIMEOUT_SECONDS", 0.1)
    with (runtime.location.directory / "emergency.lock").open("a+b") as holder:
        runtime_state_store._guard_byte(holder, acquire=True)
        code, err, diagnostics = run(
            recovery,
            monkeypatch,
            capsys,
            "dispatch",
            "send",
            "--dispatch",
            brief["dispatch_id"],
        )
        runtime_state_store._guard_byte(holder, acquire=False)

    assert code == 2
    assert "ERROR: " in err
    assert diagnostics is not None
    assert diagnostics["code"] == "config-invalid"
    assert diagnostics["emergency_record"] == {
        "written": False,
        "code": "concurrency-conflict",
    }
    assert runtime.read_emergency() is None


def test_a_runtime_state_failure_prints_its_own_diagnostics(
    recovery: test_recovery.RecoveryTests,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brief = approved(recovery)
    original = runtime_state_store._replace_atomically

    def full_disk(source: Path, target: Path) -> None:
        if "intents" in target.parts:
            raise OSError(errno.ENOSPC, "No space left on device")
        original(source, target)

    monkeypatch.setattr(runtime_state_store, "_replace_atomically", full_disk)

    code, _, diagnostics = run(
        recovery,
        monkeypatch,
        capsys,
        "dispatch",
        "send",
        "--dispatch",
        brief["dispatch_id"],
    )

    assert code == 2
    assert diagnostics is not None
    assert (diagnostics["code"], diagnostics["errno"]) == (
        "state-persist-failed",
        "ENOSPC",
    )
    assert diagnostics["emergency_record"] is None
    assert store(recovery).read_emergency() is None


def test_an_emergency_record_authorizes_nothing(
    recovery: test_recovery.RecoveryTests,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brief = approved(recovery)
    path = recovery.repo / ".harness/orchestration.json"
    config = path.read_bytes() if path.exists() else None
    invalidate_config(recovery)
    run(
        recovery,
        monkeypatch,
        capsys,
        "dispatch",
        "send",
        "--dispatch",
        brief["dispatch_id"],
    )
    record = store(recovery).read_emergency()
    assert record is not None
    forged = copy.deepcopy(record)
    forged["approved_by"] = "operator"

    assert emergency_problems(record) == []
    assert emergency_problems(forged)
    if config is None:
        path.unlink()
    else:
        path.write_bytes(config)
    code, _, diagnostics = run(
        recovery,
        monkeypatch,
        capsys,
        "dispatch",
        "send",
        "--dispatch",
        brief["dispatch_id"],
    )

    assert (code, diagnostics) == (0, None)
    assert store(recovery).read_emergency() == record
    store(recovery).emergency_path.write_text(json.dumps(forged), encoding="utf-8")
    with pytest.raises(RuntimeStateError) as caught:
        store(recovery).read_emergency()
    assert caught.value.code == "integrity-failure"


def test_diagnostics_that_cannot_be_built_still_print_a_line(
    recovery: test_recovery.RecoveryTests,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    brief = approved(recovery)
    invalidate_config(recovery)

    def broken(*arguments: object) -> JsonObject:
        raise RuntimeError("diagnostics broke")

    monkeypatch.setattr(runtime_emergency, "_diagnostics", broken)

    code, err, diagnostics = run(
        recovery,
        monkeypatch,
        capsys,
        "dispatch",
        "send",
        "--dispatch",
        brief["dispatch_id"],
    )

    assert code == 2
    assert err.index("DIAGNOSTICS: ") < err.index("ERROR: ")
    assert diagnostics == {
        "code": "internal-error",
        "step": "diagnostics",
        "detail": "diagnostics broke",
        "emergency_record": None,
    }
    assert store(recovery).read_emergency() is None
