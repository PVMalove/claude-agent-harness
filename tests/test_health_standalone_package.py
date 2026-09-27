"""harness/health/ ships into an installed project's `.harness/health/` the same way
harness/repo_map/repo_map.py ships into `.harness/repo_map/repo_map.py` (ADR 0018's bootstrap
alias) - see harness/health/__init__.py's docstring. This proves the shipped copy still runs with
no `harness/bin/harness` and no canonical `harness/` package reachable, the scenario the harness
console (#348) will run in; it is what would have caught the old harness/health/checks/_cli.py
bridge's FileNotFoundError (ticket #342 code review)."""

from __future__ import annotations

import importlib
import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
# Mirrors exactly what CAPABILITIES.json's pvmalove-suite capability copies into an installed
# project's .harness/ for the "health" and "repo_map" resources (both file and directory entries).
_FILE_RESOURCES = ("__init__.py", "errors.py", "storage.py", "token_estimator.py")
_DIR_RESOURCES = ("repo_map", "health")


def _install_shipped_only_tree(tmp_path: Path) -> Path:
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
    for name in [n for n in sys.modules if n == "harness" or n.startswith("harness.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)


def _load_standalone_health(installed: Path) -> ModuleType:
    """Load `.harness/health/__init__.py` directly by path under the name "harness.health" -
    exactly like a future harness-console runner would, since ".harness" cannot be imported by its
    literal (dotted) directory name. Executing it also runs its own bootstrap, which registers the
    aliased "harness" parent (see harness/health/__init__.py)."""
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
    installed = _install_shipped_only_tree(tmp_path)
    _forget_harness_modules(monkeypatch)

    _load_standalone_health(installed)
    registry = importlib.import_module("harness.health.registry")

    target_repo = tmp_path / "target"
    target_repo.mkdir()
    report = registry.run(target_repo)

    assert {check.group for check in report.checks} == {
        "files",
        "repo_map",
        "environment",
    }
    assert {check.status for check in report.checks} <= {
        "ok",
        "warn",
        "fail",
        "skipped",
    }
    # The one detection function that cannot ship (snapshot_diff needs harness/bin/harness's own
    # CAPABILITIES.json/source tree) degrades to 'skipped' instead of raising FileNotFoundError.
    snapshot = next(
        check for check in report.checks if check.id == "files.skill_snapshot"
    )
    assert snapshot.status == "skipped"


def test_registry_runs_without_crashing_even_with_a_lock_file_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 9 checks that used to go through the removed _cli.py bridge (ticket #342 review: 9 of
    11 failed with FileNotFoundError) all reach a normal CheckResult, not an exception, once a
    harness.lock makes their non-skipped branches run too."""
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
        "repo_map.tier",
        "environment.git",
        "environment.git_identity",
        "environment.gitattributes",
        "environment.line_endings",
        "environment.python",
        "environment.uv",
        "environment.dev_env",
        "environment.output_encoding",
        "environment.long_paths",
        "environment.path_length",
        "environment.pytest_temp",
        "environment.symlinks",
        "environment.hook_bash",
    }
