"""The flat, explicit registry of health checks that `harness health` runs.

No plugin auto-discovery: checks are wired in by explicit import, one entry per check function, so
the registry stays auditable. Future check groups (#343-#347) add entries here without touching how
already-registered checks run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .checks import environment as environment_checks
from .checks import files as files_checks
from .checks import repo_map as repo_map_checks
from .checks import windows as windows_checks
from .context import HealthContext
from .model import CheckResult, JsonObject, Report

CheckFn = Callable[[HealthContext], CheckResult]

REGISTRY: list[CheckFn] = [
    files_checks.check_lock,
    files_checks.check_agents_md,
    files_checks.check_discovery_links,
    files_checks.check_project_json,
    files_checks.check_sandboxes,
    files_checks.check_orchestration_config,
    files_checks.check_skill_snapshot,
    files_checks.check_skill_registry,
    files_checks.check_overlay_locks,
    files_checks.check_integrations,
    files_checks.check_verification_routing,
    repo_map_checks.check_tier,
    environment_checks.check_git,
    environment_checks.check_git_identity,
    environment_checks.check_gitattributes,
    environment_checks.check_line_endings,
    environment_checks.check_python,
    environment_checks.check_uv,
    environment_checks.check_dev_environment,
    environment_checks.check_output_encoding,
    windows_checks.check_long_paths,
    windows_checks.check_path_length,
    windows_checks.check_pytest_temp,
    windows_checks.check_symlinks,
    windows_checks.check_hook_bash,
]

_LOCK_REL = Path(".harness/harness.lock")


def _load_lock(repo: Path) -> JsonObject | None:
    """Parse .harness/harness.lock once per run; a missing file means no lock, not a problem."""
    path = repo / _LOCK_REL
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def run(
    repo: Path,
    *,
    online: bool = False,
    snapshot_diff: Callable[[Path], JsonObject] | None = None,
    output_encoding: str | None = None,
) -> Report:
    """Build one HealthContext and run every registered check, without early exit.

    `snapshot_diff` and `output_encoding` are forwarded to HealthContext unchanged; see its
    docstring - only harness/bin/harness's cmd_health supplies them today.
    """
    context = HealthContext(
        repo=repo,
        lock=_load_lock(repo),
        online=online,
        snapshot_diff=snapshot_diff,
        output_encoding=output_encoding,
    )
    report = Report(schema_version=1, repo=str(repo), online=online)
    for check_fn in REGISTRY:
        report.checks.append(check_fn(context))
    return report
