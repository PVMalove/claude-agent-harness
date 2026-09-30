"""Atomic explicit rebuild recovers disposable cache without destroying valid data."""

import os
import sqlite3
from pathlib import Path

import pytest

from harness.memory import build, rebuild, search
from harness.storage import storage_path
from .test_build import configure, source


@pytest.mark.parametrize("broken", [None, b"corrupt SQLite cache"])
def test_rebuild_recovers_missing_or_corrupt_cache(
    tmp_path: Path, broken: bytes | None
) -> None:
    """Recovery derives fresh pointers entirely from the authoritative corpus."""
    configure(tmp_path)
    source(tmp_path, "CONTEXT.md", "# Glossary\ntransaction")
    path = storage_path(tmp_path, "cache", "memory", "index.sqlite3")
    if broken is not None:
        path.parent.mkdir(parents=True)
        path.write_bytes(broken)
    assert rebuild(tmp_path) == {"status": "rebuilt", "indexed": 1}
    assert search(tmp_path, "transaction")["pointers"]


def test_failed_publication_preserves_previous_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An atomic-replace failure cannot erase a previously usable cache."""
    configure(tmp_path)
    source(tmp_path, "CONTEXT.md", "# Glossary\ntransaction")
    build(tmp_path)
    path = storage_path(tmp_path, "cache", "memory", "index.sqlite3")
    before = path.read_bytes()

    def denied_replace(_source: object, _destination: object) -> None:
        """Simulate a filesystem publication failure at the OS boundary."""
        raise PermissionError("fixture publication denied")

    monkeypatch.setattr(os, "replace", denied_replace)
    with pytest.raises(ValueError, match="previous index retained"):
        rebuild(tmp_path)
    assert path.read_bytes() == before
    assert search(tmp_path, "transaction")["pointers"]
    assert not list(path.parent.glob(".index-*"))


def test_fts5_unavailable_is_diagnostic_and_preserves_good_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A SQLite boundary lacking virtual tables fails explicitly without deleting the cache."""
    configure(tmp_path)
    source(tmp_path, "CONTEXT.md", "# Glossary\ntransaction")
    build(tmp_path)
    path = storage_path(tmp_path, "cache", "memory", "index.sqlite3")
    before = path.read_bytes()
    original = sqlite3.connect

    def authorizer(
        action: int,
        _first: str | None,
        _second: str | None,
        _db: str | None,
        _trigger: str | None,
    ) -> int:
        """Simulate a SQLite distribution without CREATE VIRTUAL TABLE capability."""
        return (
            sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_CREATE_VTABLE
            else sqlite3.SQLITE_OK
        )

    def no_fts(database: Path, *, timeout: float) -> sqlite3.Connection:
        """Use real SQLite while denying virtual-table creation at its API boundary."""
        db = original(database, timeout=timeout)
        db.set_authorizer(authorizer)
        return db

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", no_fts)
        with pytest.raises(ValueError, match="FTS5"):
            rebuild(tmp_path)
    assert path.read_bytes() == before
    assert search(tmp_path, "transaction")["pointers"]


def test_parallel_writers_have_bounded_lock_failure(tmp_path: Path) -> None:
    """Build and rebuild share a lock independent of the atomically replaced index."""
    configure(tmp_path)
    build(tmp_path)
    path = storage_path(tmp_path, "cache", "memory", "index.sqlite3")
    db = sqlite3.connect(path.parent / "writer.sqlite3")
    try:
        db.execute("BEGIN IMMEDIATE")
        for writer in (build, rebuild):
            with pytest.raises(ValueError, match="writer busy"):
                writer(tmp_path)
    finally:
        db.rollback()
        db.close()
    assert rebuild(tmp_path)["status"] == "rebuilt"


def test_rebuild_redacts_status_before_normalizing_case(tmp_path: Path) -> None:
    """Case-sensitive redact rules protect metadata as well as title and body."""
    configure(tmp_path, redact_rules=["SecretStatus"])
    source(tmp_path, "CONTEXT.md", "# Glossary\nStatus: SecretStatus\ntransaction")
    rebuild(tmp_path)
    pointers = search(tmp_path, "transaction")["pointers"]
    assert isinstance(pointers, list)
    assert pointers[0]["status"] == "[REDACTED]"
