#!/usr/bin/env python3
"""Проверка всего проекта: согласованность конфигурации и скиллов, целостность вендорного
snapshot, затем mypy, pytest и полный clean-room прогон."""

from __future__ import annotations

import argparse
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from collections.abc import Mapping, Sequence
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

from harness.gate_runner.gate_runner import (
    EvidenceContext,
    StageCommand,
    build_stage_evidence,
    sanitise,
)
from harness.gate_runner.junit import failed_nodeids
from harness.storage import storage_path
from scripts.verification.cache import (
    COLD_ENV,
    JUNIT_ENV,
    VerificationCacheError,
    WorktreeCache,
    checkout_fingerprint,
    head_commit,
    open_cache,
    write_evidence,
)
from scripts.verification import failed_only
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
    run_captured,
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


# Platform-neutral and slow on Windows (hundreds of tests each spawn a dozen git.exe processes):
# a pull request leaves them to Linux and the full run on `master` (ADR 0018).
WINDOWS_PR_IGNORED_TESTS = ("tests/orchestration/test_coordinator.py",)


def pytest_command(
    run_tmp: Path,
    *,
    platform: str = sys.platform,
    cpu_count: int | None = None,
    platform_subset: bool = False,
    fail_fast: bool = False,
    failed_first: Path | None = None,
    junit_xml: Path | None = None,
    cache_dir: Path | None = None,
    targets: Sequence[str] | None = None,
) -> list[str]:
    """Сформировать аргументы запуска pytest с учетом платформы и числа воркеров.

    `fail_fast` останавливает прогон на первом падении, `failed_first` хранит кэш pytest в
    указанном каталоге и запускает прежде упавшие тесты первыми, `junit_xml` пишет отчёт для сводки CI,
    `cache_dir` держит кэш pytest в кэше worktree без смены порядка тестов (ADR 0023, раздел 4),
    `targets` заменяет весь набор точными nodeid или файлами failed-only прогона (ADR 0023, раздел 3)."""
    effective_cpus = os.cpu_count() or 1 if cpu_count is None else cpu_count
    workers = (
        min(8, effective_cpus, len(targets)) if targets else min(8, effective_cpus)
    )
    args = [sys.executable, "-m", "pytest"]
    if failed_first is not None:
        args.extend(["-o", f"cache_dir={failed_first}", "--ff"])
    elif cache_dir is not None:
        args.extend(["-o", f"cache_dir={cache_dir}"])
    else:
        args.extend(["-p", "no:cacheprovider"])
    if fail_fast:
        args.append("-x")
    if junit_xml is not None:
        args.extend([f"--junitxml={junit_xml}", "-o", "junit_family=xunit2"])
    args.extend(
        [
            "-n",
            str(workers),
            "--basetemp",
            str(run_tmp / "p"),
            *(targets or [str(ROOT / "tests")]),
        ]
    )
    if platform == "win32":
        args.extend(
            [
                "--ignore=tests/memory/test_eval.py",
                "--ignore=tests/memory/test_vector_probe.py",
            ]
        )
        if platform_subset:
            args.extend(f"--ignore={path}" for path in WINDOWS_PR_IGNORED_TESTS)
    return args


def mypy_command() -> list[str]:
    """The mypy stage."""
    return [sys.executable, "-m", "mypy"]


def clean_room_command() -> list[str]:
    """The clean-room stage."""
    return [sys.executable, str(ROOT / "scripts" / "test_clean_room.py")]


# What each recorded stage is, as the stage evidence names it.
_STAGE_KINDS = {"mypy": "typecheck", "pytest": "pytest", "clean-room": "clean-room"}


