"""CLI contracts for a single repository memory cache across real Git worktrees."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from .test_build import configure, git, source
from .test_delivery import CLI


def command(repo: Path, operation: str, *args: str) -> list[str]:
    """Invoke the public memory CLI independently of the fixture's installed state."""
    return [sys.executable, str(CLI), "memory", operation, str(repo), *args]


def run_memory(repo: Path, operation: str, *args: str) -> dict[str, object]:
    """Read a successful CLI response, retaining stderr on failure."""
    process = subprocess.run(
        command(repo, operation, *args), capture_output=True, text=True
    )
    assert process.returncode == 0, process.stderr
    result: dict[str, object] = json.loads(process.stdout)
    return result


@pytest.fixture(
    params=[None, "metadata", ".git"], ids=["standard", "separate", "separate-dotgit"]
)
def checkouts(tmp_path: Path, request: pytest.FixtureRequest) -> tuple[Path, Path]:
    """Create linked checkouts with a gitignored archive present only in the main tree."""
    main = tmp_path / "main checkout"
    main.mkdir()
    if request.param is None:
        git(main, "init", "-q")
    else:
        metadata = tmp_path / "external git" / request.param
        metadata.parent.mkdir()
        git(main, "init", "-q", "--separate-git-dir", str(metadata))
        git(main, "config", "core.worktree", str(main))
    git(main, "config", "user.name", "Fixture")
    git(main, "config", "user.email", "fixture@example.invalid")
    source(main, ".gitignore", "/.harness/\n/docs/tasks/\n")
    source(main, "CONTEXT.md", "# Main glossary\ntransaction")
    git(main, "add", ".gitignore", "CONTEXT.md")
    git(main, "commit", "-qm", "fixture")
    linked = tmp_path / "linked checkout"
    git(main, "worktree", "add", "-qb", "fixture-linked", str(linked))
    configure(main, allow_paths=["CONTEXT.md", "docs/tasks/**/*.md"])
    source(main, "docs/tasks/issue-1/archive.md", "# Archived decision\ntransaction")
    configure(linked, allow_paths=[])
    source(linked, "CONTEXT.md", "# Branch glossary\nunrelated")
    return main, linked


def test_worktree_cli_search_uses_main_index_and_gitignored_corpus(
    checkouts: tuple[Path, Path],
) -> None:
    """Search uses main policy and archived bytes without creating or changing cache files."""
    main, linked = checkouts
    assert run_memory(linked, "search", "transaction")["status"] == "index_missing"
    assert not (linked / ".harness/.sandboxes").exists()
    assert run_memory(main, "rebuild") == {"status": "rebuilt", "indexed": 2}
    cache = main / ".harness/.sandboxes/cache/memory"
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in cache.iterdir()}
    expected = run_memory(main, "search", "transaction")
    pointers = expected["pointers"]
    assert isinstance(pointers, list)
    assert {p["path"] for p in pointers} == {
        "CONTEXT.md",
        "docs/tasks/issue-1/archive.md",
    }
    assert run_memory(linked, "search", "transaction") == expected
    assert not (linked / "docs/tasks").exists()
    assert not (linked / ".harness/.sandboxes").exists()
    assert before == {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in cache.iterdir()
    }


@pytest.mark.parametrize("operation", ["build", "rebuild"])
def test_worktree_cli_cannot_publish_or_create_an_index(
    checkouts: tuple[Path, Path],
    operation: str,
) -> None:
    """Both writers reject linked checkouts before touching even a missing cache."""
    main, linked = checkouts
    for existing in (False, True):
        if existing:
            run_memory(main, "rebuild")
        cache = main / ".harness/.sandboxes/cache/memory"
        before = {p.name: p.read_bytes() for p in cache.iterdir()} if existing else {}
        process = subprocess.run(
            command(linked, operation), capture_output=True, text=True
        )
        assert process.returncode != 0
        assert "memory writers require main checkout" in process.stderr
        assert not (linked / ".harness/.sandboxes").exists()
        if existing:
            assert before == {p.name: p.read_bytes() for p in cache.iterdir()}
        else:
            assert not cache.exists()


