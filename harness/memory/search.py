"""Read-only FTS literals, authoritative freshness and bounded pointer serialization."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import cast

from harness.token_estimator import estimate_tokens

from .index import SCHEMA_VERSION, LOCK_TIMEOUT, context, refresh
from .sources import allowed_paths, read_source


def degraded(status: str, diagnostic: str) -> dict[str, object]:
    """Empty pointers preserve an explicit reason instead of inventing successful hits."""
    return {"status": status, "pointers": [], "diagnostic": diagnostic}


def search_candidates(repo: Path, query: str) -> dict[str, object]:
    """Search existing cache with mode=ro; never ingest, repair or create files."""
    try:
        canonical, path, policy = context(repo)
    except ValueError as exc:
        return degraded("invalid_policy", str(exc))
    if not policy.active:
        return degraded("disabled", "memory disabled or source allowlist empty")
    terms = re.findall(r"\w+", query, flags=re.UNICODE)
    if not terms:
        return degraded("empty_query", "query has no literal terms")
    if not path.is_file():
        return degraded("index_missing", "run harness memory build from main checkout")
    # Operators, quotes and punctuation are interpreted as literal words, never FTS syntax.
    literal = " OR ".join('"' + term + '"' for term in terms)
    pointers: list[dict[str, object]] = []
    stale = False
    try:
        permitted = set(allowed_paths(canonical, policy))
        connection = sqlite3.connect(
            path.as_uri() + "?mode=ro", uri=True, timeout=LOCK_TIMEOUT
        )
        try:
            connection.execute("PRAGMA query_only=ON")
            manifest = dict(connection.execute("SELECT key,value FROM manifest"))
            if (
                manifest.get("schema_version") != SCHEMA_VERSION
                or manifest.get("policy_fingerprint") != policy.fingerprint
            ):
                return degraded(
                    "index_incompatible",
                    "policy or schema changed; run harness memory build or rebuild from main checkout",
                )
            rows = connection.execute(
                "SELECT d.source_type,d.title,d.status,d.date,d.superseded_by,d.path,d.source_hash FROM search_text JOIN documents d ON d.id=search_text.rowid WHERE search_text MATCH ? ORDER BY bm25(search_text),d.path",
                (literal,),
            )
            for (
                source_type,
                title,
                status,
                date,
                superseded_by,
                relative,
                source_hash,
            ) in rows:
                if relative not in permitted or source_type not in policy.source_types:
                    stale = True
                    continue
                try:
                    document = read_source(canonical, relative, policy)
                except (OSError, ValueError):
                    document = None
                if document is None or document.source_hash != source_hash:
                    stale = True
                    continue
                # Use sanitized authoritative metadata, never arbitrary cached fields.
                if len(relative) > 1024:
                    continue
                pointer = {
                    "title": document.title[:256],
                    "status": document.status[:64],
                    "date": document.date[:64],
                    "superseded_by": document.superseded_by[:256],
                    "history_to_verify": True,
                    "path": relative,
                    "source_hash": source_hash,
                    "source_type": document.source_type,
                    "inclusion_reason": "literal relevance; historical source requires verification",
                }
                pointers.append(pointer)
        finally:
            connection.close()
    except (sqlite3.Error, OSError, ValueError):
        return degraded(
            "index_unavailable",
            "index unreadable or corrupt; check FTS5 support or run harness memory rebuild from main checkout",
        )
    result: dict[str, object] = {"status": "ok", "pointers": pointers}
    if stale:
        result["status"] = "stale_sources"
        result["diagnostic"] = (
            "changed, removed or revoked sources omitted; run harness memory build from main checkout"
        )
    return result


def search(repo: Path, query: str) -> dict[str, object]:
    """Return bounded pointers from the existing read-only cache."""
    result = search_candidates(repo, query)
    try:
        _, _, policy = context(repo)
    except ValueError:
        return result
    selected: list[dict[str, object]] = []
    for candidate in cast(list[dict[str, object]], result["pointers"]):
        pointer = {
            key: value
            for key, value in candidate.items()
            if key not in {"source_type", "inclusion_reason"}
        }
        if (
            estimate_tokens(json.dumps([*selected, pointer], ensure_ascii=False))
            <= policy.max_tokens
        ):
            selected.append(pointer)
        if len(selected) >= policy.top_k:
            break
    return {**result, "pointers": selected}


def search_with_refresh(repo: Path, query: str) -> dict[str, object]:
    """Main checkout refreshes derived history; linked checkout remains read-only."""
    try:
        canonical, _, policy = context(repo)
    except ValueError:
        return degraded("invalid_policy", "cannot read valid memory policy")
    if not policy.active or not re.findall(r"\w+", query, flags=re.UNICODE):
        return search(repo, query)
    if canonical == repo.resolve():
        try:
            refresh(repo)
        except (ValueError, OSError):
            return degraded(
                "refresh_failed",
                "memory refresh failed; previous cache retained; retry or explicitly rebuild from main checkout",
            )
    return search(repo, query)
