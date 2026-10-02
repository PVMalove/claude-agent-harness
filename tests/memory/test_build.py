"""Build real offline caches from explicitly selected Markdown sources."""

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from harness.memory import build
from harness.storage import storage_path


def configure(repo: Path, **overrides: object) -> None:
    """Give a fixture explicit policy without installing any runtime state."""
    (repo / ".harness").mkdir(exist_ok=True)
    config = {
        "memory": {"enabled": True},
        "memory_policy": {
            "source_types": ["adr", "glossary"],
            "allow_paths": ["docs/adr/*.md", "CONTEXT.md"],
            "redact_rules": ["secret=[^\\s]+"],
            "min_similarity": 1,
            "top_k": 5,
            "max_tokens": 1000,
            **overrides,
        },
    }
    (repo / ".harness/project.json").write_text(json.dumps(config), encoding="utf-8")


def source(repo: Path, path: str, text: str) -> Path:
    """Write an authoritative fixture source."""
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def test_build_selects_explicit_types_paths_and_redacts_before_persistence(
    tmp_path: Path,
) -> None:
    """The cache contains sanitized corpus only and keeps original-byte provenance."""
    configure(tmp_path, source_types=["adr"])
    text = "# ADR secret=title\nStatus: accepted\ntransaction secret=body\n"
    source(tmp_path, "docs/adr/0001.md", text)
    source(tmp_path, "CONTEXT.md", "# Glossary\nsecret=hidden\n")
    source(tmp_path, "private.md", "secret=private")
    result = build(tmp_path)
    assert result == {"status": "built", "indexed": 1}
    db = storage_path(tmp_path, "cache", "memory", "index.sqlite3")
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT title, status, path, source_hash FROM documents"
        ).fetchall() == [
            (
                "ADR [REDACTED]",
                "accepted",
                "docs/adr/0001.md",
                hashlib.sha256(text.encode()).hexdigest(),
            )
        ]
        dump = "\n".join(connection.iterdump())
        assert "secret=" not in dump
        assert "[REDACTED]" in dump


def test_build_refreshes_hashes_deletes_sources_and_revokes_policy(
    tmp_path: Path,
) -> None:
    """Repeated builds track authoritative changes and remove revoked records."""
    configure(tmp_path)
    file = source(tmp_path, "CONTEXT.md", "# Glossary\nold term")
    assert build(tmp_path)["indexed"] == 1
    assert build(tmp_path)["indexed"] == 1
    file.write_text("# Changed\nnew term", encoding="utf-8")
    assert build(tmp_path)["indexed"] == 1
    with sqlite3.connect(
        storage_path(tmp_path, "cache", "memory", "index.sqlite3")
    ) as db:
        assert db.execute("SELECT title FROM documents").fetchone() == ("Changed",)
    file.unlink()
    assert build(tmp_path)["indexed"] == 0
    source(tmp_path, "CONTEXT.md", "# Glossary")
    build(tmp_path)
    configure(tmp_path, allow_paths=[])
    assert build(tmp_path)["indexed"] == 0
    with sqlite3.connect(
        storage_path(tmp_path, "cache", "memory", "index.sqlite3")
    ) as db:
        assert db.execute("SELECT count(*) FROM search_text").fetchone() == (0,)


@pytest.mark.parametrize("change", [{"allow_paths": []}, {"source_types": []}])
def test_empty_allowlist_never_reads_sources(
    tmp_path: Path, change: dict[str, object]
) -> None:
    """Even unreadable selected Markdown is irrelevant without both allowlists."""
    configure(tmp_path, **change)
    source(tmp_path, "CONTEXT.md", "# Not selected")
    assert build(tmp_path)["indexed"] == 0


def test_build_rejects_symlink_components(tmp_path: Path) -> None:
    """Explicit allow_paths cannot authorize following symlinks out of the corpus."""
    configure(tmp_path)
    outside = source(tmp_path, "outside.md", "# Secret")
    (tmp_path / "CONTEXT.md").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        build(tmp_path)
    (tmp_path / "CONTEXT.md").unlink()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/adr").symlink_to(tmp_path, target_is_directory=True)
    configure(tmp_path, allow_paths=["docs/adr/outside.md"])
    with pytest.raises(ValueError, match="symlink"):
        build(tmp_path)


@pytest.mark.parametrize(
    "content",
    [b"\xff", b"x" * (1024 * 1024 + 1)],
    ids=["non-utf8", "oversized"],
)
def test_build_rejects_non_utf8_and_oversized_sources(
    tmp_path: Path, content: bytes
) -> None:
    """Invalid corpus cannot silently create a partially updated cache."""
    configure(tmp_path)
    (tmp_path / "CONTEXT.md").write_bytes(content)
    with pytest.raises(ValueError, match="UTF-8|1 MiB"):
        build(tmp_path)


def test_explicit_superseded_status_is_excluded_without_guessing_body(
    tmp_path: Path,
) -> None:
    """Status metadata controls exclusion; a body mention does not."""
    configure(tmp_path)
    source(tmp_path, "docs/adr/1.md", "# Old\nStatus: superseded\ntransaction")
    source(
        tmp_path,
        "docs/adr/2.md",
        "# Current\n## Статус\nПринято\nbody mentions superseded",
    )
    source(tmp_path, "docs/adr/3.md", "# Unknown\nno explicit status")
    assert build(tmp_path)["indexed"] == 2
    with sqlite3.connect(
        storage_path(tmp_path, "cache", "memory", "index.sqlite3")
    ) as db:
        assert db.execute("SELECT status FROM documents ORDER BY path").fetchall() == [
            ("принято",),
            ("unknown",),
        ]


def git(repo: Path, *args: str) -> None:
    """Run Git operations in disposable fixtures only."""
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_linked_worktree_cannot_write_the_shared_main_cache(tmp_path: Path) -> None:
    """Main checkout owns both policy and source bytes for the shared index."""
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q")
    git(main, "config", "user.name", "Fixture")
    git(main, "config", "user.email", "fixture@example.invalid")
    configure(main)
    source(main, "CONTEXT.md", "# Main glossary")
    git(main, "add", "CONTEXT.md")
    git(main, "commit", "-qm", "fixture")
    linked = tmp_path / "linked"
    git(main, "worktree", "add", "-qb", "fixture-linked", str(linked))
    assert build(main)["indexed"] == 1
    assert storage_path(main, "cache", "memory", "index.sqlite3") == storage_path(
        linked, "cache", "memory", "index.sqlite3"
    )
    with pytest.raises(ValueError, match="main checkout"):
        build(linked)