class _EvidenceRecorder:
    """Runs the stages with separate, kept output streams and writes their stage evidence.

    A stage the run never reached, because an earlier one failed or ``--only`` skipped it after the
    failure, is recorded as ``not_run`` (ADR 0023, section 5).
    """

    def __init__(
        self,
        directory: Path,
        context: EvidenceContext,
        planned: list[str],
        selections: Mapping[str, str],
    ) -> None:
        self._directory = directory
        self._context = context
        self._planned = planned
        self.selections = dict(selections)
        self._runs: dict[str, StageCommand] = {}

    def run(
        self,
        name: str,
        cmd: list[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        junit: Path | None = None,
    ) -> None:
        """Run one stage, report its duration like ``run_stage`` and keep its evidence."""
        captured = run_captured(cmd, env=env, cwd=cwd)
        state = "passed" if captured.exit_code == 0 else "failed"
        print(f"[verify] {name}: {state} in {captured.duration_ms / 1000:.2f}s")
        failed: tuple[str, ...] = ()
        nothing_selected = (
            name == "pytest" and captured.exit_code == failed_only.NO_TESTS_EXIT
        )
        if (
            captured.exit_code != 0
            and not nothing_selected
            and junit is not None
            and junit.is_file()
        ):
            try:
                failed = failed_nodeids(junit.read_text(encoding="utf-8"), ROOT).nodeids
            except (OSError, UnicodeDecodeError, ValueError):
                failed = ()
        command = subprocess.list2cmdline(cmd)
        self._runs[name] = StageCommand(
            command=command,
            executed=sanitise(command),
            exit_code=captured.exit_code,
            output="",
            stdout=sanitise(captured.stdout),
            stderr=sanitise(captured.stderr),
            duration_ms=captured.duration_ms,
            failed_nodeids=failed,
            not_run=nothing_selected,
        )
        if captured.exit_code != 0:
            sys.exit(captured.exit_code)

    def record_unrun(self, name: str, exit_code: int, reason: str) -> None:
        """Record a stage that selected nothing and so never started: ``not_run`` with ``reason``."""
        self._runs[name] = StageCommand(
            command=name,
            executed=name,
            exit_code=exit_code,
            output="",
            stderr=reason + "\n",
            not_run=True,
        )

    def write(self) -> None:
        """Write the evidence document and the streams, whatever happened to the run."""
        bundle = build_stage_evidence(
            self._context,
            [(name, self._runs.get(name)) for name in self._planned],
            directory="",
            kinds=_STAGE_KINDS,
            selections=self.selections,
        )
        write_evidence(self._directory, bundle)


def _mirror_junit(source: Path, targets: list[Path]) -> None:
    """Copy the JUnit report to every other place that expects it (CI, the gate, the cache)."""
    if not source.is_file():
        return
    for target in targets:
        if target != source:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)


