"""Плоский явный реестр проверок здоровья, запускаемых командой `harness health`.

Автоматическое обнаружение плагинов не используется: проверки подключаются явным импортом,
по одной записи на функцию проверки, благодаря чему реестр остаётся легко проверяемым.
Новые группы проверок добавляются сюда без изменения логики запуска уже зарегистрированных.

Каждая запись объявляет стабильный идентификатор проверки заранее, поэтому даже при падении
проверки отчёт содержит результат под этим id (со статусом `fail`), не прерывая выполнение
остальных проверок.

Словарь `FIXERS` связывает идентификатор проверки с действием по исправлению, запускаемым только
при флаге `harness health --fix`. Исправления ограничены локальной структурой `.harness` —
созданием каталогов, перегенерацией реестра навыков. Системные настройки (реестр Windows,
режим разработчика, глобальный git config) и рабочие деревья не модифицируются: такие проверки
лишь возвращают объект `Fix` с командой для ручного выполнения.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .checks import directories as directory_checks
from .checks import environment as environment_checks
from .checks import files as files_checks
from .checks import memory as memory_checks
from .checks import orchestration as orchestration_checks
from .checks import repo_map as repo_map_checks
from .checks import runtime_sandbox as runtime_sandbox_checks
from .checks import tracker as tracker_checks
from .checks import windows as windows_checks
from .context import HealthContext
from .model import CheckResult, JsonObject, Report
from . import project_files

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
    ("memory.index", memory_checks.check_index),
    ("memory.model", memory_checks.check_model),
    ("environment.os", environment_checks.check_os),
    ("environment.git", environment_checks.check_git),
    ("environment.git_identity", environment_checks.check_git_identity),
    ("environment.gitattributes", environment_checks.check_gitattributes),
    ("environment.line_endings", environment_checks.check_line_endings),
    ("environment.python", environment_checks.check_python),
    ("environment.uv", environment_checks.check_uv),
    ("environment.glab", environment_checks.check_glab),
    ("environment.dev_env", environment_checks.check_dev_environment),
    ("environment.output_encoding", environment_checks.check_output_encoding),
    ("environment.codex_sandbox", runtime_sandbox_checks.check_codex_sandbox),
    ("environment.claude_sandbox", runtime_sandbox_checks.check_claude_sandbox),
    ("environment.long_paths", windows_checks.check_long_paths),
    ("environment.path_length", windows_checks.check_path_length),
    ("environment.pytest_temp", windows_checks.check_pytest_temp),
    ("environment.symlinks", windows_checks.check_symlinks),
    ("environment.hook_bash", windows_checks.check_hook_bash),
    ("tracker.project", tracker_checks.check_project),
    ("tracker.auth", tracker_checks.check_auth),
    ("tracker.reachability", tracker_checks.check_reachability),
    ("tracker.permissions", tracker_checks.check_permissions),
    ("tracker.labels", tracker_checks.check_labels),
    ("tracker.git_base", tracker_checks.check_git_base),
    ("orchestration.ledger_summary", orchestration_checks.check_ledger_summary),
    ("orchestration.unfinished_batches", orchestration_checks.check_unfinished_batches),
    ("orchestration.blocked_batches", orchestration_checks.check_blocked_batches),
    ("orchestration.stale_dispatches", orchestration_checks.check_stale_dispatches),
    ("orchestration.orphaned_worktrees", orchestration_checks.check_orphaned_worktrees),
    ("orchestration.disposable_data", orchestration_checks.check_disposable_data),
]

FIXERS: dict[str, FixFn] = {
    "files.skill_registry": files_checks.fix_skill_registry,
    **{
        check_id: directory_checks.make_fix(check_id)
        for check_id in directory_checks.DIRECTORY_PATHS
    },
    "tracker.labels": tracker_checks.fix_labels,
}


def _load_lock(repo: Path) -> tuple[JsonObject | None, str | None]:
    """Разобрать .harness/harness.lock один раз за запуск как (lock, error).

    Отсутствующий файл возвращает (None, None); файл, который не удалось прочитать или который
    не является JSON-объектом, возвращает (None, <причина>) — это фиксируется проверкой `files.check_lock`
    как `fail` вместо аварийной остановки всего запуска до начала проверок.
    """
    path = repo / project_files.LOCK_REL
    if not path.is_file():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return None, "ожидался JSON-объект"
    return data, None


def run(
    repo: Path,
    *,
    online: bool = False,
    snapshot_diff: Callable[[Path], JsonObject] | None = None,
    output_encoding: str | None = None,
    fix: bool = False,
    harness_cli: tuple[str, ...] = ("harness",),
) -> Report:
    """Создать HealthContext и выполнить все зарегистрированные проверки без преждевременного выхода.

    При `fix=True` для проверок, зарегистрированных в `FIXERS`, применяется действие по исправлению
    сразу после выполнения; описание выполненного действия добавляется в `report.fixes_applied`,
    а проверка запускается повторно, чтобы отчёт отражал состояние после исправления.

    Параметры `snapshot_diff`, `output_encoding` и `harness_cli` передаются в `HealthContext`
    без изменений (см. docstring класса — их передают только `cmd_health` в `harness/bin/harness.py` и консоль).
    """
    lock, lock_error = _load_lock(repo)
    context = HealthContext(
        repo=repo,
        lock=lock,
        lock_error=lock_error,
        online=online,
        snapshot_diff=snapshot_diff,
        output_encoding=output_encoding,
        harness_cli=harness_cli,
    )
    report = Report(schema_version=1, repo=str(repo), online=online)
    for check_id, check_fn in REGISTRY:
        result = _run_isolated(check_id, check_fn, context)
        fixer = FIXERS.get(check_id) if fix else None
        if fixer is not None:
            applied = _apply_isolated(fixer, context, result)
            if applied is not None:
                report.fixes_applied.append(applied)
                result = _run_isolated(check_id, check_fn, context)
        report.checks.append(result)
    return report


def _apply_isolated(
    fixer: FixFn, context: HealthContext, result: CheckResult
) -> str | None:
    """Выполнить одно действие исправления; аварийное завершение оставляет результат проверки неизменным."""
    try:
        return fixer(context, result)
    except (Exception, SystemExit):
        return None


def _run_isolated(
    check_id: str, check_fn: CheckFn, context: HealthContext
) -> CheckResult:
    """Выполнить одну проверку; сбой превращается в результат со статусом `fail`, позволяя продолжить остальные проверки.

    Исключение SystemExit также перехватывается: вспомогательные функции, разделяемые со сборщиком
    (project_files.fail), завершают процесс при ошибке, что не должно обрывать запуск проверок здоровья.
    KeyboardInterrupt распространяется дальше.
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
