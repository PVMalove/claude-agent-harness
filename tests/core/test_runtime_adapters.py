"""Managed adapter delivery preserves project-owned paths at the CLI boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.core.test_uninstall import git, harness


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-b", "main")
    return root


@pytest.mark.parametrize("command", ["init", "adopt"])
def test_install_preserves_unknown_adapter(repo: Path, command: str) -> None:
    path = repo / ".harness/adapters/claude_worker.py"
    path.parent.mkdir(parents=True)
    path.write_text("project adapter\n", encoding="utf-8")
    result = harness(command, str(repo), "--capability", "backend-orchestration")
    assert result.returncode != 0
    assert "adapter" in result.stderr
    assert path.read_text() == "project adapter\n"
    assert not (repo / ".harness/harness.lock").exists()


def install(repo: Path) -> None:
    result = harness("init", str(repo), "--capability", "backend-orchestration")
    assert result.returncode == 0, result.stderr


def test_update_refuses_local_adapter_unless_explicit_force(repo: Path) -> None:
    install(repo)
    path = repo / ".harness/adapters/claude_worker.py"
    original = path.read_bytes()
    path.write_text("local edit\n")
    result = harness("update", str(repo))
    assert result.returncode != 0
    assert path.read_text() == "local edit\n"
    result = harness("update", str(repo), "--force-managed-files")
    assert result.returncode == 0, result.stderr
    assert path.read_bytes() == original


@pytest.mark.parametrize("command", ["init", "adopt", "update"])
def test_lifecycle_preserves_symlink_adapter(repo: Path, command: str) -> None:
    if command == "update":
        install(repo)
    path = repo / ".harness/adapters/claude_worker.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    target = repo / "outside.py"
    target.write_text("project file\n")
    path.symlink_to(target)
    result = harness(
        command,
        str(repo),
        "--capability",
        "backend-orchestration",
        *(["--force-managed-files"] if command == "update" else []),
    )
    assert result.returncode != 0
    assert path.is_symlink()
    assert target.read_text() == "project file\n"


def test_update_refreshes_known_adapter_and_json_diff_shows_drift(repo: Path) -> None:
    install(repo)
    path = repo / ".harness/adapters/claude_worker.py"
    original = path.read_bytes()
    path.write_text("old managed version\n")
    import hashlib

    lock_path = repo / ".harness/harness.lock"
    lock = json.loads(lock_path.read_text())
    lock["files"][".harness/adapters/claude_worker.py"] = hashlib.sha256(
        path.read_bytes()
    ).hexdigest()
    lock_path.write_text(json.dumps(lock))
    diff = harness("diff", str(repo), "--json")
    assert (
        ".harness/adapters/claude_worker.py"
        in json.loads(diff.stdout)["package_changed"]
    )
    result = harness("update", str(repo))
    assert result.returncode == 0, result.stderr
    assert path.read_bytes() == original


def test_capability_removal_preserves_modified_and_unknown_adapters(repo: Path) -> None:
    install(repo)
    path = repo / ".harness/adapters/claude_worker.py"
    path.write_text("local edit\n")
    unknown = path.with_name("local.py")
    unknown.write_text("unknown\n")
    result = harness(
        "update", str(repo), "--capability", "pvmalove-suite", "--force-managed-files"
    )
    assert result.returncode == 0, result.stderr
    assert path.read_text() == "local edit\n"
    assert unknown.read_text() == "unknown\n"


def test_uninstall_preserves_local_and_unknown_adapters(repo: Path) -> None:
    from harness.uninstall import apply_uninstall, plan_uninstall

    install(repo)
    path = repo / ".harness/adapters/claude_worker.py"
    path.write_text("local edit\n")
    unknown = path.with_name("local.py")
    unknown.write_text("unknown\n")
    result = apply_uninstall(repo, plan_uninstall(repo), confirm="UNINSTALL")
    assert result["failed"] == []
    assert path.read_text() == "local edit\n"
    assert unknown.read_text() == "unknown\n"


@pytest.mark.parametrize("mode", ["soft", "hard"])
def test_cleanup_keeps_installed_and_project_adapters(repo: Path, mode: str) -> None:
    from harness.cleanup import apply_cleanup, plan_cleanup

    install(repo)
    folder = repo / ".harness/adapters"
    modified = folder / "claude_worker.py"
    modified.write_text("local edit\n")
    unknown = folder / "local.py"
    unknown.write_text("unknown\n")
    target = repo / "target.py"
    target.write_text("target\n")
    link = folder / "linked.py"
    link.symlink_to(target)
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    result = apply_cleanup(
        repo,
        plan_cleanup(repo, mode, min_age_hours=0),
        confirm="HARD" if mode == "hard" else None,
    )
    assert result["failed"] == []
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before
    assert link.is_symlink()


def test_uninstall_preserves_symlink_adapters_without_touching_target(
    repo: Path,
) -> None:
    from harness.uninstall import apply_uninstall, plan_uninstall

    install(repo)
    path = repo / ".harness/adapters/claude_worker.py"
    path.unlink()
    target = repo / "target.py"
    target.write_text("target\n")
    path.symlink_to(target)
    result = apply_uninstall(repo, plan_uninstall(repo), confirm="UNINSTALL")
    assert result["failed"] == []
    assert path.is_symlink()
    assert target.read_text() == "target\n"


def test_uninstall_preserves_linked_adapter_directory(repo: Path) -> None:
    from harness.uninstall import apply_uninstall, plan_uninstall
    import shutil

    install(repo)
    folder = repo / ".harness/adapters"
    shutil.rmtree(folder)
    target = repo / "own-adapters"
    target.mkdir()
    (target / "local.py").write_text("project\n")
    folder.symlink_to(target, target_is_directory=True)
    result = apply_uninstall(repo, plan_uninstall(repo), confirm="UNINSTALL")
    assert result["failed"] == []
    assert folder.is_symlink()
    assert (target / "local.py").read_text() == "project\n"
