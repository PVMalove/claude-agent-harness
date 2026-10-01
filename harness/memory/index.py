"""SQLite FTS5 derived cache; writers operate only on the canonical checkout."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from collections.abc import Iterator
import sqlite3
from pathlib import Path

from harness.storage import storage_path, storage_root

from .policy import Policy, parse_policy
from .sources import Source, collect_sources
from . import sources
from .adapters import selected_generation

SCHEMA_VERSION = "2"
LOCK_TIMEOUT = 2.0


def context(repo: Path, *, writer: bool = False) -> tuple[Path, Path, Policy]:
    """Resolve canonical sources, shared cache and main-checkout configuration."""
    canonical = storage_root(repo, require_main_checkout=True).parent
    if writer and canonical != repo.resolve():
        raise ValueError(f"memory writers require main checkout: {canonical}")
    try:
        config = json.loads(
            (canonical / ".harness/project.json").read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        config = {}
    except (OSError, ValueError):
        raise ValueError("cannot read memory project configuration") from None
    if not isinstance(config, dict):
        raise ValueError("memory project configuration must be an object")
    return (
        canonical,
        storage_path(canonical, "cache", "memory", "index.sqlite3"),
        parse_policy(config),
    )


def initialize(connection: sqlite3.Connection) -> None:
    """Create an ordinary rollback-journal database with sanitized FTS content."""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS manifest (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS documents (id INTEGER PRIMARY KEY, source_type TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL, date TEXT NOT NULL, superseded_by TEXT NOT NULL, path TEXT UNIQUE NOT NULL, source_hash TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS source_hashes (path TEXT PRIMARY KEY, source_hash TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS search_text USING fts5(title, body)"
    )


def replace_documents(
    connection: sqlite3.Connection,
    documents: list[Source],
    policy: Policy,
    retained: set[str] | None = None,
) -> None:
    """Incremental hashes and revocation share one transaction, including policy changes."""
    manifest = dict(connection.execute("SELECT key, value FROM manifest"))
    if manifest and manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("memory schema mismatch; run harness memory rebuild")
    if manifest.get("policy_fingerprint") != policy.fingerprint:
        connection.execute("DELETE FROM documents")
        connection.execute("DELETE FROM search_text")
        connection.execute("DELETE FROM source_hashes")
    current = {
        row[1]: (row[0], row[2])
        for row in connection.execute("SELECT id, path, source_hash FROM documents")
    }
    retained = (
        {document.path for document in documents} if retained is None else retained
    )
    for path, (identifier, _) in current.items():
        if path not in retained:
            connection.execute("DELETE FROM search_text WHERE rowid=?", (identifier,))
            connection.execute("DELETE FROM documents WHERE id=?", (identifier,))
    for document in documents:
        existing = current.get(document.path)
        if existing and existing[1] == document.source_hash:
            continue
        if existing:
            connection.execute("DELETE FROM search_text WHERE rowid=?", (existing[0],))
            connection.execute("DELETE FROM documents WHERE id=?", (existing[0],))
        cursor = connection.execute(
            "INSERT INTO documents(source_type,title,status,date,superseded_by,path,source_hash) VALUES (?,?,?,?,?,?,?)",
            (
                document.source_type,
                document.title,
                document.status,
                document.date,
                document.superseded_by,
                document.path,
                document.source_hash,
            ),
        )
        connection.execute(
            "INSERT INTO search_text(rowid,title,body) VALUES (?,?,?)",
            (cursor.lastrowid, document.title, document.body),
        )
    connection.executemany(
        "INSERT INTO manifest VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE manifest.value != excluded.value",
        [
            ("schema_version", SCHEMA_VERSION),
            ("policy_fingerprint", policy.fingerprint),
            ("min_similarity", str(policy.min_similarity)),
        ],
    )


@contextmanager
def writer_lock(path: Path) -> Iterator[None]:
    """Serialize build/rebuild publication with a bounded SQLite lock, released on process exit."""
    lock = sqlite3.connect(path.parent / "writer.sqlite3", timeout=LOCK_TIMEOUT)
    try:
        try:
            lock.execute("BEGIN IMMEDIATE")
        except sqlite3.Error:
            raise ValueError(
                "memory writer busy or lock unavailable; retry after the active writer finishes"
            ) from None
        yield
    finally:
        lock.rollback()
        lock.close()


def write_index(path: Path, documents: list[Source], policy: Policy) -> None:
    """Commit sanitized documents and verify the complete database before publication."""
    connection = sqlite3.connect(path, timeout=LOCK_TIMEOUT)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        with connection:
            initialize(connection)
            replace_documents(connection, documents, policy)
            connection.executemany(
                "INSERT OR REPLACE INTO source_hashes VALUES (?,?)",
                [(d.path, d.source_hash) for d in documents],
            )
            connection.execute(
                "INSERT INTO search_text(search_text) VALUES ('integrity-check')"
            )
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("memory index integrity check failed")
    finally:
        connection.close()


def _publish_fresh(path: Path, documents: list[Source], policy: Policy) -> None:
    """Publish an adjacent verified cache; failed replace preserves the prior cache."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".index-", suffix=".sqlite3", delete=False
        ) as stream:
            temporary = Path(stream.name)
        write_index(temporary, documents, policy)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def refresh(repo: Path) -> dict[str, object]:
    """Hash authorized bytes first; project only changes under the bounded writer lock."""
    canonical, path, policy = context(repo, writer=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with writer_lock(path):
            if path.exists():
                with sqlite3.connect(
                    path.as_uri() + "?mode=ro", uri=True, timeout=LOCK_TIMEOUT
                ) as probe:
                    manifest = dict(probe.execute("SELECT key,value FROM manifest"))
                    if probe.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                        raise ValueError(
                            "memory index corrupt; run harness memory rebuild"
                        )
                if manifest.get("schema_version") != SCHEMA_VERSION:
                    documents = collect_sources(canonical, policy)
                    _publish_fresh(path, documents, policy)
                    return {"status": "built", "indexed": len(documents)}
            connection = sqlite3.connect(path, timeout=LOCK_TIMEOUT)
            try:
                initialize(connection)
                manifest = dict(connection.execute("SELECT key,value FROM manifest"))
                known = (
                    dict(
                        connection.execute("SELECT path,source_hash FROM source_hashes")
                    )
                    if manifest.get("policy_fingerprint") == policy.fingerprint
                    else {}
                )
                generation = (
                    selected_generation(canonical)
                    if sources.STATE_SOURCE_TYPES & set(policy.source_types)
                    and policy.active
                    else ""
                )
                snapshot = (
                    sources.snapshot_manifest(canonical) if policy.active else b""
                )
                permitted = sources.allowed_paths(canonical, policy)
                retained = set(permitted)
                hashes: dict[str, str] = {}
                documents = []
                for relative in permitted:
                    try:
                        raw = sources.raw_bytes(canonical, relative)
                    except FileNotFoundError:
                        retained.discard(relative)
                        continue
                    digest = hashlib.sha256(raw).hexdigest()
                    hashes[relative] = digest
                    if known.get(relative) == digest:
                        continue
                    document = sources.read_source(canonical, relative, policy, raw=raw)
                    if document is None:
                        retained.discard(relative)
                    else:
                        documents.append(document)
                if generation and selected_generation(canonical) != generation:
                    raise ValueError(
                        "memory ledger generation changed during ingestion; retry"
                    )
                if policy.active and sources.snapshot_manifest(canonical) != snapshot:
                    raise ValueError("memory snapshot changed during ingestion; retry")
                with connection:
                    replace_documents(connection, documents, policy, retained)
                    for relative in set(known) - hashes.keys():
                        connection.execute(
                            "DELETE FROM source_hashes WHERE path=?", (relative,)
                        )
                    connection.executemany(
                        "INSERT INTO source_hashes VALUES (?,?) ON CONFLICT(path) DO UPDATE SET source_hash=excluded.source_hash WHERE source_hashes.source_hash != excluded.source_hash",
                        hashes.items(),
                    )
                    connection.execute(
                        "INSERT INTO search_text(search_text) VALUES ('integrity-check')"
                    )
                indexed = connection.execute(
                    "SELECT count(*) FROM documents"
                ).fetchone()[0]
            finally:
                connection.close()
    except (sqlite3.Error, OSError):
        raise ValueError(
            "memory refresh failed; previous cache retained; check permissions, writer and FTS5 or run harness memory rebuild"
        ) from None
    return {"status": "built", "indexed": indexed}


def build(repo: Path) -> dict[str, object]:
    """Explicit main-checkout refresh of the derived corpus."""
    return refresh(repo)


def rebuild(repo: Path) -> dict[str, object]:
    """Build a fresh adjacent SQLite cache and atomically publish only after full verification."""
    canonical, path, policy = context(repo, writer=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with writer_lock(path):
            documents = collect_sources(canonical, policy)
            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=".index-", suffix=".sqlite3", delete=False
            ) as stream:
                temporary = Path(stream.name)
            write_index(temporary, documents, policy)
            os.replace(temporary, path)
    except (sqlite3.Error, OSError):
        raise ValueError(
            "memory rebuild failed; previous index retained; check FTS5 support, permissions and active readers"
        ) from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"status": "rebuilt", "indexed": len(documents)}
