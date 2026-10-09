"""Frozen memory provenance is checked against authoritative source bytes."""

from pathlib import Path
import subprocess
import sqlite3

import pytest

from harness.memory import build
from harness.orchestration.ledger.lifecycle import LifecycleLedger
from harness.orchestration.workflow.context_package import _persist_context_package
from harness.orchestration.workflow.history import _context_package_freshness
from .test_build import configure, git, source


@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize(
    "change", ["edit", "delete", "symlink", "revoke", "index_only"]
)
def test_frozen_memory_source_freshness(
    tmp_path: Path, change: str, linked: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Index writes alone cannot invalidate an immutable source pointer."""
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Fixture")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    configure(tmp_path, min_similarity=0)
    file = source(tmp_path, "CONTEXT.md", "# Memory\ntransaction")
    git(tmp_path, "add", "CONTEXT.md")
    git(tmp_path, "commit", "-qm", "fixture")
    snapshot = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip()
    build(tmp_path)
    working = tmp_path
    if linked:
        working = tmp_path / "linked"
        git(tmp_path, "worktree", "add", "-qb", "linked", str(working))
        configure(working, min_similarity=0)
        source(working, "CONTEXT.md", "# Branch-local content")
    root = tmp_path / "state"
    ledger = LifecycleLedger(root)
    ledger.ensure()
    batch = {
        "batch_id": "batch-memory",
        "base_commit": snapshot,
        "goal": "transaction",
        "definition_of_done": [],
        "context_packages": [],
    }
    package = _persist_context_package(
        working,
        root,
        ledger,
        batch,
        role="shared",
        snapshot=snapshot,
        inclusion_reason="test",
    )
    assert package["memory"]["pointers"]
    if change == "edit":
        file.write_text("# Changed", encoding="utf-8")
    elif change in {"delete", "symlink"}:
        file.unlink()
        if change == "symlink":
            file.symlink_to(source(tmp_path, "other.md", "# Memory\ntransaction"))
    elif change == "revoke":
        configure(tmp_path, min_similarity=0, allow_paths=[])
    else:
        build(tmp_path)

    def forbid_index(*args: object, **kwargs: object) -> None:
        raise AssertionError("freshness must never query the derived index")

    monkeypatch.setattr(sqlite3, "connect", forbid_index)
    result = _context_package_freshness(working, root, batch)
    assert result is not None
    assert result["status"] == ("fresh" if change == "index_only" else "stale")
    if change != "index_only":
        assert (
            result["memory_diagnostic"]
            == "frozen memory source changed, unavailable or revoked"
        )
        pointer = package["memory"]["pointers"][0]
        mismatch = result["memory_mismatch"]
        assert mismatch["actual_source_hash"] != pointer["source_hash"]
        if change == "symlink":
            # A symlinked source breaks the allowlist walk itself, before any pointer is read.
            assert mismatch["path"] is None
        else:
            assert mismatch["path"] == pointer["path"]
            assert mismatch["expected_source_hash"] == pointer["source_hash"]
    else:
        assert "memory_mismatch" not in result


@pytest.mark.parametrize(
    "change", ["none", "bytes", "eligibility", "generation", "type"]
)
def test_completion_pointer_uses_projected_type_and_source_eligibility(
    tmp_path: Path, change: str
) -> None:
    """Reports remain fresh only while their selected projection is still authorized."""
    import json

    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Fixture")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    configure(
        tmp_path,
        min_similarity=0,
        source_types=["completion_report"],
        allow_paths=[".harness/orchestration/state/generations/**/*.json"],
    )
    source(tmp_path, "README.md", "# Fixture")
    git(tmp_path, "add", "README.md")
    git(tmp_path, "commit", "-qm", "fixture")
    snapshot = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip()
    prefix = ".harness/orchestration/state"
    source(
        tmp_path,
        prefix + "/ledger.json",
        json.dumps(
            {"version": 3, "generation": "generation-one", "selected_at": "fixture"}
        ),
    )
    report = source(
        tmp_path,
        prefix + "/generations/generation-one/reports/report.json",
        json.dumps(
            {
                "role": "developer",
                "ticket": "#1",
                "outcome": "completed",
                "lessons": ["transaction lesson"],
            }
        ),
    )
    build(tmp_path)
    root = tmp_path / "state"
    ledger = LifecycleLedger(root)
    ledger.ensure()
    batch = {
        "batch_id": "batch-memory",
        "base_commit": snapshot,
        "goal": "transaction",
        "definition_of_done": [],
        "context_packages": [],
    }
    package = _persist_context_package(
        tmp_path,
        root,
        ledger,
        batch,
        role="shared",
        snapshot=snapshot,
        inclusion_reason="test",
    )
    assert package["memory"]["pointers"][0]["source_type"] == "completion_report"
    if change in {"bytes", "eligibility"}:
        payload = json.loads(report.read_text())
        if change == "bytes":
            payload["unused"] = "changed"
        else:
            payload.pop("lessons")
        report.write_text(json.dumps(payload))
    elif change == "generation":
        (tmp_path / prefix / "generations/generation-two").mkdir()
        source(
            tmp_path,
            prefix + "/ledger.json",
            json.dumps(
                {"version": 3, "generation": "generation-two", "selected_at": "fixture"}
            ),
        )
    elif change == "type":
        configure(
            tmp_path,
            min_similarity=0,
            source_types=["qa_finding"],
            allow_paths=[prefix + "/generations/**/*.json"],
        )
    result = _context_package_freshness(tmp_path, root, batch)
    assert result is not None
    assert result["status"] == ("fresh" if change == "none" else "stale")
