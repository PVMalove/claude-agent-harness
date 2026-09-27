"""The flat, explicit registry of health checks that `harness health` runs.

No plugin auto-discovery: checks are wired in by explicit import, one entry per check function, so
the registry stays auditable. Future check groups (#343-#347) add entries here without touching how
already-registered checks run.

Each entry declares the check's stable id up front, so a check that crashes is still reported under
that id (as `fail`) instead of aborting the rest of the report.

FIXERS maps a check id to its fix action, run only under `harness health --fix` (ticket #345). A fix
action is limited to local `.harness` scaffolding - creating a directory, regenerating the skill
registry. System settings (Windows registry, Developer Mode, global git config) and worktrees are
never touched: those checks only carry a `Fix` with the command to run by hand.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .checks import directories as directory_checks
from .checks import environment as environment_checks
from .checks import files as files_checks
from .checks import repo_map as repo_map_checks
from .checks import windows as windows_checks
from .context import HealthContext
from .model import CheckResult, JsonObject, Report

CheckFn = Callable[[HealthContext], CheckResult]
# Applies the fix for one check result; returns a human description of what it did, or None.
FixFn = Callable[[HealthContext, CheckResult], str | None]

REGISTRY: list[tuple[str, CheckFn]] = [
    ("files.lock", files_checks.check_lock),
    ("files.agents_md", files_checks.check_agents_md),
    ("files.discovery_links", files_checks.check_discovery_links),
    ("files.project_json", files_checks.check_project_json),
    ("files.sandboxes", files_checks.check_sandboxes),
    ("files.orchestration_config", files_checks.check_orchestration_config),
    ("files.skill_snapshot", files_checks.check_skill_snapshot),
    ("files.skill_registry", files_checks.check_skill_registry),
    ("files.overlay_locks", files_checks.check_overlay_locks),
    ("files.integrations", files_checks.check_integrations),
    ("files.verification_routing", files_checks.check_verification_routing),
    *(
        (check_id, directory_checks.make_check(check_id))
        for check_id in directory_checks.DIRECTORY_PATHS
    ),
    ("repo_map.tier", repo_map_checks.check_tier),
    ("environment.git", environment_checks.check_git),
    ("environment.git_identity", environment_checks.check_git_identity),
    ("environment.gitattributes", environment_checks.check_gitattributes),
    ("environment.line_endings", environment_checks.check_line_endings),
    ("environment.python", environment_checks.check_python),
    ("environment.uv", environment_checks.check_uv),
    ("environment.dev_env", environment_checks.check_dev_environment),
    ("environment.output_encoding", environment_checks.check_output_encoding),
    ("environment.long_paths", windows_checks.check_long_paths),
    ("environment.path_length", windows_checks.check_path_length),
    ("environment.pytest_temp", windows_checks.check_pytest_temp),
    ("environment.symlinks", windows_checks.check_symlinks),
    ("environment.hook_bash", windows_checks.check_hook_bash),
]

FIXERS: dict[str, FixFn] = {
    "files.skill_registry": files_checks.fix_skill_registry,
    **{
        check_id: directory_checks.make_fix(check_id)
        for check_id in directory_checks.DIRECTORY_PATHS
    },
}

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
    fix: bool = False,
) -> Report:
    """Build one HealthContext and run every registered check, without early exit.

    With `fix`, a check that has an entry in FIXERS gets its fix action applied right after it
    runs; whatever the action did is appended to `report.fixes_applied` and the check is re-run so
    the report shows the state after the fix.

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
    for check_id, check_fn in REGISTRY:
        result = _run_isolated(check_id, check_fn, context)
        fixer = FIXERS.get(check_id) if fix else None
        if fixer is not None:
            applied = _apply_isolated(check_id, fixer, context, result)
            if applied is not None:
                report.fixes_applied.append(applied)
                result = _run_isolated(check_id, check_fn, context)
        report.checks.append(result)
    return report


def _apply_isolated(
    check_id: str, fixer: FixFn, context: HealthContext, result: CheckResult
) -> str | None:
    """Run one fix action; a crash leaves the check's own result in place instead of ending the run."""
    try:
        return fixer(context, result)
    except (Exception, SystemExit):
        return None


def _run_isolated(check_id: str, check_fn: CheckFn, context: HealthContext) -> CheckResult:
    """Run one check; a crash becomes its `fail` result so the remaining checks still run.

    SystemExit is caught too: the helpers health shares with the packager (files._fail) exit the
    process on error, which must not end a health run. KeyboardInterrupt still propagates.
    """
    try:
        return check_fn(context)
    except (Exception, SystemExit) as exc:
        return CheckResult(
            id=check_id,
            group=check_id.partition(".")[0],
            status="fail",
            message=f"проверка {check_fn.__name__} аварийно завершилась: "
            f"{type(exc).__name__}: {exc}",
        )
