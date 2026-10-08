"""Edge cases of memory discovery, the writer lock and the tracker subprocess."""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest import mock

import pytest

from harness.errors import INTERNAL_INVARIANT_REMEDY, HarnessError
from harness.memory import build
from harness.memory.index import writer_lock
from harness.memory.policy import Policy
from harness.memory.sources import allowed_paths

from .test_build import configure, source

# `harness.memory.sync` the attribute is the re-exported function; the module needs importlib.
sync = importlib.import_module("harness.memory.sync")


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("*.md", ["CONTEXT.md"]),
        ("**/*.md", ["CONTEXT.md", "docs/adr/0001.md"]),
    ],
)
def test_a_root_level_glob_walks_the_repository_root(
    tmp_path: Path, pattern: str, expected: list[str]
) -> None:
    source(tmp_path, "CONTEXT.md", "# Glossary\n")
    source(tmp_path, "docs/adr/0001.md", "# ADR\n")
    source(tmp_path, "node_modules/pkg/README.md", "# Dependency\n")
    policy = Policy(
        enabled=True, source_types=("adr", "glossary"), allow_paths=(pattern,)
    )

    assert allowed_paths(tmp_path, policy) == expected


def test_a_root_level_glob_builds_the_index(tmp_path: Path) -> None:
    configure(tmp_path, allow_paths=["**/*.md"])
    source(tmp_path, "CONTEXT.md", "# Glossary\n")
    source(tmp_path, "docs/adr/0001.md", "# ADR\n")

    assert build(tmp_path) == {"status": "built", "indexed": 2}


def test_an_unopenable_writer_lock_is_a_memory_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="memory writer busy or lock unavailable"):
        with writer_lock(tmp_path / "missing" / "index.sqlite3"):
            pass


def test_a_tracker_process_without_pipes_is_killed_and_reported(
    tmp_path: Path,
) -> None:
    process = mock.Mock(stdout=None, stderr=None)

    with mock.patch("harness.memory.sync.subprocess.Popen", return_value=process):
        with pytest.raises(HarnessError) as raised:
            sync.fetch_page(["gh", "api", "repos/o/r/issues"], tmp_path)

    assert raised.value.remedy == INTERNAL_INVARIANT_REMEDY
    process.kill.assert_called_once_with()
    process.wait.assert_called_once_with()
