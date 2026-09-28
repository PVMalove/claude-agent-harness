"""`harness health` directory checks and the local `--fix` (ticket #345).

The process tests run the real CLI against a freshly initialized project and assert only external
behavior: the `--json` report, the exit code, and the files on disk after `--fix`."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from harness.health.checks import directories
from harness.health.context import HealthContext

_HARNESS = Path(__file__).resolve().parents[1] / "harness" / "bin" / "harness.py"
_DIRECTORY_IDS = {
    "directories.harness",
    "directories.sandboxes",
    "directories.cache",
    "directories.logs",
    "directories.scratch",
    "directories.runs",
    "directories.reports",
    "directories.worktrees",
    "directories.orchestration_state",
}


def _context(repo: Path, lock: dict[str, object] | None = None) -> HealthContext:
    return HealthContext(repo=repo, lock=lock, online=False)


def _check(check_id: str, repo: Path, lock: dict[str, object] | None = None) -> dict[str, str]:
    result = directories.make_check(check_id)(_context(repo, lock))
    return {"status": result.status, "message": result.message}


# --- Unit: classification ---------------------------------------------------------------------


def test_missing_directory_with_writable_parent_is_ok_and_will_be_created(
    tmp_path: Path,
) -> None:
    result = _check("directories.cache", tmp_path)

    assert result["status"] == "ok"
    assert "будет создан" in result["message"]
    assert not (tmp_path / ".harness").exists()  # the check itself creates nothing


def test_file_in_place_of_directory_fails(tmp_path: Path) -> None:
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness" / ".sandboxes").write_text("", encoding="utf-8")

    assert _check("directories.sandboxes", tmp_path)["status"] == "fail"


def test_existing_unwritable_directory_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logs = tmp_path / ".harness" / ".sandboxes" / "logs"
    logs.mkdir(parents=True)
    real_access = os.access
    monkeypatch.setattr(
        os,
        "access",
        lambda path, mode: False if Path(path) == logs else real_access(path, mode),
    )

    result = _check("directories.logs", tmp_path)

    assert result["status"] == "fail"
    assert ".harness/.sandboxes/logs" in result["message"]


def test_missing_directory_under_unwritable_ancestor_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = tmp_path / ".harness"
    harness.mkdir()
    real_access = os.access
    monkeypatch.setattr(
        os,
        "access",
        lambda path, mode: False if Path(path) == harness else real_access(path, mode),
    )

    assert _check("directories.runs", tmp_path)["status"] == "fail"


def test_orchestration_state_depends_on_the_capability(tmp_path: Path) -> None:
    assert _check("directories.orchestration_state", tmp_path)["status"] == "skipped"
    lock: dict[str, object] = {"capabilities": ["backend-orchestration"]}
    assert _check("directories.orchestration_state", tmp_path, lock)["status"] == "ok"


# --- Process: `harness health --json [--fix]` -------------------------------------------------


def _environment(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    env.pop("GIT_CONFIG_GLOBAL", None)
    return env


def _run(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_HARNESS), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )


def _project(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    home = tmp_path / "home"
    home.mkdir()
    env = _environment(home)
    repo = tmp_path / "project"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    init = _run("init", str(repo), "--capability", "pvmalove-suite", env=env)
    assert init.returncode == 0, init.stderr
    return repo, env


def _health(repo: Path, env: dict[str, str], *flags: str) -> dict[str, object]:
    completed = _run("health", str(repo), "--json", *flags, env=env)
    assert completed.stdout, completed.stderr
    data: dict[str, object] = json.loads(completed.stdout)
    return data


def _statuses(report: dict[str, object]) -> dict[str, str]:
    checks = report["checks"]
    assert isinstance(checks, list)
    return {check["id"]: check["status"] for check in checks}


def test_health_reports_every_directory_check_by_stable_id(tmp_path: Path) -> None:
    repo, env = _project(tmp_path)
    shutil.rmtree(repo / ".harness" / ".sandboxes")

    report = _health(repo, env)

    statuses = _statuses(report)
    assert _DIRECTORY_IDS <= set(statuses)
    assert statuses["directories.sandboxes"] == "ok"
    assert statuses["directories.worktrees"] == "ok"
    assert report["fixes_applied"] == []
    assert not (repo / ".harness" / ".sandboxes").exists()  # without --fix nothing is created


def test_fix_creates_missing_directories_and_rebuilds_the_registry(tmp_path: Path) -> None:
    repo, env = _project(tmp_path)
    shutil.rmtree(repo / ".harness" / ".sandboxes")
    registry = repo / ".harness" / "skills" / "REGISTRY.md"
    expected_registry = registry.read_text(encoding="utf-8")
    registry.write_text("# stale\n", encoding="utf-8")
    assert _statuses(_health(repo, env))["files.skill_registry"] == "fail"

    report = _health(repo, env, "--fix")

    sandboxes = repo / ".harness" / ".sandboxes"
    for category in directories.SANDBOX_CATEGORY_ORDER:
        assert (sandboxes / category).is_dir()
    assert registry.read_text(encoding="utf-8") == expected_registry
    fixes = report["fixes_applied"]
    assert isinstance(fixes, list)
    assert "пересобран .harness/skills/REGISTRY.md" in fixes
    assert "создан каталог .harness/.sandboxes" in fixes
    assert "создан каталог .harness/.sandboxes/worktrees" in fixes
    statuses = _statuses(report)
    assert statuses["files.skill_registry"] == "ok"
    assert statuses["directories.sandboxes"] == "ok"

    assert _health(repo, env, "--fix")["fixes_applied"] == []  # idempotent


def test_fix_never_changes_system_settings_or_worktrees(tmp_path: Path) -> None:
    repo, env = _project(tmp_path)
    home = Path(env["HOME"])
    orphan = repo / ".harness" / ".sandboxes" / "worktrees" / "orphan"
    orphan.mkdir(parents=True)
    (orphan / "keep.txt").write_text("keep", encoding="utf-8")
    home_before = sorted(path.relative_to(home) for path in home.rglob("*"))

    report = _health(repo, env, "--fix")

    # No git identity in the isolated HOME: health prints the `git config` command, never runs it.
    checks = report["checks"]
    assert isinstance(checks, list)
    identity = next(check for check in checks if check["id"] == "environment.git_identity")
    assert identity["status"] == "warn"
    assert identity["fix"]["command"]
    assert sorted(path.relative_to(home) for path in home.rglob("*")) == home_before
    assert (orphan / "keep.txt").read_text(encoding="utf-8") == "keep"
    fixes = report["fixes_applied"]
    assert isinstance(fixes, list)
    assert all(
        fix.startswith(("создан каталог .harness/", "пересобран .harness/")) for fix in fixes
    )


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX permission bits are not enforced for root or on Windows",
)
def test_existing_unwritable_directory_fails_the_process(tmp_path: Path) -> None:
    repo, env = _project(tmp_path)
    logs = repo / ".harness" / ".sandboxes" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    logs.chmod(0o500)
    try:
        completed = _run("health", str(repo), "--json", "--fix", env=env)
    finally:
        logs.chmod(0o700)

    report = json.loads(completed.stdout)
    assert _statuses(report)["directories.logs"] == "fail"
    assert completed.returncode == 1
