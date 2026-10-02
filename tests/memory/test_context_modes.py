"""Context memory modes have distinct immutable identities and no refresh."""

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from harness.memory import build
from harness.orchestration.core.utils import JsonObject
from harness.orchestration.ledger.lifecycle import LifecycleLedger
from harness.orchestration.workflow.context_package import _persist_context_package
from .test_build import configure, git, source


def test_bypass_and_disabled_never_open_index_and_do_not_reuse_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Fixture")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    configure(tmp_path, min_similarity=0)
    source(tmp_path, "CONTEXT.md", "# Memory\ntransaction")
    git(tmp_path, "add", "CONTEXT.md")
    git(tmp_path, "commit", "-qm", "fixture")
    snapshot = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip()
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

    def package(no_memory: bool = False) -> JsonObject:
        return _persist_context_package(
            tmp_path,
            root,
            ledger,
            batch,
            role="shared",
            snapshot=snapshot,
            inclusion_reason="test",
            no_memory=no_memory,
        )

    enabled = package()
    assert enabled["memory"]["pointers"]

    def forbid_index(*args: object, **kwargs: object) -> None:
        raise AssertionError("disabled or bypass cannot query the index")

    monkeypatch.setattr(sqlite3, "connect", forbid_index)
    bypass = package(True)
    assert bypass["memory"]["status"] == "bypass"
    assert bypass["memory"]["pointers"] == []
    assert bypass["context_package_id"] != enabled["context_package_id"]
    assert package(True) == bypass
    config_path = tmp_path / ".harness/project.json"
    config = json.loads(config_path.read_text())
    config["memory"]["enabled"] = False
    config_path.write_text(json.dumps(config))
    disabled = package()
    assert disabled["memory"]["status"] == "disabled"
    assert disabled["context_package_id"] != bypass["context_package_id"]


def test_missing_index_is_frozen_without_creation_or_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An index appearing later cannot silently replace the registered evidence."""
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Fixture")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    configure(tmp_path, min_similarity=0)
    source(tmp_path, "CONTEXT.md", "# Memory\ntransaction")
    git(tmp_path, "add", "CONTEXT.md")
    git(tmp_path, "commit", "-qm", "fixture")
    snapshot = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip()
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
    assert package["memory"]["status"] == "index_missing"
    assert package["memory"]["pointers"] == []
    assert not (tmp_path / ".harness/.sandboxes/cache/memory").exists()
    build(tmp_path)

    def forbid_index(*args: object, **kwargs: object) -> None:
        raise AssertionError("reuse must not query or refresh newly available memory")

    monkeypatch.setattr(sqlite3, "connect", forbid_index)
    reused = _persist_context_package(
        tmp_path,
        root,
        ledger,
        batch,
        role="shared",
        snapshot=snapshot,
        inclusion_reason="test",
    )
    assert reused == package