@pytest.mark.parametrize(
    "operations", [("build", "build"), ("rebuild", "rebuild"), ("build", "rebuild")]
)
def test_parallel_cli_writers_publish_a_complete_searchable_corpus(
    checkouts: tuple[Path, Path],
    operations: tuple[str, str],
) -> None:
    """Real concurrent processes preserve all pointers with either publication strategy."""
    main, linked = checkouts
    configure(
        main,
        allow_paths=["CONTEXT.md", "docs/tasks/**/*.md"],
        top_k=128,
        max_tokens=50000,
    )
    for number in range(126):
        source(
            main, f"docs/tasks/archive/{number}.md", f"# Decision {number}\ntransaction"
        )
    with (
        subprocess.Popen(
            command(main, operations[0]),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as first,
        subprocess.Popen(
            command(main, operations[1]),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as second,
    ):
        for process in (first, second):
            stdout, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, stderr
            assert json.loads(stdout)["indexed"] == 128
    result = run_memory(linked, "search", "transaction")
    assert result["status"] == "ok"
    pointers = result["pointers"]
    assert isinstance(pointers, list) and len(pointers) == 128
    assert not (linked / ".harness/.sandboxes").exists()


def test_busy_cli_writers_fail_without_damaging_the_shared_index(
    checkouts: tuple[Path, Path],
) -> None:
    """A held SQLite boundary lock makes concurrent writers fail with retry guidance."""
    main, linked = checkouts
    run_memory(main, "rebuild")
    expected = run_memory(linked, "search", "transaction")
    cache = main / ".harness/.sandboxes/cache/memory"
    connection = sqlite3.connect(cache / "writer.sqlite3")
    try:
        connection.execute("BEGIN IMMEDIATE")
        with (
            subprocess.Popen(
                command(main, "build"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ) as first,
            subprocess.Popen(
                command(main, "rebuild"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ) as second,
        ):
            for process in (first, second):
                _, stderr = process.communicate(timeout=10)
                assert process.returncode != 0
                assert "memory writer busy" in stderr
        assert run_memory(linked, "search", "transaction") == expected
    finally:
        connection.rollback()
        connection.close()
    assert run_memory(main, "rebuild")["indexed"] == 2


def test_unknown_main_checkout_never_creates_a_worktree_index(
    tmp_path: Path,
) -> None:
    """Missing separate-Git-dir metadata cannot silently enable branch-local writers."""
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q", "--separate-git-dir", str(tmp_path / "metadata"))
    git(main, "config", "user.name", "Fixture")
    git(main, "config", "user.email", "fixture@example.invalid")
    git(main, "commit", "--allow-empty", "-qm", "fixture")
    linked = tmp_path / "linked"
    git(main, "worktree", "add", "-qb", "fixture-linked", str(linked))
    configure(linked)
    for operation in ("build", "rebuild"):
        process = subprocess.run(
            command(linked, operation), capture_output=True, text=True
        )
        assert process.returncode != 0
        assert "cannot locate main checkout" in process.stderr
    assert not (linked / ".harness/.sandboxes").exists()


def test_worktree_writers_fail_closed_when_git_is_unavailable(
    checkouts: tuple[Path, Path],
) -> None:
    """Losing Git must not turn a linked checkout into a new cache owner."""
    _, linked = checkouts
    configure(linked)
    for operation in ("build", "rebuild"):
        process = subprocess.run(
            command(linked, operation),
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": ""},
        )
        assert process.returncode != 0
        assert "cannot locate main checkout" in process.stderr
    assert not (linked / ".harness/.sandboxes").exists()


@pytest.mark.parametrize("target", ["empty", "missing", "foreign", "linked"])
def test_memory_rejects_core_worktree_without_a_main_checkout_binding(
    checkouts: tuple[Path, Path],
    target: str,
) -> None:
    """Shared metadata cannot authorize a missing, foreign or linked cache owner."""
    main, linked = checkouts
    run_memory(main, "rebuild")
    configure(linked)
    if target == "empty":
        configured = ""
    elif target == "missing":
        configured = str(main.parent / "missing")
    elif target == "linked":
        configured = str(linked)
    else:
        foreign = main.parent / "foreign"
        foreign.mkdir()
        git(foreign, "init", "-q")
        configure(foreign)
        source(foreign, "CONTEXT.md", "# Foreign decision\ntransaction")
        run_memory(foreign, "rebuild")
        configured = str(foreign)
    git(linked, "config", "core.worktree", configured)
    assert run_memory(linked, "search", "transaction")["status"] == "invalid_policy"
    for operation in ("build", "rebuild"):
        process = subprocess.run(
            command(linked, operation), capture_output=True, text=True
        )
        assert process.returncode != 0
        assert "cannot locate main checkout" in process.stderr
    assert not (linked / ".harness/.sandboxes").exists()


def test_memory_writers_do_not_create_a_second_index_in_a_repository_subdirectory(
    checkouts: tuple[Path, Path],
) -> None:
    """A repo argument inside a checkout must not become a second memory owner."""
    main, _ = checkouts
    nested = main / "nested"
    nested.mkdir()
    configure(nested)
    source(nested, "CONTEXT.md", "# Nested decision\ntransaction")
    for operation in ("build", "rebuild"):
        process = subprocess.run(
            command(nested, operation), capture_output=True, text=True
        )
        assert process.returncode != 0
        assert "cannot locate main checkout" in process.stderr
    assert not (nested / ".harness/.sandboxes").exists()
