"""SQLite FTS5 derived cache; writers operate only on the canonical checkout."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from harness.storage import storage_path, storage_root

from .policy import Policy, parse_policy
from .sources import Source, collect_sources

SCHEMA_VERSION = "1"
LOCK_TIMEOUT = 2.0


def context(repo: Path, *, writer: bool = False) -> tuple[Path, Path, Policy]:
    """Resolve canonical sources, shared cache and main-checkout configuration."""
    canonical = storage_root(repo).parent
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
        storage_path(repo, "cache", "memory", "index.sqlite3"),
        parse_policy(config),
    )


def initialize(connection: sqlite3.Connection) -> None:
    """Create an ordinary rollback-journal database with sanitized FTS content."""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS manifest (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS documents (id INTEGER PRIMARY KEY, source_type TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL, path TEXT UNIQUE NOT NULL, source_hash TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS search_text USING fts5(title, body)"
    )


def replace_documents(
    connection: sqlite3.Connection, documents: list[Source], policy: Policy
) -> None:
    """Incremental hashes and revocation share one transaction, including policy changes."""
    manifest = dict(connection.execute("SELECT key, value FROM manifest"))
    if manifest and manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("memory schema mismatch; run harness memory rebuild")
    if manifest.get("policy_fingerprint") != policy.fingerprint:
        connection.execute("DELETE FROM documents")
        connection.execute("DELETE FROM search_text")
    current = {
        row[1]: (row[0], row[2])
        for row in connection.execute("SELECT id, path, source_hash FROM documents")
    }
    retained = {document.path for document in documents}
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
            "INSERT INTO documents(source_type,title,status,path,source_hash) VALUES (?,?,?,?,?)",
            (
                document.source_type,
                document.title,
                document.status,
                document.path,
                document.source_hash,
            ),
        )
        connection.execute(
            "INSERT INTO search_text(rowid,title,body) VALUES (?,?,?)",
            (cursor.lastrowid, document.title, document.body),
        )
    connection.executemany(
        "INSERT OR REPLACE INTO manifest VALUES (?,?)",
        [
            ("schema_version", SCHEMA_VERSION),
            ("policy_fingerprint", policy.fingerprint),
            ("min_similarity", str(policy.min_similarity)),
        ],
    )


def build(repo: Path) -> dict[str, object]:
    """Explicitly refresh a rebuildable local cache; never ingest from a linked worktree."""
    canonical, path, policy = context(repo, writer=True)
    documents = collect_sources(canonical, policy)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        connection = sqlite3.connect(path, timeout=LOCK_TIMEOUT)
        try:
            connection.execute("PRAGMA journal_mode=DELETE")
            with connection:
                initialize(connection)
                replace_documents(connection, documents, policy)
        finally:
            connection.close()
    except sqlite3.Error:
        raise ValueError(
            "memory index unavailable or corrupt; check FTS5 support or run harness memory rebuild"
        ) from None
    return {"status": "built", "indexed": len(documents)}


def rebuild(repo: Path) -> dict[str, object]:
    """Full-rebuild entrypoint; atomic corrupt-cache recovery is the next slice."""
    result = build(repo)
    result["status"] = "rebuilt"
    return result
