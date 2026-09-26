#!/usr/bin/env python3
"""Проверка всего проекта: согласованность конфигурации и скиллов, целостность вендорного
snapshot, затем mypy, pytest и полный clean-room прогон."""

import atexit
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

MIN_PYTHON = (3, 12)
if sys.version_info < MIN_PYTHON:
    sys.stderr.write(
        "[ERROR] verify requires Python {}+ (found {}).\n".format(
            ".".join(map(str, MIN_PYTHON)), sys.version.split()[0]
        )
    )
    sys.exit(1)

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.storage import storage_path
from scripts.verification.docs_sync import (
    check_docs_agents_enumeration,
    check_docs_agents_mirror,
    check_no_retired_path_inventory_term,
    check_pvmalove_additions_docs_sync,
    check_pvmalove_override_docs_sync,
    check_pvmalove_suite_summary_sync,
)
from scripts.verification.instructions import check_no_dispatch_specific_data_in_always_sent_files
from scripts.verification.process import isolated_temp_env, remove_tree, run_ok, run_stage
from scripts.verification.text_checks import check_no_todo, grep_contains, grep_line
from scripts.verification.vendor_pin import check_vendor_pin

__all__ = ["isolated_temp_env", "main", "remove_tree", "run_ok", "run_stage"]

# Every Python entry point `py_compile` must accept before any stage runs: the extensionless CLIs and
# each script module, including the packages the verification and clean-room scripts are split into.
_COMPILED_SCRIPTS = (
    ROOT / "harness" / "bin" / "harness",
    ROOT / "bin" / "install-global",
    ROOT / "scripts" / "build_registry.py",
    ROOT / "scripts" / "verify.py",
    ROOT / "scripts" / "test_clean_room.py",
)
_COMPILED_PACKAGES = (ROOT / "scripts" / "verification", ROOT / "scripts" / "clean_room")


def _prepare_run_root() -> tuple[Path, dict[str, str]]:
    """Создать короткий корень запуска до любого subprocess Python и окружение, изолированное в нём.

    py_compile и mypy тоже пишут байткод; кэш рядом с исходниками падает в ограниченных worktree.
    """
    tests_root = storage_path(ROOT, "tmp", "tests")
    tests_root.mkdir(parents=True, exist_ok=True)
    run_tmp = Path(tempfile.mkdtemp(prefix="v", dir=tests_root))
    (run_tmp / ".active.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    atexit.register(lambda: remove_tree(run_tmp) if run_tmp.exists() else None)
    return run_tmp, isolated_temp_env(dict(os.environ, PYTHONPATH=str(ROOT)), run_tmp)


def _compiled_sources() -> list[str]:
    """Файлы, которые должны компилироваться без ошибок."""
    modules = [path for package in _COMPILED_PACKAGES for path in sorted(package.glob("*.py"))]
    return [str(path) for path in (*_COMPILED_SCRIPTS, *modules)]


def _check_global_skills() -> None:
    """Проверить глобальные entry-скиллы и ключевые фразы, на которые опираются их контракты."""
    start_project = ROOT / "global-skills" / "start-project" / "SKILL.md"
    integrate_project = ROOT / "global-skills" / "integrate-project" / "SKILL.md"
    grep_line(start_project, "name: start-project")
    grep_line(integrate_project, "name: integrate-project")
    check_no_todo(ROOT / "global-skills")
    grep_contains(start_project, "until the owner confirms")
    grep_contains(start_project, "Prove each layer separately")
    grep_contains(integrate_project, "Audit is read-only")
    grep_contains(ROOT / "docs" / "skills" / "implement.md", "module-owned guidance")
    grep_contains(ROOT / "docs" / "skills" / "pilot.md", "самоотчёт роли не является token telemetry")


def _check_documentation() -> None:
    """Проверить согласованность документации, шаблонов и постоянных инструкций."""
    check_docs_agents_mirror()
    check_no_retired_path_inventory_term()
    grep_contains(
        ROOT / "skills" / "first-party" / "pvmalove" / "to-tickets" / "SKILL.md",
        "or symbol signatures",
    )
    agents_seed = ROOT / "harness" / "project" / "AGENTS.md.tmpl"
    grep_contains(agents_seed, "For code discovery, run the Repo Map")
    grep_contains(agents_seed, "then use targeted `rg` searches and reads.")
    check_docs_agents_enumeration()
    check_pvmalove_override_docs_sync()
    check_pvmalove_additions_docs_sync()
    check_pvmalove_suite_summary_sync()
    check_vendor_pin()
    check_no_dispatch_specific_data_in_always_sent_files()


def _static_checks(test_env: dict[str, str]) -> None:
    """Быстрые проверки до тестов: JSON каталога, компиляция, скиллы, реестр и документация."""
    run_ok(
        [sys.executable, "-m", "json.tool", str(ROOT / "harness" / "CAPABILITIES.json")],
        stdout=subprocess.DEVNULL,
        env=test_env,
    )
    run_ok([sys.executable, "-m", "py_compile", *_compiled_sources()], env=test_env)
    _check_global_skills()
    run_ok([sys.executable, str(ROOT / "scripts" / "build_registry.py")], env=test_env)
    run_ok(["git", "-C", str(ROOT), "diff", "--exit-code", "--", "skills/REGISTRY.md"])
    _check_documentation()


def _run_stages(run_tmp: Path, test_env: dict[str, str]) -> None:
    """Стадии с замером времени: mypy, pytest и clean-room прогон."""
    run_stage("mypy", [sys.executable, "-m", "mypy"], cwd=ROOT, env=test_env)
    try:
        run_stage(
            "pytest",
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                "-n",
                "4",
                "--basetemp",
                str(run_tmp / "p"),
                str(ROOT / "tests"),
            ],
            env=test_env,
        )
        run_stage(
            "clean-room",
            [sys.executable, str(ROOT / "scripts" / "test_clean_room.py")],
            # UTF-8 mode: the harness CLI writes UTF-8, and a cp1252 runner locale cannot decode it.
            env=dict(test_env, HARNESS_TEST_RUN_ROOT=str(run_tmp), PYTHONUTF8="1"),
        )
    finally:
        remove_tree(run_tmp)


def main() -> None:
    """Точка входа: подготовить изолированный корень, выполнить проверки и стадии."""
    run_tmp, test_env = _prepare_run_root()
    _static_checks(test_env)
    _run_stages(run_tmp, test_env)
    print("agent-harness verification passed")


if __name__ == "__main__":
    main()
