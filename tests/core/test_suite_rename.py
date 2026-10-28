"""Existing suite installations remain usable after the public capability rename."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.bin.harness import capabilities, selected_capabilities
from harness.console import brand
from harness.console.project_target import inspect_target
from tests.core.test_uninstall import git, harness


def test_catalog_lists_only_neutral_suite_and_cli_accepts_legacy_name() -> None:
    assert "engineering-suite" in capabilities()
    assert "pvmalove-suite" not in capabilities()
    assert selected_capabilities(["pvmalove-suite", "engineering-suite"]) == [
        "engineering-suite"
    ]


@pytest.mark.parametrize("suite", ["engineering-suite", "pvmalove-suite"])
def test_install_and_update_legacy_snapshot(tmp_path: Path, suite: str) -> None:
    git(tmp_path, "init", "-b", "main")
    result = harness("init", str(tmp_path), "--capability", suite)
    assert result.returncode == 0, result.stderr
    lock_path = tmp_path / ".harness/harness.lock"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    assert lock["capabilities"] == ["engineering-suite"]
    lock["capabilities"] = ["pvmalove-suite"]
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    project_path = tmp_path / ".harness/project.json"
    project = json.loads(project_path.read_text(encoding="utf-8"))
    project["base_branch"] = "release"
    project_path.write_text(json.dumps(project), encoding="utf-8")
    before = project_path.read_bytes()

    assert inspect_target(str(tmp_path)).capabilities == ("engineering-suite",)
    assert brand.installed_capabilities(tmp_path) == ("engineering-suite",)
    diff = harness("diff", str(tmp_path), "--json")
    assert diff.returncode == 0, diff.stderr
    assert json.loads(diff.stdout)["capabilities"] == ["engineering-suite"]
    health = harness("health", str(tmp_path), "--json")
    checks = json.loads(health.stdout)["checks"]
    policy = next(check for check in checks if check["id"] == "hooks.policy")
    assert policy["status"] != "skipped"

    result = harness("update", str(tmp_path))
    assert result.returncode == 0, result.stderr
    updated_lock = json.loads(lock_path.read_text(encoding="utf-8"))
    assert updated_lock["capabilities"] == ["engineering-suite"]
    assert project_path.read_bytes() == before
