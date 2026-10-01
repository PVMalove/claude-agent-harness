"""Read-only memory search returns bounded authoritative pointers."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from harness.memory import build, search
from harness.token_estimator import estimate_tokens
from harness.storage import storage_path
from .test_build import configure, source
from .test_delivery import CLI


def test_search_returns_unicode_pointers_without_source_text(tmp_path: Path) -> None:
    """Dormant min_similarity does not remove FTS hits; literals do not execute operators."""
    configure(tmp_path)
    source(tmp_path, "CONTEXT.md", "# Глоссарий\nТранзакция secret=hidden\n")
    build(tmp_path)
    result = search(tmp_path, '"Транзакция" OR *')
    assert result["status"] == "ok"
    pointers = result["pointers"]
    assert isinstance(pointers, list) and len(pointers) == 1
    assert set(pointers[0]) == {
        "title",
        "status",
        "date",
        "superseded_by",
        "path",
        "source_hash",
        "history_to_verify",
    }
    assert pointers[0]["title"] == "Глоссарий"
    assert pointers[0]["status"] == "unknown"
    assert "Транзакция" not in json.dumps(pointers, ensure_ascii=False)
    assert "hidden" not in json.dumps(pointers)


def test_search_excludes_adr_superseded_by_a_replacement(tmp_path: Path) -> None:
    """The ADR format's replacement reference revokes a precedent, not only its bare status."""
    configure(tmp_path)
    source(
        tmp_path,
        "docs/adr/0001-old.md",
        "# Old decision\nStatus: superseded by ADR-0002\ntransaction",
    )
    source(
        tmp_path,
        "docs/adr/0002-current.md",
        "# Current decision\nStatus: accepted\ntransaction",
    )
    build(tmp_path)
    pointers = search(tmp_path, "transaction")["pointers"]
    assert isinstance(pointers, list)
    assert [p["path"] for p in pointers] == ["docs/adr/0002-current.md"]


@pytest.mark.parametrize("query", ["", '"', "*()^-:", "NEAR( OR NOT )"])
def test_empty_and_operator_queries_return_no_raw_fts_error(
    tmp_path: Path, query: str
) -> None:
    """FTS metacharacters never reach the MATCH expression unquoted."""
    configure(tmp_path)
    source(tmp_path, "CONTEXT.md", "# Glossary\ntransaction")
    build(tmp_path)
    assert search(tmp_path, query)["pointers"] == []


def test_search_order_and_limits_apply_to_complete_serialized_pointers(
    tmp_path: Path,
) -> None:
    """Stable equal ranks use paths and never truncate a pointer to fit."""
    configure(tmp_path, top_k=1)
    source(tmp_path, "docs/adr/b.md", "# Same\ntransaction")
    source(tmp_path, "docs/adr/a.md", "# Same\ntransaction")
    build(tmp_path)
    pointers = search(tmp_path, "transaction")["pointers"]
    assert isinstance(pointers, list)
    assert [p["path"] for p in pointers] == ["docs/adr/a.md"]
    configure(tmp_path, max_tokens=1)
    build(tmp_path)
    pointers = search(tmp_path, "transaction")["pointers"]
    assert pointers == []
    assert estimate_tokens(json.dumps(pointers, ensure_ascii=False)) <= 1


def test_search_never_modifies_cache_or_creates_sidecars(tmp_path: Path) -> None:
    """A read-only filesystem cache is usable with unchanged size, mtime and entries."""
    configure(tmp_path)
    source(tmp_path, "CONTEXT.md", "# Glossary\ntransaction")
    build(tmp_path)
    index = storage_path(tmp_path, "cache", "memory", "index.sqlite3")
    index.chmod(0o444)
    before = (
        index.stat().st_mtime_ns,
        index.stat().st_size,
        sorted(p.name for p in index.parent.iterdir()),
    )
    try:
        assert search(tmp_path, "transaction")["status"] == "ok"
        assert search(tmp_path, "transaction")["pointers"]
        assert before == (
            index.stat().st_mtime_ns,
            index.stat().st_size,
            sorted(p.name for p in index.parent.iterdir()),
        )
    finally:
        index.chmod(0o600)


