"""Тесты автономного запуска пакета health без корневого пакета harness."""

from __future__ import annotations

import importlib
import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
# Mirrors exactly what CAPABILITIES.json's pvmalove-suite capability copies into an installed
# project's .harness/ for health and its runtime dependencies (file and directory entries).
_FILE_RESOURCES = ("__init__.py", "errors.py", "storage.py", "token_estimator.py")
_DIR_RESOURCES = ("repo_map", "health", "memory", "gate_runner")


def _install_shipped_only_tree(tmp_path: Path) -> Path:
    """Скопировать в изолированный каталог только поставляемые ресурсы пакета health."""
    installed = tmp_path / "installed" / ".harness"
    installed.mkdir(parents=True)
    for name in _FILE_RESOURCES:
        shutil.copy(_REPO_ROOT / "harness" / name, installed / name)
    for name in _DIR_RESOURCES:
        shutil.copytree(_REPO_ROOT / "harness" / name, installed / name)
    assert not (
        installed.parent / "harness"
    ).exists()  # no canonical harness/ next to it
    return installed


def _forget_harness_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    """Удалить загруженные модули harness из sys.modules для изоляции импорта."""
    for name in [n for n in sys.modules if n == "harness" or n.startswith("harness.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)


def _load_standalone_health(installed: Path) -> ModuleType:
    """Загрузить .harness/health/__init__.py напрямую по пути под именем harness.health."""
    health_dir = installed / "health"
    spec = importlib.util.spec_from_file_location(
        "harness.health",
        health_dir / "__init__.py",
        submodule_search_locations=[str(health_dir)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["harness.health"] = module
    spec.loader.exec_module(module)
    return module


def test_registry_runs_from_a_copied_health_only_tree_with_no_bin_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверить, что реестр проверок запускается из изолированной копии без harness.py."""
    installed = _install_shipped_only_tree(tmp_path)
    _forget_harness_modules(monkeypatch)

    _load_standalone_health(installed)
    registry = importlib.import_module("harness.health.registry")

    target_repo = tmp_path / "target"
    target_repo.mkdir()
    report = registry.run(target_repo)

    assert {check.group for check in report.checks} == {
        "files",
        "directories",
        "repo_map",
        "memory",
        "environment",
        "tracker",
        "orchestration",
    }
    assert {check.status for check in report.checks} <= {
        "ok",
        "warn",
        "fail",
        "skipped",
    }
    # The one detection function that cannot ship (snapshot_diff needs harness/bin/harness.py's own
    # CAPABILITIES.json/source tree) degrades to 'skipped' instead of raising FileNotFoundError.
    snapshot = next(
        check for check in report.checks if check.id == "files.skill_snapshot"
    )
    assert snapshot.status == "skipped"


def test_registry_runs_without_crashing_even_with_a_lock_file_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверить, что реестр проверок выполняется без ошибок при наличии файла harness.lock."""
    import json

    installed = _install_shipped_only_tree(tmp_path)
    _forget_harness_modules(monkeypatch)

    _load_standalone_health(installed)
    registry = importlib.import_module("harness.health.registry")

    target_repo = tmp_path / "target"
    (target_repo / ".harness").mkdir(parents=True)
    (target_repo / ".harness" / "harness.lock").write_text(
        json.dumps({"schema": 1, "capabilities": ["project-foundation"], "files": {}}),
        encoding="utf-8",
    )

    report = registry.run(target_repo)

    ids = {check.id for check in report.checks}
    assert ids == {
        "files.lock",
        "files.agents_md",
        "files.discovery_links",
        "files.project_json",
        "files.sandboxes",
        "files.orchestration_config",
        "files.skill_snapshot",
        "files.skill_registry",
        "files.overlay_locks",
        "files.integrations",
        "files.verification_routing",
        "directories.harness",
        "directories.sandboxes",
        "directories.cache",
        "directories.logs",
        "directories.scratch",
        "directories.pr_body",
        "directories.runs",
        "directories.reports",
        "directories.worktrees",
        "directories.orchestration_state",
        "repo_map.tier",
        "memory.index",
        "memory.model",
        "environment.os",
        "environment.git",
        "environment.git_identity",
        "environment.gitattributes",
        "environment.line_endings",
        "environment.python",
        "environment.uv",
        "environment.glab",
        "environment.dev_env",
        "environment.output_encoding",
        "environment.codex_sandbox",
        "environment.claude_sandbox",
        "environment.long_paths",
        "environment.path_length",
        "environment.pytest_temp",
        "environment.symlinks",
        "environment.hook_bash",
        "tracker.project",
        "tracker.auth",
        "tracker.reachability",
        "tracker.permissions",
        "tracker.labels",
        "tracker.git_base",
        "orchestration.ledger_summary",
        "orchestration.unfinished_batches",
        "orchestration.blocked_batches",
        "orchestration.stale_dispatches",
        "orchestration.orphaned_worktrees",
        "orchestration.disposable_data",
    }
    # The project tracker resolver ships inside .harness/health/ and runs from the copied tree.
    project = next(check for check in report.checks if check.id == "tracker.project")
    assert project.status == "ok", project.message
