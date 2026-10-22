"""``harness-runtime migrate`` stopped, failed and disturbed at every step (ADR 0020, section 5).

A debug build of the real Rust process pauses at the boundary of each step
(``HARNESS_RUNTIME_TEST_PAUSE_AT``) or fails there (``HARNESS_RUNTIME_TEST_FAIL_AT``). The tests
kill it with SIGKILL, repeat the command, and write the legacy state while it is importing. One
authority must remain at every point: the file ledger before the commit point and the store after
it. CI builds the debug binary (``cargo build --locked``); a release binary has no hooks.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

from harness.orchestration.ledger.lifecycle import _canonical

from .runtime_store_support import (
    DISPATCH_IDS,
    ROOT,
    STATE_REL,
    PausedMigrate,
    authorities,
    build_state,
    load,
    migrate,
    tree,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the verdict owner-dead of a killed migrate is not provable on Windows (exit code 2)",
)

STEPS = (
    "preconditions",
    "locked",
    "snapshot",
    "import",
    "parity",
    "renamed",
    "authority",
    "pointer-replace",
    "pointer",
)


def _legacy(repo: Path) -> dict[str, bytes]:
    """The legacy generations and the lock audit: migrate never rewrites or deletes them."""
    state = repo / STATE_REL
    return {
        path: content
        for path, content in tree(state).items()
        if path.startswith(("generations/", "lock-audit/"))
    }


def _store_files(repo: Path) -> list[str]:
    return sorted(
        path.name
        for path in (repo / STATE_REL).iterdir()
        if path.name.startswith("runtime.db") or path.name == "AUTHORITY.json"
    )


def test_the_steps_the_tests_stop_at_are_the_steps_the_hooks_name() -> None:
    source = (ROOT / "native/harness-runtime/crates/state/src/hooks.rs").read_text(
        encoding="utf-8"
    )
    block = source.split("pub const STEPS")[1].split("];")[0]

    assert tuple(part.split('"')[1] for part in block.split(",") if '"' in part) == STEPS


@pytest.mark.parametrize("step", STEPS)
def test_a_sigkill_at_a_step_then_a_repeat_leaves_exactly_one_authority(
    tmp_path: Path, step: str
) -> None:
    build_state(tmp_path)
    legacy = _legacy(tmp_path)
    process = PausedMigrate(tmp_path, step)
    process.wait_paused()

    process.kill()

    killed = authorities(tmp_path)
    assert sum(killed.values()) == 1, killed
    # The pointer is the commit point: only a kill after it leaves the store as the authority.
    assert killed["runtime-store"] is (step == "pointer")
    assert _legacy(tmp_path) == legacy

    repeated = migrate(tmp_path)

    assert repeated.returncode == 0, repeated.stderr
    report = json.loads(repeated.stdout)
    assert report["already_cut_over"] is (step == "pointer")
    assert authorities(tmp_path) == {"file-ledger": False, "runtime-store": True}
    assert not (tmp_path / STATE_REL / ".coordinator.lock").exists()
    assert not (tmp_path / STATE_REL / "runtime.db.importing").exists()
    assert {path: content for path, content in _legacy(tmp_path).items() if path in legacy} == legacy
    again = migrate(tmp_path)
    assert json.loads(again.stdout)["already_cut_over"] is True


def test_a_killed_migrate_leaves_the_lock_that_the_next_one_removes_with_an_audit_record(
    tmp_path: Path,
) -> None:
    build_state(tmp_path)
    process = PausedMigrate(tmp_path, "locked")
    process.wait_paused()
    process.kill()
    lock = tmp_path / STATE_REL / ".coordinator.lock"
    owner = load(lock / "owner.json")
    assert owner["operation"] == "runtime-cutover"
    assert not (tmp_path / STATE_REL / "lock-audit").exists()

    assert migrate(tmp_path).returncode == 0

    audit = [
        load(path)
        for path in sorted((tmp_path / STATE_REL / "lock-audit").glob("*.json"))
    ]
    assert [record["action"] for record in audit] == ["ledger-lock-owner-dead-release"]
    details = audit[0]["details"]
    assert isinstance(details, dict)
    assert details["verdict"] == "owner-dead"
    assert not lock.exists()


def test_a_live_migrate_holds_the_lock_and_a_second_one_is_refused_with_exit_code_2(
    tmp_path: Path,
) -> None:
    build_state(tmp_path)
    first = PausedMigrate(tmp_path, "snapshot")
    first.wait_paused()

    second = migrate(tmp_path)

    assert second.returncode == 2
    assert "locked" in second.stderr
    assert (tmp_path / STATE_REL / ".coordinator.lock/owner.json").is_file()
    code, _, stderr = first.resume()
    assert code == 0, stderr
    assert authorities(tmp_path) == {"file-ledger": False, "runtime-store": True}


@pytest.mark.parametrize("step", STEPS[:-1])
def test_an_injected_failure_exits_3_and_leaves_the_file_ledger_the_authority(
    tmp_path: Path, step: str
) -> None:
    build_state(tmp_path)
    legacy = _legacy(tmp_path)

    failed = migrate(tmp_path, env={"HARNESS_RUNTIME_TEST_FAIL_AT": step})

    assert failed.returncode == 3, failed.stderr
    assert f"injected failure at the step {step}" in failed.stderr
    assert "REMEDY: repeat `harness-runtime migrate --repo <repo>`" in failed.stderr
    assert authorities(tmp_path) == {"file-ledger": True, "runtime-store": False}
    assert _store_files(tmp_path) == []
    assert not (tmp_path / STATE_REL / ".coordinator.lock").exists()
    assert _legacy(tmp_path) == legacy

    assert migrate(tmp_path).returncode == 0
    assert authorities(tmp_path) == {"file-ledger": False, "runtime-store": True}


def _rewrite_a_status(repo: Path) -> None:
    generation = _generation(repo)
    path = generation / "dispatch-status" / f"{DISPATCH_IDS[1]}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["note"] = "written while the import ran"
    path.write_text(_canonical(record), encoding="utf-8", newline="\n")


def _change_the_pointer(repo: Path) -> None:
    path = repo / STATE_REL / "ledger.json"
    pointer = load(path)
    pointer["selected_at"] = "2026-10-22T12:00:00+00:00"
    path.write_text(_canonical(pointer), encoding="utf-8", newline="\n")


def _add_a_record(repo: Path) -> None:
    (_generation(repo) / "qa-lane" / "sequence.json").write_text(
        _canonical({"next": 7}), encoding="utf-8", newline="\n"
    )


def _generation(repo: Path) -> Path:
    state = repo / STATE_REL
    return state / "generations" / str(load(state / "ledger.json")["generation"])


@pytest.mark.parametrize(
    "write", [_rewrite_a_status, _change_the_pointer, _add_a_record]
)
def test_a_legacy_write_during_the_import_exits_4_and_commits_no_cutover(
    tmp_path: Path, write: object
) -> None:
    assert callable(write)
    build_state(tmp_path)
    process = PausedMigrate(tmp_path, "import")
    process.wait_paused()
    write(tmp_path)

    code, stdout, stderr = process.resume()

    assert code == 4, stderr
    assert stdout == ""
    assert "the legacy state changed while the migration ran" in stderr
    assert "REMEDY: stop every writer of the state directory" in stderr
    assert authorities(tmp_path) == {"file-ledger": True, "runtime-store": False}
    assert _store_files(tmp_path) == []
    assert not (tmp_path / STATE_REL / ".coordinator.lock").exists()


def test_a_source_changed_after_the_stop_is_migrated_as_it_is_now(tmp_path: Path) -> None:
    build_state(tmp_path)
    process = PausedMigrate(tmp_path, "import")
    process.wait_paused()
    _rewrite_a_status(tmp_path)
    assert process.resume()[0] == 4

    repeated = migrate(tmp_path)

    assert repeated.returncode == 0, repeated.stderr
    with closing(
        sqlite3.connect(f"file:{tmp_path / STATE_REL / 'runtime.db'}?mode=ro", uri=True)
    ) as store:
        (body,) = store.execute(
            "SELECT body_json FROM dispatch_status WHERE id = ?", (DISPATCH_IDS[1],)
        ).fetchone()
    assert json.loads(body)["note"] == "written while the import ran"


def test_a_release_build_has_no_test_hooks() -> None:
    release = ROOT / "native/harness-runtime/target/release/harness-runtime"
    if not release.is_file():
        pytest.skip("no release build of harness-runtime (cargo build --release)")

    assert b"HARNESS_RUNTIME_TEST" not in release.read_bytes()
