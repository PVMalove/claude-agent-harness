#!/usr/bin/env python3
"""Проверка всего проекта: согласованность конфигурации и скиллов, целостность вендорного
snapshot, затем mypy, pytest и полный clean-room прогон."""

from __future__ import annotations

import argparse
import atexit
import json
import os
import subprocess
import sys
import tempfile
import traceback
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
    check_guide_diagram_previews,
    check_no_retired_path_inventory_term,
    check_pvmalove_additions_docs_sync,
    check_pvmalove_override_docs_sync,
    check_pvmalove_suite_summary_sync,
)
from scripts.verification.instructions import (
    check_no_dispatch_specific_data_in_always_sent_files,
)
from scripts.verification.process import (
    isolated_temp_env,
    remove_tree,
    run_ok,
    run_stage,
)
from scripts.verification.text_checks import check_no_todo, grep_contains, grep_line
from scripts.verification.vendor_pin import check_vendor_pin

__all__ = [
    "check_syntax",
    "isolated_temp_env",
    "main",
    "remove_tree",
    "run_ok",
    "run_stage",
]


# Every Python entry point must compile before any stage runs: the extensionless CLIs and
# each script module, including the packages the verification and clean-room scripts are split into.
_COMPILED_SCRIPTS = (
    ROOT / "harness" / "bin" / "harness.py",
    ROOT / "bin" / "install-global.py",
    ROOT / "scripts" / "build_registry.py",
    ROOT / "scripts" / "verify.py",
    ROOT / "scripts" / "test_clean_room.py",
)
_COMPILED_PACKAGES = (
    ROOT / "scripts" / "verification",
    ROOT / "scripts" / "clean_room",
)


def _prepare_run_root() -> tuple[Path, dict[str, str]]:
    """Создать короткий корень запуска до любого subprocess Python и окружение, изолированное в нём.
    Python subprocesses run without bytecode writes, avoiding MAX_PATH when the source is in a
    linked worktree. Syntax checks below compile in memory for the same reason.
    """
    tests_root = storage_path(ROOT, "runs", "tests")
    tests_root.mkdir(parents=True, exist_ok=True)
    run_tmp = Path(tempfile.mkdtemp(prefix="v", dir=tests_root))
    (run_tmp / ".active.json").write_text(
        json.dumps({"pid": os.getpid()}), encoding="utf-8"
    )
    atexit.register(lambda: remove_tree(run_tmp) if run_tmp.exists() else None)
    return run_tmp, isolated_temp_env(dict(os.environ, PYTHONPATH=str(ROOT)), run_tmp)


def check_syntax(sources: list[str]) -> None:
    """Скомпилировать исходники в памяти, как `py_compile`, но без записи `.pyc` (#381)."""
    failed = False
    for source in sources:
        try:
            compile(Path(source).read_bytes(), source, "exec")
        except (SyntaxError, ValueError) as error:
            sys.stderr.write("".join(traceback.format_exception_only(error)))
            failed = True
    if failed:
        sys.exit(1)


def _compiled_sources() -> list[str]:
    """Файлы, которые должны компилироваться без ошибок."""
    modules = [
        path for package in _COMPILED_PACKAGES for path in sorted(package.glob("*.py"))
    ]
    return [str(path) for path in (*_COMPILED_SCRIPTS, *modules)]


def _check_python_syntax() -> None:
    """Скомпилировать точки входа без записи файлов .pyc при длинных путях worktree."""
    for source in _compiled_sources():
        compile(Path(source).read_bytes(), source, "exec")


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
    grep_contains(
        ROOT / "docs" / "skills" / "pilot.md",
        "самоотчёт роли не является token telemetry",
    )


def _check_documentation() -> None:
    """Проверить согласованность документации, шаблонов и постоянных инструкций."""
    check_docs_agents_mirror()
    check_guide_diagram_previews()
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


