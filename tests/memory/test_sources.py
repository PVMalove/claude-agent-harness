"""Reserved tracker snapshot sources honor selector membership."""

import sqlite3
from pathlib import Path

import pytest

from harness.memory import build
from .test_build import configure, source

from harness.memory.sources import source_type


def test_snapshot_namespace_has_no_generic_markdown_fallback(tmp_path: Path) -> None:
    assert source_type(".harness/.sandboxes/memory/snapshot/private.md") == ""
    assert (
        source_type(
            ".harness/.sandboxes/memory/snapshot/records/ticket-1-" + "a" * 64 + ".json"
        )
        == "task_archive"
    )
    assert (
        source_type(
            ".harness/.sandboxes/memory/snapshot/records/completion_report-1-"
            + "a" * 64
            + ".json"
        )
        == "completion_report"
    )


def test_local_markdown_keeps_baseline_opt_in_and_redacts_metadata(tmp_path: Path) -> None:
    configure(tmp_path, source_types=["adr"], allow_paths=["docs/adr/*.md"],
              redact_rules=["customprivate"])
    source(tmp_path, "docs/adr/0001.md", "# title customprivate\n"
           "Status: accepted customprivate\nDate: customprivate\n"
           "superseded_by: customprivate\n"
           "body customprivate token=localvalue https://example.invalid/reference /tmp/example.log\n")
    assert build(tmp_path)["indexed"] == 1
    with sqlite3.connect(tmp_path / ".harness/.sandboxes/cache/memory/index.sqlite3") as db:
        retained = "\n".join(db.iterdump())
        assert db.execute("SELECT title, status, date, superseded_by FROM documents").fetchone() == (
            "title [REDACTED]", "accepted [REDACTED]", "[REDACTED]", "[REDACTED]",
        )
    assert "customprivate" not in retained
    for literal in ("token=localvalue", "https://example.invalid/reference", "/tmp/example.log"):
        assert literal in retained


@pytest.mark.parametrize("prefix", ["glpat-", "github_pat_"], ids=["gitlab_pat", "github_fine_grained_pat"])
def test_source_paths_with_tracker_tokens_are_rejected(tmp_path: Path, prefix: str) -> None:
    configure(tmp_path, source_types=["adr"], allow_paths=["docs/adr/*.md"], redact_rules=[])
    relative = "docs/adr/" + prefix + "SyntheticPathFixture0123456789" + ".md"
    source(tmp_path, relative, "# Safe body")
    with pytest.raises(ValueError, match="path cannot be safely retained"):
        build(tmp_path)
