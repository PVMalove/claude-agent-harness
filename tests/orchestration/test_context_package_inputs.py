"""Explicit read inputs for an architect whose implementation files do not exist yet."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Iterator

import pytest

from harness.orchestration.core.utils import JsonObject
from tests.orchestration import test_recovery as recovery_support
from tests.orchestration.test_coordinator import ORCHESTRATION_ROOT, _git

Planning = tuple[recovery_support.RecoveryTests, JsonObject]


@pytest.fixture
def planning() -> Iterator[Planning]:
    fixture = recovery_support.RecoveryTests()
    fixture.setUp()
    try:
        document = fixture.repo / "docs/adr/0030-server.md"
        document.parent.mkdir(parents=True)
        document.write_text("# Server contract\nLoopback only.\n")
        _git(fixture.repo, "add", "docs/adr/0030-server.md")
        _git(fixture.repo, "commit", "-m", "docs: server contract")
        _git(fixture.repo, "push", "origin", "master")
        batch = fixture._create_batch()
        yield fixture, batch
    finally:
        fixture.tearDown()


def test_explicit_context_inputs_replace_an_incomplete_package_before_coding(planning: Planning) -> None:
    fixture, batch = planning
    original = fixture._propose(batch["batch_id"], "architect")
    snapshot = batch["base_commit"]
    command = [
        sys.executable, str(ORCHESTRATION_ROOT / "coordinator.py"),
        "--repo", str(fixture.repo), "context-package", "register",
        "--batch", batch["batch_id"], "--candidate-commit", snapshot,
        "--min-starting-files", "1", "--starting-file", "docs/adr/0030-server.md",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    package = json.loads(result.stdout)
    paths = [entry["path"] for entry in package["starting_files"]]
    assert "docs/adr/0030-server.md" in paths
    expected = hashlib.sha256((fixture.repo / "docs/adr/0030-server.md").read_bytes()).hexdigest()
    assert package["file_hashes"]["docs/adr/0030-server.md"] == expected
    assert package["context_package_id"] != original["transition"]["context_package_id"]
    reused = fixture._propose(batch["batch_id"], "architect")
    assert reused["transition"]["context_package_id"] == package["context_package_id"]
    repeated = subprocess.run(command, capture_output=True, text=True, check=False)
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout)["context_package_id"] == package["context_package_id"]


@pytest.mark.parametrize("path", ["../outside.md", "/outside.md", "docs/missing.md"])
def test_explicit_context_input_must_exist_in_the_pinned_snapshot(planning: Planning, path: str) -> None:
    fixture, batch = planning
    result = subprocess.run(
        [sys.executable, str(ORCHESTRATION_ROOT / "coordinator.py"), "--repo", str(fixture.repo),
         "context-package", "register", "--batch", batch["batch_id"],
         "--candidate-commit", batch["base_commit"], "--min-starting-files", "1",
         "--starting-file", path],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    assert "starting file" in result.stderr
    assert not fixture._batch_record(batch["batch_id"]).get("context_packages")
