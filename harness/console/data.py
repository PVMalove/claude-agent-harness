"""Сбор фактов средствами только стандартной библиотеки для экранов Dashboard и Diagnostics консоли.
Каждая функция здесь может импортироваться и выполняться без установленного пакета `textual` — экраны
`screens/*.py` только отрисовывают возвращаемые данные и не содержат собственной логики проверок health
или каталога сборщика.

Функция `harness.health.registry.run` вызывается внутри процесса через `run_health` с теми же
параметрами `snapshot_diff` и вызовом CLI, что и команда `cmd_health` в `harness/bin/harness.py`,
поэтому отчёт консоли в точности совпадает с отчётом CLI (включая дрейф снепшота скиллов). Статус дрейфа
и количество активных батчей считываются через CLI сборщика и координатора с флагом `--json` в
подпроцессе, что исключает дублирование логики разрешения возможностей и журнала в консоли (оба факта
остаются опциональными: для проекта без `.harness/harness.lock` или без бэкенд-оркестрации отображается
«не подключено»).
"""

from __future__ import annotations

import functools
import json
import runpy
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, cast

from ..health import registry as health_registry
from ..health.context import shell_join
from ..health.model import JsonObject, Report

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = _PACKAGE_ROOT / "VERSION"
BIN_HARNESS_PATH = _PACKAGE_ROOT / "bin" / "harness.py"


@dataclass(frozen=True)
class DashboardData:
    """Факты для отображения на экране Dashboard: счётчики проверок health, активные батчи, уровень Repo Map, версия харнесса и статус дрейфа."""

    ok: int
    warn: int
    fail: int
    skipped: int
    repo_map_tier: str
    harness_version: str
    drift_state: str
    active_batches: (
        int | None
    )  # None: backend-orchestration is not connected in this project
    # (status, check id, message) of every failed and then every warning check.
    problems: tuple[tuple[str, str, str], ...] = ()


def harness_version() -> str:
    """Считывает текущую версию харнесса из файла VERSION пакета."""
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def _repo_map_tier(report: Report) -> str:
    """Извлекает уровень готовности Repo Map (tier) из проверок отчёта health."""
    for check in report.checks:
        if check.id == "repo_map.tier":
            return check.message
    return "не установлен"


def drift_state(repo: Path, *, timeout: float = 30.0) -> str:
    """Вызывает `harness diff --json` в подпроцессе для определения состояния дрейфа управляемых файлов."""
    try:
        result = subprocess.run(
            [sys.executable, str(BIN_HARNESS_PATH), "diff", str(repo), "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return "неизвестно"
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return "неизвестно"
    state = payload.get("state") if isinstance(payload, dict) else None
    return state if isinstance(state, str) else "неизвестно"


def active_batches(repo: Path, *, timeout: float = 30.0) -> int | None:
    """Возвращает количество открытых батчей через `coordinator.py batch list --open`, либо None, если оркестрация не подключена."""
    coordinator = repo / ".harness" / "orchestration" / "coordinator.py"
    if not coordinator.is_file():
        return None
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(coordinator),
                "--repo",
                str(repo),
                "batch",
                "list",
                "--open",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    batches = payload.get("batches") if isinstance(payload, dict) else None
    if not isinstance(batches, list):
        return None
    return len(batches)


@functools.cache
def _packager() -> dict[str, object]:
    """Кэширует пространство имён CLI сборщика харнесса для доступа к snapshot_diff и вызовам CLI."""
    return runpy.run_path(str(BIN_HARNESS_PATH))


def run_health(repo: Path, *, online: bool = False, fix: bool = False) -> Report:
    """Выполняет проверку состояния репозитория, аналогично вызову `harness health [--online] [--fix]`."""
    packager = _packager()
    return health_registry.run(
        repo,
        online=online,
        fix=fix,
        snapshot_diff=cast(Callable[[Path], JsonObject], packager["snapshot_diff"]),
        harness_cli=cast(tuple[str, ...], packager["HARNESS_CLI"]),
    )


def collect_dashboard(repo: Path, *, online: bool = False) -> DashboardData:
    """Собирает сводные данные для экрана Dashboard. По умолчанию работает в режиме офлайн без ожидания сети."""
    report = run_health(repo, online=online)
    summary = report.summary()
    return DashboardData(
        ok=summary["ok"],
        warn=summary["warn"],
        fail=summary["fail"],
        skipped=summary["skipped"],
        repo_map_tier=_repo_map_tier(report),
        harness_version=harness_version(),
        drift_state=drift_state(repo),
        active_batches=active_batches(repo),
        problems=tuple(
            (check.status, check.id, check.message)
            for status in ("fail", "warn")
            for check in report.checks
            if check.status == status
        ),
    )


def collect_diagnostics(repo: Path, *, online: bool = False) -> Report:
    """Формирует полный отчёт health для экрана Diagnostics, аналогичный запуску команды `harness health`."""
    return run_health(repo, online=online)


def apply_local_fixes(repo: Path, *, online: bool = False) -> Report:
    """Применяет локальные исправления проверок (создание директорий, обновление реестра) и повторно выполняет проверки."""
    return run_health(repo, online=online, fix=True)


def health_cli_line(repo: Path, *flags: str) -> str:
    """Формирует эквивалентную строковую команду CLI для запуска проверки health, например `harness health <repo> --online`."""
    return shell_join(["harness", "health", str(repo), *flags])
