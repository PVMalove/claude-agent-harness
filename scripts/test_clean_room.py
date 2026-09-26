#!/usr/bin/env python3
"""End-to-end clean-room проверка: harness/bin/harness, bin/install-global и hooks проекта.

Сценарии живут в пакете `scripts/clean_room` и выполняются по порядку `SCENARIOS` против
одноразовых репозиториев; проверяется итоговое состояние файлов и реальное поведение hooks.
"""

import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

MIN_PYTHON = (3, 12)
if sys.version_info < MIN_PYTHON:
    sys.stderr.write(
        "[ERROR] test-clean-room requires Python {}+ (found {}).\n".format(
            ".".join(map(str, MIN_PYTHON)), sys.version.split()[0]
        )
    )
    sys.exit(1)

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from harness.storage import storage_path
from scripts.clean_room import (
    backend_orchestration,
    base_commit_gate,
    baseline_metrics,
    checkpoint_session,
    context_package,
    coordinator_seam,
    delivery_stats,
    delta_review,
    delta_review_probe,
    dispatch_watchdog,
    fixed_sequence_review,
    global_install,
    install_and_update,
    ledger_migration,
    orca_adapter,
    orchestration_config,
    policy_contract,
    project_hooks,
    risk_aware_review,
    stuck_batch,
    zero_config_defaults,
)

# Order matters: each scenario reads what earlier ones left in the shared context (see each module's
# `run` docstring), exactly as the single sequential run did before the split.
SCENARIOS = (
    install_and_update,
    backend_orchestration,
    orca_adapter,
    policy_contract,
    coordinator_seam,
    ledger_migration,
    risk_aware_review,
    delta_review_probe,
    delta_review,
    fixed_sequence_review,
    context_package,
    checkpoint_session,
    base_commit_gate,
    dispatch_watchdog,
    stuck_batch,
    zero_config_defaults,
    delivery_stats,
    baseline_metrics,
    orchestration_config,
    project_hooks,
    global_install,
)


def _force_remove_readonly(func, path, exc):
    """Снять атрибут «только чтение» и повторить удаление: git оставляет такие файлы на Windows."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


# Longest observed ledger atomic-write path below the root is 166 chars.
DEEPEST_RELATIVE_PATH = 170
WINDOWS_MAX_PATH = 259


def _long_paths_enabled():
    """Включены ли длинные пути Windows (`LongPathsEnabled`)."""
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem"
        ) as key:
            return winreg.QueryValueEx(key, "LongPathsEnabled")[0] == 1
    except OSError:
        return False


def _check_path_budget(test_root):
    """Упасть заранее с понятной рекомендацией, если корень не оставляет места под MAX_PATH (#305).

    Иначе длинный TMP выводит дерево за MAX_PATH, и ошибка WinError всплывает глубоко в git или hook.
    """
    if os.name != "nt" or _long_paths_enabled():
        return
    if len(str(test_root)) + 1 + DEEPEST_RELATIVE_PATH > WINDOWS_MAX_PATH:
        sys.exit(
            f"test-clean-room: temp root {test_root} ({len(str(test_root))} chars) leaves no room "
            f"for its {DEEPEST_RELATIVE_PATH}-char tree under Windows MAX_PATH "
            f"({WINDOWS_MAX_PATH}). Point TMP/TEMP at a shorter directory, or enable "
            "LongPathsEnabled."
        )


def _run(test_root: Path):
    """Выполнить все сценарии по порядку с общим контекстом, начиная с корня `test_root`."""
    context = SimpleNamespace(test_root=test_root)
    for scenario in SCENARIOS:
        scenario.run(context)


def main():
    """Точка входа: подготовить корень запуска, выполнить сценарии и убрать за собой."""
    run_root = os.environ.get("HARNESS_TEST_RUN_ROOT")
    if run_root:
        test_root = Path(run_root) / "c"
        test_root.mkdir()
    else:
        tests_root = storage_path(ROOT, "tmp", "tests")
        tests_root.mkdir(parents=True, exist_ok=True)
        test_root = Path(tempfile.mkdtemp(prefix="c", dir=tests_root))
    try:
        _check_path_budget(test_root)
        _run(test_root)
    finally:
        try:
            shutil.rmtree(test_root, onexc=_force_remove_readonly)
        except OSError as exc:
            # A cleanup failure must not replace the test's own result or traceback.
            print(f"warning: could not remove {test_root}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