@pytest.mark.parametrize("change", ["edit", "delete", "supersede", "symlink"])
def test_search_omits_stale_or_unsafe_sources(tmp_path: Path, change: str) -> None:
    """Source provenance is rechecked before any cached pointer is returned."""
    configure(tmp_path)
    file = source(tmp_path, "CONTEXT.md", "# Glossary\ntransaction")
    build(tmp_path)
    if change == "edit":
        file.write_text("# Changed", encoding="utf-8")
    elif change == "supersede":
        file.write_text("# Old\nStatus: superseded", encoding="utf-8")
    else:
        file.unlink()
        if change == "symlink":
            file.symlink_to(source(tmp_path, "other.md", "# Glossary\ntransaction"))
    assert search(tmp_path, "transaction")["pointers"] == []


def test_search_denies_old_policy_without_rebuilding_or_writing(tmp_path: Path) -> None:
    """Policy revocation and changed redact rules cannot expose stale titles."""
    configure(tmp_path)
    source(tmp_path, "CONTEXT.md", "# Secret title\ntransaction")
    build(tmp_path)
    configure(tmp_path, redact_rules=["Secret"])
    assert search(tmp_path, "transaction")["status"] == "index_incompatible"
    assert search(tmp_path, "transaction")["pointers"] == []
    build(tmp_path)
    assert "Secret" not in json.dumps(search(tmp_path, "transaction"))
    configure(tmp_path, allow_paths=[])
    assert search(tmp_path, "transaction")["status"] == "disabled"


def test_search_missing_or_corrupt_index_degrades_without_repair(
    tmp_path: Path,
) -> None:
    """Read-only search always leaves explicit writer recovery to the caller."""
    configure(tmp_path)
    path = storage_path(tmp_path, "cache", "memory", "index.sqlite3")
    assert search(tmp_path, "term")["status"] == "index_missing"
    assert not path.parent.exists()
    path.parent.mkdir(parents=True)
    path.write_bytes(b"corrupt")
    assert search(tmp_path, "term")["status"] == "index_unavailable"
    assert path.read_bytes() == b"corrupt"


def test_public_cli_build_search_rebuild(tmp_path: Path) -> None:
    """Thin CLI emits JSON pointers and routes all three explicit operations."""
    configure(tmp_path)
    source(tmp_path, "CONTEXT.md", "# Glossary\ntransaction")
    for operation in ("build", "search", "rebuild"):
        argv = [sys.executable, str(CLI), "memory", operation, str(tmp_path)]
        if operation == "search":
            argv.append("transaction")
        process = subprocess.run(argv, capture_output=True, text=True)
        assert process.returncode == 0, process.stderr
        result = json.loads(process.stdout)
        if operation == "search":
            assert set(result["pointers"][0]) == {
                "title",
                "status",
                "date",
                "superseded_by",
                "path",
                "source_hash",
                "history_to_verify",
            }
        else:
            assert result["indexed"] == 1


def test_worktree_search_reads_authoritative_main_sources(tmp_path: Path) -> None:
    """Branch-local content cannot redefine pointers from the shared main corpus."""
    from .test_build import git

    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q")
    git(main, "config", "user.name", "Fixture")
    git(main, "config", "user.email", "fixture@example.invalid")
    configure(main)
    source(main, "CONTEXT.md", "# Main glossary\ntransaction")
    git(main, "add", "CONTEXT.md")
    git(main, "commit", "-qm", "fixture")
    linked = tmp_path / "linked"
    git(main, "worktree", "add", "-qb", "fixture-linked", str(linked))
    build(main)
    source(linked, "CONTEXT.md", "# Different branch glossary")
    assert search(linked, "transaction") == search(main, "transaction")


def test_metadata_is_explicit_sanitized_history_and_superseded_is_never_returned(
    tmp_path: Path,
) -> None:
    """Pointers retain declared metadata without claiming current truth."""
    configure(tmp_path)
    source(tmp_path, "docs/adr/old.md", "# Old\nStatus: superseded\ntransaction")
    source(
        tmp_path,
        "docs/adr/current.md",
        "---\nstatus: accepted\ndate: 2026-09-30\nsuperseded-by: secret=replacement\n---\n# Current\ntransaction mentions superseded",
    )
    build(tmp_path)
    pointers = search(tmp_path, "transaction")["pointers"]
    assert isinstance(pointers, list) and len(pointers) == 1
    assert pointers[0]["status"] == "accepted"
    assert pointers[0]["date"] == "2026-09-30"
    assert pointers[0]["superseded_by"] == "[REDACTED]"
    assert pointers[0]["history_to_verify"] is True
    source(tmp_path, "docs/adr/current.md", "# Old\nСтатус: заменён\ntransaction")
    assert search(tmp_path, "transaction")["pointers"] == []