def _check_private_terms(test_env: dict[str, str]) -> None:
    """Проверить staged diff, непубликованные коммиты и ветку по приватному списку терминов (#438).

    Без списка проверка печатает уведомление о пропуске и не падает: так в CI без секрета.
    """
    run_ok(
        [
            sys.executable,
            str(ROOT / "scripts" / "check_private_terms.py"),
            "--repo",
            str(ROOT),
            "--staged",
            "--commits",
            "--branch",
        ],
        env=test_env,
    )


def _static_checks(test_env: dict[str, str]) -> None:
    """Быстрые проверки до тестов: приватные термины, JSON каталога, компиляция, скиллы, реестр
    и документация."""
    _check_private_terms(test_env)
    run_ok(
        [
            sys.executable,
            "-m",
            "json.tool",
            str(ROOT / "harness" / "CAPABILITIES.json"),
        ],
        stdout=subprocess.DEVNULL,
        env=test_env,
    )

    _check_python_syntax()
    _check_global_skills()
    run_ok([sys.executable, str(ROOT / "scripts" / "build_registry.py")], env=test_env)
    run_ok(["git", "-C", str(ROOT), "diff", "--exit-code", "--", "skills/REGISTRY.md"])
    _check_documentation()


def pytest_command(
    run_tmp: Path,
    *,
    platform: str = sys.platform,
    cpu_count: int | None = None,
) -> list[str]:
    """Сформировать аргументы запуска pytest с учетом платформы и числа воркеров."""
    effective_cpus = os.cpu_count() or 1 if cpu_count is None else cpu_count
    workers = 2 if platform == "win32" else min(4, effective_cpus)
    args = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-n",
        str(workers),
        "--basetemp",
        str(run_tmp / "p"),
        str(ROOT / "tests"),
    ]
    if platform == "win32":
        args.extend(
            [
                "--ignore=tests/memory/test_eval.py",
                "--ignore=tests/memory/test_vector_probe.py",
            ]
        )
    return args


def _run_stages(run_tmp: Path, test_env: dict[str, str]) -> None:
    """Стадии с замером времени: mypy, pytest и clean-room прогон."""
    run_stage("mypy", [sys.executable, "-m", "mypy"], cwd=ROOT, env=test_env)
    try:
        run_stage("pytest", pytest_command(run_tmp), env=test_env)
        run_stage(
            "clean-room",
            [sys.executable, str(ROOT / "scripts" / "test_clean_room.py")],
            # UTF-8 mode: the harness CLI writes UTF-8, and a cp1252 runner locale cannot decode it.
            env=dict(test_env, HARNESS_TEST_RUN_ROOT=str(run_tmp), PYTHONUTF8="1"),
        )
    finally:
        remove_tree(run_tmp)


def build_parser() -> argparse.ArgumentParser:
    """Парсер CLI: опций нет, `--help` описывает стадии до их запуска."""
    return argparse.ArgumentParser(
        prog="scripts/verify.py",
        description=(
            "Full project verification. Takes no options: every run executes all stages\n"
            "in order and stops at the first failure."
        ),
        epilog=(
            "stages:\n"
            "  1. static checks: private terms, CAPABILITIES.json syntax, Python compilation,\n"
            "     global skills, documentation sync, vendor pin\n"
            "  2. skills/REGISTRY.md: rebuilt in place, then must match the committed file\n"
            "  3. mypy\n"
            "  4. pytest\n"
            "  5. clean-room install and update run\n"
            "\n"
            "Temporary files go to an isolated run root under the harness storage and are removed\n"
            "at exit. Usually run as `make verify`."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def main(argv: list[str] | None = None) -> None:
    """Точка входа: подготовить изолированный корень, выполнить проверки и стадии."""
    build_parser().parse_args(argv)
    run_tmp, test_env = _prepare_run_root()
    _static_checks(test_env)
    _run_stages(run_tmp, test_env)
    print("agent-harness verification passed")


if __name__ == "__main__":
    main()
