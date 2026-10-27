"""The audit chain of the runtime store and ``ledger validate --full`` (ADR 0020, section 4).

A real runtime serves a cut-over state the real coordinator wrote. The tests play the attacker: they
change ``runtime.db`` with the standard ``sqlite3`` module, in the test process only (no module of
the coordinator opens it). An ordinary command (``dispatch status``, ``ledger validate``) verifies
the audit records after the checkpoint; ``ledger validate --full`` recomputes the whole chain from
its genesis against the import anchor, never uses the checkpoint, and writes nothing when it fails.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness.json_types import JsonObject
from harness.orchestration.ledger.lifecycle import _canonical

from .runtime_store_support import (
    STATE_REL,
    ServingRuntime,
    Store,
    build_state,
    coordinator,
    coordinator_repository,
    coordinator_with,
    migrate,
    tree,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the seam tests drive a Unix-socket runtime"
)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    store = Store(tmp_path)
    try:
        yield store
    finally:
        store.stop_runtime()


def _database(store: Store) -> sqlite3.Connection:
    return sqlite3.connect(store.database, timeout=10)


def _tamper(store: Store, *statements: str) -> None:
    connection = _database(store)
    try:
        for statement in statements:
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()


def _dump(store: Store) -> list[list[tuple[object, ...]]]:
    """The audit chain and its checkpoint as the file holds them."""
    connection = _database(store)
    try:
        return [
            connection.execute("SELECT * FROM audit ORDER BY seq").fetchall(),
            connection.execute("SELECT * FROM audit_checkpoint").fetchall(),
        ]
    finally:
        connection.close()


def _tail(store: Store) -> tuple[int, str]:
    connection = _database(store)
    try:
        row = connection.execute(
            "SELECT seq, chain_sha256 FROM audit ORDER BY seq DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    return int(row[0]), str(row[1])


def _record_sha256(at: str, action: str, details: JsonObject) -> str:
    text = _canonical({"action": action, "at": at, "details": details})
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _remedy(result_stderr: str) -> str:
    return next(line for line in result_stderr.splitlines() if line.startswith("REMEDY:"))


def _full(store: Store) -> tuple[int, str]:
    result = store.run("ledger", "validate", "--full")
    return result.returncode, result.stderr


def test_an_untouched_store_passes_the_ordinary_and_the_full_validation(store: Store) -> None:
    ordinary = store.run("ledger", "validate")
    full = store.run("ledger", "validate", "--full")

    assert ordinary.returncode == 0, ordinary.stderr
    assert full.returncode == 0, full.stderr
    report = json.loads(full.stdout)
    assert report["valid"] is True
    assert report["store"]["mode"] == "full"
    assert report["batches"] == [store.batch]


def test_full_validation_accepts_a_migration_repaired_batch(tmp_path: Path) -> None:
    repo = coordinator_repository(tmp_path)
    build_state(repo, owes_reassessment_without_candidate=True)
    result = migrate(repo)
    assert result.returncode == 0, result.stderr
    runtime = ServingRuntime(repo)
    try:
        result = coordinator_with(repo, runtime, "ledger", "validate", "--full")
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["store"]["valid"] is True
    finally:
        runtime.stop()


def test_a_record_changed_after_the_checkpoint_covered_it_is_found_by_the_full_check_only(
    store: Store,
) -> None:
    _tamper(store, "UPDATE audit SET details_json = '{\"tampered\":true}' WHERE seq = 1")

    status = store.run("dispatch", "status")
    ordinary = store.run("ledger", "validate")
    code, stderr = _full(store)

    assert status.returncode == 0, status.stderr
    assert ordinary.returncode == 0, ordinary.stderr
    assert code == 2
    assert _remedy(stderr).startswith("REMEDY: [invariant_violation] ")
    assert "audit_record" in stderr


def test_a_record_added_after_the_checkpoint_stops_every_ordinary_command(store: Store) -> None:
    seq, chain = _tail(store)
    at = "2026-10-01T00:00:00+00:00"
    details: JsonObject = {"path": "reports/forged.json", "sha256": "0"}
    record = _record_sha256(at, "immutable-record", details)
    forged = hashlib.sha256((chain + record).encode()).hexdigest()
    connection = _database(store)
    connection.execute(
        "INSERT INTO audit VALUES (?, ?, 'immutable-record', ?, ?, ?, ?)",
        (seq + 1, at, json.dumps(details), "0" * 64, chain, forged),
    )
    connection.commit()
    connection.close()

    status = store.run("dispatch", "status")
    ordinary = store.run("ledger", "validate")
    heartbeat = store.heartbeat()

    for result in (status, ordinary, heartbeat):
        assert result.returncode == 2, result.stderr
        assert _remedy(result.stderr).startswith("REMEDY: [invariant_violation] ")
        assert "audit_record" in result.stderr


@pytest.mark.parametrize("damage", ["delete", "corrupt"])
def test_a_deleted_or_damaged_checkpoint_is_rebuilt_without_an_error(
    store: Store, damage: str
) -> None:
    if damage == "delete":
        _tamper(store, "DELETE FROM audit_checkpoint")
    else:
        _tamper(store, "UPDATE audit_checkpoint SET chain_sha256 = 'damaged'")

    status = store.run("dispatch", "status")
    beat = store.heartbeat()

    assert status.returncode == 0, status.stderr
    assert beat.returncode == 0, beat.stderr
    connection = _database(store)
    seq, chain = connection.execute("SELECT seq, chain_sha256 FROM audit_checkpoint").fetchone()
    connection.close()
    assert (seq, chain) == _tail(store)


def test_the_full_check_rebuilds_a_deleted_checkpoint(store: Store) -> None:
    _tamper(store, "DELETE FROM audit_checkpoint")

    code, stderr = _full(store)

    assert code == 0, stderr
    connection = _database(store)
    rows = connection.execute("SELECT seq FROM audit_checkpoint").fetchall()
    connection.close()
    assert rows == [(_tail(store)[0],)]


@pytest.mark.parametrize("attack", ["deletion", "insertion", "reordering"])
def test_a_deleted_inserted_or_reordered_record_is_found_and_the_store_is_not_changed(
    store: Store, attack: str
) -> None:
    store.heartbeat()
    store.heartbeat()
    seq, _ = _tail(store)
    assert seq >= 4
    statements: tuple[str, ...]
    if attack == "deletion":
        statements = (f"DELETE FROM audit WHERE seq = {seq - 1}",)
    elif attack == "insertion":
        details = json.dumps({"path": "reports/forged.json", "sha256": "0"})
        record = _record_sha256(
            "2026-10-01T00:00:00+00:00", "immutable-record", json.loads(details)
        )
        statements = (
            f"UPDATE audit SET seq = -seq WHERE seq >= {seq - 1}",
            "UPDATE audit SET seq = 1 - seq WHERE seq < 0",
            f"INSERT INTO audit SELECT {seq - 1}, '2026-10-01T00:00:00+00:00', 'immutable-record', "
            f"'{details}', '{record}', chain_sha256, 'x' FROM audit WHERE seq = {seq - 2}",
        )
    else:
        statements = (
            f"UPDATE audit SET seq = 0 WHERE seq = {seq - 1}",
            f"UPDATE audit SET seq = {seq - 1} WHERE seq = {seq}",
            f"UPDATE audit SET seq = {seq} WHERE seq = 0",
        )
    _tamper(store, *statements)
    before = _dump(store)

    code, stderr = _full(store)

    assert code == 2, attack
    assert _remedy(stderr).startswith("REMEDY: [invariant_violation] ")
    assert _dump(store) == before


def test_a_row_changed_outside_the_runtime_is_found_by_the_last_audit_record_of_its_path(
    store: Store,
) -> None:
    _tamper(
        store,
        "UPDATE dispatch_status SET body_json = json_set(body_json, '$.note', 'edited') "
        f"WHERE id = '{store.dispatch}'",
    )

    code, stderr = _full(store)

    assert code == 2
    assert "dispatch-status/" in stderr
    assert "last audit record" in stderr


def test_the_legacy_audit_the_import_stored_cannot_change(store: Store) -> None:
    _tamper(
        store,
        "UPDATE legacy_audit SET bytes = CAST('{}' AS BLOB) "
        "WHERE path = (SELECT MIN(path) FROM legacy_audit)",
    )

    code, stderr = _full(store)

    assert code == 2
    assert "legacy_audit" in stderr


def test_a_row_that_breaks_a_check_constraint_is_found_by_the_integrity_check(
    store: Store,
) -> None:
    _tamper(
        store,
        "PRAGMA ignore_check_constraints = ON",
        f"UPDATE dispatch_status SET execution_state = 'running', state = 'reported' "
        f"WHERE id = '{store.dispatch}'",
    )

    code, stderr = _full(store)

    assert code == 2
    assert "integrity_check" in stderr


def test_the_status_of_the_store_names_its_authority_and_the_audit_records(store: Store) -> None:
    result = store.run("ledger", "status")

    assert result.returncode == 0, result.stderr
    status = json.loads(result.stdout)
    assert status["authority"] == "runtime-store"
    assert status["audit_records"] == store.audit_records()


@pytest.mark.parametrize("command", ["clean", "migrate"])
def test_the_commands_that_rewrite_the_file_ledger_are_refused_in_the_store_epoch(
    store: Store, command: str
) -> None:
    before = tree(store.repo / STATE_REL, skip=("runtime.db",))
    dump = _dump(store)

    result = store.run("ledger", command)

    assert result.returncode == 2
    assert f"`ledger {command}` does not run in the runtime-store epoch" in result.stderr
    assert tree(store.repo / STATE_REL, skip=("runtime.db",)) == before
    assert _dump(store) == dump


def test_full_is_accepted_for_a_file_ledger_and_means_the_ordinary_check(tmp_path: Path) -> None:
    build_state(tmp_path)

    ordinary = coordinator(tmp_path, "ledger", "validate")
    full = coordinator(tmp_path, "ledger", "validate", "--full")

    assert ordinary.returncode == full.returncode == 0, full.stderr
    assert json.loads(ordinary.stdout) == json.loads(full.stdout)


def test_the_import_leaves_the_file_ledger_in_the_snapshot_unchanged(tmp_path: Path) -> None:
    build_state(tmp_path)
    legacy = tree(tmp_path / STATE_REL)

    assert migrate(tmp_path).returncode == 0

    state = tree(tmp_path / STATE_REL, skip=("runtime.db", "AUTHORITY.json", "ledger.json"))
    assert {path: data for path, data in state.items() if path in legacy} == {
        path: data for path, data in legacy.items() if path in state
    }
