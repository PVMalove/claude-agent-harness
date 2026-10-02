"""Lazy main-checkout refresh and unchanged raw-query boundaries."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from harness.memory import search
from harness.storage import storage_path
from .test_build import configure, source


def test_main_search_refreshes_once_and_only_reindexes_changed_bytes(
    tmp_path: Path,
) -> None:
    """Caller sees fresh history immediately while unchanged sources skip projection."""
    from harness.memory import search_with_refresh

    configure(tmp_path)
    file = source(tmp_path, "CONTEXT.md", "# Glossary\noldword secret=one")
    first = search_with_refresh(tmp_path, "oldword")
    assert first["status"] == "ok" and first["pointers"]
    index = storage_path(tmp_path, "cache", "memory", "index.sqlite3")
    before = index.read_bytes()
    with patch(
        "harness.memory.sources.read_source",
        side_effect=AssertionError("unchanged source projected again"),
    ):
        # Raw query still revalidates sources independently; skip only refresh projection.
        from harness.memory.index import refresh

        assert refresh(tmp_path)["indexed"] == 1
    assert index.read_bytes() == before
    file.write_text("# Changed\nnewword secret=two")
    result = search_with_refresh(tmp_path, "newword")
    pointers = result["pointers"]
    initial = first["pointers"]
    assert isinstance(pointers, list) and isinstance(initial, list)
    assert pointers and pointers[0]["title"] == "Changed"
    assert search(tmp_path, "oldword")["pointers"] == []
    assert pointers[0]["source_hash"] != initial[0]["source_hash"]
    with sqlite3.connect(index) as db:
        assert db.execute("SELECT count(*) FROM documents").fetchone() == (1,)


def test_unsafe_discovery_fails_atomically_without_returning_old_success(
    tmp_path: Path,
) -> None:
    """A selected symlink directory never disappears silently from the corpus."""
    from harness.memory import search_with_refresh

    configure(tmp_path, allow_paths=["docs/**/*.md"])
    source(tmp_path, "docs/glossary.md", "# Glossary\ntransaction")
    assert search_with_refresh(tmp_path, "transaction")["pointers"]
    index = storage_path(tmp_path, "cache", "memory", "index.sqlite3")
    before = index.read_bytes()
    outside = tmp_path / "outside"
    outside.mkdir()
    source(tmp_path, "outside/private.md", "# Unsafe\ntransaction")
    (tmp_path / "docs/adr").symlink_to(outside, target_is_directory=True)
    result = search_with_refresh(tmp_path, "transaction")
    assert result["status"] == "refresh_failed" and result["pointers"] == []
    assert index.read_bytes() == before


def test_disabled_empty_invalid_and_linked_refresh_never_write(tmp_path: Path) -> None:
    """Validation and main-checkout ownership precede any cache creation."""
    from harness.memory import build, search_with_refresh
    from .test_build import git

    configure(tmp_path)
    assert search_with_refresh(tmp_path, "*")["status"] == "empty_query"
    assert not storage_path(
        tmp_path, "cache", "memory", "index.sqlite3"
    ).parent.exists()
    configure(tmp_path, allow_paths=[])
    assert search_with_refresh(tmp_path, "term")["status"] == "disabled"
    config = tmp_path / ".harness/project.json"
    config.write_text("{invalid")
    assert search_with_refresh(tmp_path, "term")["status"] == "invalid_policy"
    assert not storage_path(
        tmp_path, "cache", "memory", "index.sqlite3"
    ).parent.exists()
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q")
    git(main, "config", "user.name", "Fixture")
    git(main, "config", "user.email", "fixture@example.invalid")
    configure(main)
    source(main, "CONTEXT.md", "# Main\ntransaction")
    git(main, "add", "CONTEXT.md")
    git(main, "commit", "-qm", "fixture")
    linked = tmp_path / "linked"
    git(main, "worktree", "add", "-qb", "fixture-linked", str(linked))
    assert search_with_refresh(linked, "transaction")["status"] == "index_missing"
    build(main)
    index = storage_path(main, "cache", "memory", "index.sqlite3")
    before = (
        index.read_bytes(),
        index.stat().st_mtime_ns,
        sorted(p.name for p in index.parent.iterdir()),
    )
    configure(linked, allow_paths=[])
    source(linked, "CONTEXT.md", "# Branch\nbranchword")
    pointers = search_with_refresh(linked, "transaction")["pointers"]
    assert isinstance(pointers, list)
    assert pointers[0]["title"] == "Main"
    assert before == (
        index.read_bytes(),
        index.stat().st_mtime_ns,
        sorted(p.name for p in index.parent.iterdir()),
    )


def test_main_refresh_failures_preserve_cache_and_report_empty_pointers(
    tmp_path: Path,
) -> None:
    """Corruption, invalid sources and a busy writer cannot masquerade as fresh success."""
    from harness.memory import search_with_refresh

    configure(tmp_path)
    file = source(tmp_path, "CONTEXT.md", "# Main\ntransaction")
    search_with_refresh(tmp_path, "transaction")
    index = storage_path(tmp_path, "cache", "memory", "index.sqlite3")
    before = index.read_bytes()
    file.write_bytes(b"\xff")
    assert search_with_refresh(tmp_path, "transaction")["status"] == "refresh_failed"
    assert index.read_bytes() == before
    file.write_text("# Main\ntransaction")
    with sqlite3.connect(index.parent / "writer.sqlite3") as lock:
        lock.execute("BEGIN IMMEDIATE")
        assert search_with_refresh(tmp_path, "transaction")["pointers"] == []
    assert index.read_bytes() == before
    index.write_bytes(b"corrupt")
    assert search_with_refresh(tmp_path, "transaction")["status"] == "refresh_failed"
    assert index.read_bytes() == b"corrupt"


def test_schema_replacement_primes_hashes_and_preserves_cache_on_replace_error(
    tmp_path: Path,
) -> None:
    """A derived schema upgrade is atomic and subsequent refresh does no projection."""
    from harness.memory import build, search_with_refresh
    from harness.memory.index import refresh

    configure(tmp_path)
    source(tmp_path, "CONTEXT.md", "# Main\ntransaction")
    build(tmp_path)
    index = storage_path(tmp_path, "cache", "memory", "index.sqlite3")
    db = sqlite3.connect(index)
    try:
        db.execute("UPDATE manifest SET value='old' WHERE key='schema_version'")
        db.commit()
    finally:
        db.close()
    before = index.read_bytes()
    with patch(
        "harness.memory.index.os.replace",
        side_effect=PermissionError("sharing conflict"),
    ):
        assert (
            search_with_refresh(tmp_path, "transaction")["status"] == "refresh_failed"
        )
    assert index.read_bytes() == before
    assert search_with_refresh(tmp_path, "transaction")["pointers"]
    with patch(
        "harness.memory.sources.read_source",
        side_effect=AssertionError("upgraded source projected again"),
    ):
        assert refresh(tmp_path)["indexed"] == 1


def test_ignored_sources_cannot_persist_secret_paths_in_hash_manifest(
    tmp_path: Path,
) -> None:
    """Even excluded status sources must pass pointer identity sanitization before SQL."""
    from harness.memory import search_with_refresh

    configure(tmp_path)
    source(tmp_path, "CONTEXT.md", "# Main\ntransaction")
    assert search_with_refresh(tmp_path, "transaction")["pointers"]
    index = storage_path(tmp_path, "cache", "memory", "index.sqlite3")
    before = index.read_bytes()
    source(tmp_path, "docs/adr/secret=private.md", "# Old\nStatus: superseded")
    assert search_with_refresh(tmp_path, "transaction")["status"] == "refresh_failed"
    assert index.read_bytes() == before
    assert b"private" not in index.read_bytes()


@pytest.mark.parametrize("bound", ["count", "traversal", "size"])
def test_ingestion_bounds_preserve_the_last_successful_cache(
    tmp_path: Path, bound: str
) -> None:
    """Candidate and traversal limits fail before a partial corpus is published."""
    from harness.memory import search_with_refresh

    configure(tmp_path, allow_paths=["docs/**/*.md"])
    source(tmp_path, "docs/glossary.md", "# Main\ntransaction")
    assert search_with_refresh(tmp_path, "transaction")["pointers"]
    index = storage_path(tmp_path, "cache", "memory", "index.sqlite3")
    before = index.read_bytes()
    directory = tmp_path / "docs/extra"
    directory.mkdir()
    if bound == "size":
        (directory / "large.md").write_bytes(b"x" * (1024 * 1024 + 1))
    else:
        count, suffix = (1001, ".md") if bound == "count" else (10001, ".txt")
        for number in range(count):
            (directory / f"{number}{suffix}").touch()
    assert search_with_refresh(tmp_path, "transaction")["status"] == "refresh_failed"
    assert index.read_bytes() == before


def test_generation_switch_invalid_json_and_policy_revocation_are_atomic(
    tmp_path: Path,
) -> None:
    """Selected generation and policy own both searchable documents and hash identities."""
    from harness.memory import search_with_refresh
    from .test_ingest import ledger_fixture

    configure(
        tmp_path,
        source_types=["qa_finding"],
        allow_paths=[".harness/orchestration/state/generations/**/*.json"],
    )
    generation = ledger_fixture(tmp_path)
    report = generation / "reports/qa.json"
    report.write_text(json.dumps({"role": "qa", "output": "oldword"}))
    assert search_with_refresh(tmp_path, "oldword")["pointers"]
    index = storage_path(tmp_path, "cache", "memory", "index.sqlite3")
    before = index.read_bytes()
    report.write_text("{broken")
    assert search_with_refresh(tmp_path, "oldword")["status"] == "refresh_failed"
    assert index.read_bytes() == before
    replacement = generation.parent / "generation-new"
    (replacement / "reports").mkdir(parents=True)
    (replacement / "reports/qa.json").write_text(
        json.dumps({"role": "qa", "output": "newword"})
    )
    selector = tmp_path / ".harness/orchestration/state/ledger.json"
    value = json.loads(selector.read_text())
    value["generation"] = "generation-new"
    selector.write_text(json.dumps(value))
    assert search_with_refresh(tmp_path, "newword")["pointers"]
    assert search(tmp_path, "oldword")["pointers"] == []
    with sqlite3.connect(index) as db:
        assert all(
            "generation-new" in row[0]
            for row in db.execute("SELECT path FROM source_hashes")
        )
    configure(tmp_path, source_types=["glossary"], allow_paths=["CONTEXT.md"])
    assert search_with_refresh(tmp_path, "newword")["pointers"] == []
    with sqlite3.connect(index) as db:
        assert db.execute("SELECT count(*) FROM source_hashes").fetchone() == (0,)