def _stage(
    recorder: _EvidenceRecorder | None,
    name: str,
    cmd: list[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    junit: Path | None = None,
) -> None:
    """Run one stage, with its streams kept apart when a recorder is given."""
    if recorder is None:
        run_stage(name, cmd, env=env, cwd=cwd)
    else:
        recorder.run(name, cmd, env=env, cwd=cwd, junit=junit)


def _run_stages(
    run_tmp: Path,
    test_env: dict[str, str],
    *,
    only: str | None,
    platform_subset: bool,
    fail_fast: bool = False,
    failed_first: Path | None = None,
    junit_xml: Path | None = None,
    cache: WorktreeCache | None = None,
    recorder: _EvidenceRecorder | None = None,
    junit_mirrors: list[Path] | None = None,
) -> None:
    """Стадии с замером времени: mypy, pytest и clean-room прогон; `only` оставляет одну группу.

    С `cache` mypy и pytest держат кэши в кэше worktree; clean-room стадия всегда холодная.
    С `recorder` потоки стадий хранятся раздельно и порождают stage evidence."""
    try:
        if only != "clean-room":
            mypy_env = (
                test_env
                if cache is None
                else dict(test_env, MYPY_CACHE_DIR=str(cache.mypy_dir))
            )
            _stage(recorder, "mypy", mypy_command(), cwd=ROOT, env=mypy_env)
            try:
                _stage(
                    recorder,
                    "pytest",
                    pytest_command(
                        run_tmp,
                        platform_subset=platform_subset,
                        fail_fast=fail_fast,
                        failed_first=failed_first,
                        junit_xml=junit_xml,
                        cache_dir=None if cache is None else cache.pytest_dir,
                    ),
                    env=test_env,
                    junit=junit_xml,
                )
            finally:
                if junit_xml is not None:
                    _mirror_junit(junit_xml, junit_mirrors or [])
        if only != "tests":
            cold = {} if cache is None else {"MYPY_CACHE_DIR": str(run_tmp / "mypy")}
            _stage(
                recorder,
                "clean-room",
                clean_room_command(),
                # UTF-8 mode: the harness CLI writes UTF-8, and a cp1252 runner locale cannot decode it.
                env=dict(
                    test_env, HARNESS_TEST_RUN_ROOT=str(run_tmp), PYTHONUTF8="1", **cold
                ),
            )
    finally:
        try:
            if recorder is not None:
                recorder.write()
        finally:
            remove_tree(run_tmp)


def _run_failed_only(
    run_tmp: Path,
    test_env: dict[str, str],
    *,
    platform_subset: bool,
    junit_xml: Path | None,
    cache: WorktreeCache,
    recorder: _EvidenceRecorder | None,
    junit_mirrors: list[Path],
) -> None:
    """Rerun only the tests that failed in the previous JUnit report of this worktree cache.

    The pytest stage alone runs, with the exact failed nodeids (``failed-only``), or with their
    surviving test files when a nodeid vanished (``focused``). An empty selection, like a pytest
    run that collects nothing, is ``not_run`` with exit code 5. The result is a diagnostic: it
    proves only the selected tests and never authorizes a publish (ADR 0023, section 3).
    """
    try:
        # The previous report is read before this run replaces it in the cache.
        selection = failed_only.select(ROOT, cache.junit_path, test_env)
        if recorder is not None:
            recorder.selections["pytest"] = selection.selection
        print(f"[verify] failed-only: {selection.reason}")
        if selection.empty:
            print(
                "[verify] pytest: not_run, no test selected "
                f"(exit {failed_only.NO_TESTS_EXIT})"
            )
            if recorder is not None:
                recorder.record_unrun(
                    "pytest", failed_only.NO_TESTS_EXIT, selection.reason
                )
            sys.exit(failed_only.NO_TESTS_EXIT)
        try:
            _stage(
                recorder,
                "pytest",
                pytest_command(
                    run_tmp,
                    platform_subset=platform_subset,
                    junit_xml=junit_xml,
                    cache_dir=cache.pytest_dir,
                    targets=selection.targets,
                ),
                env=test_env,
                # The nodeids are relative to the rootdir, so pytest runs from the checkout.
                cwd=ROOT,
                junit=junit_xml,
            )
        finally:
            if junit_xml is not None:
                _mirror_junit(junit_xml, junit_mirrors)
        print(
            f"[verify] {selection.selection}: the selected tests passed; this proves only "
            "them and never authorizes a publish, which needs the full clean-room run"
        )
    finally:
        try:
            if recorder is not None:
                recorder.write()
        finally:
            remove_tree(run_tmp)


def build_parser() -> argparse.ArgumentParser:
    """Парсер CLI: две необязательные опции сужения прогона, `--help` описывает стадии до их запуска."""
    parser = argparse.ArgumentParser(
        prog="scripts/verify.py",
        description=(
            "Full project verification. Without options every run executes all stages\n"
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
    parser.add_argument(
        "--only",
        choices=("tests", "clean-room"),
        help=(
            "run one CI job's share: 'tests' = static checks, mypy and pytest; "
            "'clean-room' = static checks and the clean-room run"
        ),
    )
    parser.add_argument(
        "--platform-subset",
        action="store_true",
        help=(
            "on Windows skip the platform-neutral slow test files "
            "(WINDOWS_PR_IGNORED_TESTS); the full run omits this flag"
        ),
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="stop pytest at the first failing test instead of running the whole suite",
    )
    parser.add_argument(
        "--failed-first",
        type=Path,
        metavar="CACHE_DIR",
        help=(
            "keep the pytest cache in CACHE_DIR and run the tests that failed last time first, "
            "so a repeated failure shows up in the first minute; the whole suite still runs, "
            "so it is recorded as the full selection"
        ),
    )
    parser.add_argument(
        "--worktree-cache",
        action="store_true",
        help=(
            "keep the mypy and pytest caches and the previous JUnit report in "
            ".harness/.sandboxes/cache/<worktree-id>/<environment_fingerprint>/; a cache of "
            "another worktree or fingerprint is never read, and the run is refused when "
            f"{COLD_ENV} marks a cold clean-room environment"
        ),
    )
    parser.add_argument(
        "--failed-only",
        action="store_true",
        help=(
            "rerun only the pytest tests that failed in the previous JUnit report of the "
            "--worktree-cache (the exact nodeids; their surviving test files when a nodeid "
            "vanished); an empty selection or no collected test is not_run with exit code 5. "
            "A diagnostic: it never authorizes a publish"
        ),
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        metavar="DIR",
        help=(
            "write stage-evidence.v1 for the mypy, pytest and clean-room stages into DIR: "
            "separate stdout and stderr files with their sha256, exit codes, durations, failed "
            "nodeids, and not_run for every stage the run did not reach"
        ),
    )
    parser.add_argument(
        "--junit-xml",
        type=Path,
        metavar="PATH",
        help=(
            "write the pytest results as JUnit XML (the CI summary reads it); without it the "
            f"report goes to ${JUNIT_ENV}, the worktree cache or the evidence directory"
        ),
    )
    return parser


def _junit_paths(
    args: argparse.Namespace, cache: WorktreeCache | None
) -> tuple[Path | None, list[Path]]:
    """The JUnit report path of the pytest stage and the other places that expect a copy of it."""
    wanted: list[Path] = []
    if args.junit_xml is not None:
        wanted.append(args.junit_xml)
    if os.environ.get(JUNIT_ENV):
        wanted.append(Path(os.environ[JUNIT_ENV]))
    if cache is not None:
        wanted.append(cache.junit_path)
    if args.evidence_dir is not None:
        wanted.append(args.evidence_dir / "pytest.junit.xml")
    return (wanted[0] if wanted else None), wanted


def _planned_stages(only: str | None) -> list[str]:
    """The stages a run with this ``--only`` goes through, in order."""
    return [
        *(["mypy", "pytest"] if only != "clean-room" else []),
        *(["clean-room"] if only != "tests" else []),
    ]


def _refuse(message: str) -> None:
    sys.stderr.write(f"[ERROR] {message}\n")
    sys.exit(2)


def main(argv: list[str] | None = None) -> None:
    """Точка входа: подготовить изолированный корень, выполнить проверки и стадии."""
    args = build_parser().parse_args(argv)
    if args.worktree_cache and args.failed_first is not None:
        _refuse(
            "--worktree-cache and --failed-first both own the pytest cache; pick one"
        )
    if args.failed_only:
        if not args.worktree_cache:
            _refuse("--failed-only reads the previous JUnit report of --worktree-cache")
        if args.failed_first is not None or args.only is not None or args.fail_fast:
            _refuse(
                "--failed-only runs the pytest stage alone and cannot combine with "
                "--failed-first, --only or --fail-fast"
            )
    cache: WorktreeCache | None = None
    recorder: _EvidenceRecorder | None = None
    try:
        if args.worktree_cache:
            cache = open_cache(ROOT, os.environ)
        if args.evidence_dir is not None:
            context = EvidenceContext(
                candidate_sha=head_commit(ROOT),
                environment_fingerprint=checkout_fingerprint(ROOT),
            )
            planned = ["pytest"] if args.failed_only else _planned_stages(args.only)
            recorder = _EvidenceRecorder(
                args.evidence_dir, context, planned, {"pytest": "full"}
            )
    except VerificationCacheError as exc:
        _refuse(str(exc))
    junit_xml, junit_copies = _junit_paths(args, cache)
    run_tmp, test_env = _prepare_run_root()
    if args.failed_only and cache is not None:
        _run_failed_only(
            run_tmp,
            test_env,
            platform_subset=args.platform_subset,
            junit_xml=junit_xml,
            cache=cache,
            recorder=recorder,
            junit_mirrors=junit_copies,
        )
        return
    try:
        _static_checks(test_env)
    except BaseException:
        # No stage was reached: the evidence says so instead of staying silent.
        if recorder is not None:
            recorder.write()
        raise
    _run_stages(
        run_tmp,
        test_env,
        only=args.only,
        platform_subset=args.platform_subset,
        fail_fast=args.fail_fast,
        failed_first=args.failed_first,
        junit_xml=junit_xml,
        cache=cache,
        recorder=recorder,
        junit_mirrors=junit_copies,
    )
    print("agent-harness verification passed")


if __name__ == "__main__":
    main()
