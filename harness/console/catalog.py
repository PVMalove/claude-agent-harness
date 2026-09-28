"""Каталог команд консоли: декларативные структуры данных стандартной библиотеки, описывающие
каждую команду разделов консоли — отображаемый заголовок, эквивалентные аргументы CLI, класс
обратимости и функция CLI, в которую диспетчеризуется вызов. Описан и протестирован без зависимости от textual.

Консоль не реализует логику команд заново: она запускает аргументы CLI записи как подпроцесс (см.
`process_argv`), благодаря чему функция CLI, указанная в `function`, выполняет работу со всеми
существующими проверками (блокировки леджера, `--confirm RESET`, отказ при дрейфе файлов).
Тест tests/test_console_catalog.py проверяет, что каждый набор аргументов действительно передаётся в эту функцию.
"""

from __future__ import annotations

import re
import shlex
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping, Sequence

from ..gate_runner.gate_runner import project_python
from .launcher import BIN_HARNESS_PATH


class Reversibility(Enum):
    """Классы обратимости команд, определяющие необходимость и причину запроса подтверждения."""

    REVERSIBLE = "reversible"
    DELETES_LOCAL_DATA = "deletes-local-data"
    TERMINAL_COORDINATOR_ACTION = "terminal-coordinator-action"
    EXTERNAL_CHANGE = "external-change"
    OVERWRITES_MANAGED_FILES = "overwrites-managed-files"


# Why an irreversible class needs a confirmation - shown on the confirmation screen.
CONFIRMATION_REASONS = {
    Reversibility.DELETES_LOCAL_DATA: "Команда удаляет локальные данные — восстановить их нельзя.",
    Reversibility.TERMINAL_COORDINATOR_ACTION: (
        "Команда записывает терминальное решение coordinator — отменить его нельзя."
    ),
    Reversibility.EXTERNAL_CHANGE: (
        "Команда меняет внешнюю систему: это видят другие люди или это расходует токены."
    ),
    Reversibility.OVERWRITES_MANAGED_FILES: (
        "Команда перезаписывает управляемые файлы харнесса — незакоммиченные правки в них пропадут."
    ),
}

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
_REPO = "repo"


@dataclass(frozen=True)
class CatalogEntry:
    """Запись команды в каталоге: параметры CLI с плейсхолдерами `{repo}` и `{<входное поле>}`, класс обратимости и целевая функция."""

    key: str
    title: str
    argv: tuple[str, ...]
    reversibility: Reversibility
    function: str
    typed_confirmation: str | None = None
    # Runs `python` under the repository's dev environment (`.harness/.venv`: pytest, mypy, ...)
    # instead of the console's own interpreter - only for this repository's verification script.
    dev_environment: bool = False

    @property
    def needs_confirmation(self) -> bool:
        """Определяет, требует ли команда подтверждения перед запуском (любой класс, кроме REVERSIBLE)."""
        return self.reversibility is not Reversibility.REVERSIBLE

    def available(self, repo: Path) -> bool:
        """Проверяет доступность команды для запуска в репозитории (существование необходимого скрипта)."""
        if self.argv[0] != "python":
            return True
        return (repo / self.argv[1]).is_file()

    @property
    def inputs(self) -> tuple[str, ...]:
        """Возвращает список имён параметров команды, которые требуется запросить у пользователя."""
        names: list[str] = []
        for part in self.argv:
            for name in _PLACEHOLDER.findall(part):
                if name != _REPO and name not in names:
                    names.append(name)
        return tuple(names)

    def cli_argv(
        self, repo: Path, values: Mapping[str, str] | None = None
    ) -> list[str]:
        """Формирует список аргументов CLI с подстановкой значений параметров вместо плейсхолдеров."""
        known = {_REPO: str(repo), **(values or {})}
        return [
            _PLACEHOLDER.sub(lambda match: known.get(match[1], f"<{match[1]}>"), part)
            for part in self.argv
        ]

    def cli_line(self, repo: Path, values: Mapping[str, str] | None = None) -> str:
        """Возвращает экранированную строковую команду CLI для отображения в интерфейсе."""
        return shlex.join(self.cli_argv(repo, values))


def process_argv(
    cli_argv: Sequence[str], repo: Path | None = None, *, dev_environment: bool = False
) -> list[str]:
    """Преобразует CLI-команду в список аргументов подпроцесса с выбором нужного интерпретатора Python."""
    head, *rest = cli_argv
    if head == "harness":
        return [sys.executable, str(BIN_HARNESS_PATH), *rest]
    if head == "python":
        if dev_environment and repo is not None:
            return [str(project_python(repo)), *rest]
        return [sys.executable, *rest]
    return list(cli_argv)


_HARNESS_CLI = "harness/bin/harness.py"
_COORDINATOR = "harness.orchestration.coordinator"
_COORDINATOR_SCRIPT = ".harness/orchestration/coordinator.py"

HARNESS_COMMANDS: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        "init",
        "Установить харнесс",
        ("harness", "init", "{repo}"),
        Reversibility.OVERWRITES_MANAGED_FILES,
        f"{_HARNESS_CLI}:cmd_init",
    ),
    CatalogEntry(
        "update",
        "Обновить харнесс",
        ("harness", "update", "{repo}"),
        Reversibility.OVERWRITES_MANAGED_FILES,
        f"{_HARNESS_CLI}:cmd_update",
    ),
    CatalogEntry(
        "diff",
        "Дрейф управляемых файлов",
        ("harness", "diff", "{repo}"),
        Reversibility.REVERSIBLE,
        f"{_HARNESS_CLI}:cmd_diff",
    ),
    CatalogEntry(
        "adopt",
        "Внедрить харнесс в существующий проект",
        ("harness", "adopt", "{repo}"),
        Reversibility.OVERWRITES_MANAGED_FILES,
        f"{_HARNESS_CLI}:cmd_adopt",
    ),
    CatalogEntry(
        "registry",
        "Пересобрать реестр скиллов",
        ("harness", "registry", "{repo}"),
        Reversibility.REVERSIBLE,
        f"{_HARNESS_CLI}:cmd_registry",
    ),
    CatalogEntry(
        "lock-project-skills",
        "Зафиксировать скиллы проекта",
        ("harness", "lock-project-skills", "{repo}"),
        Reversibility.REVERSIBLE,
        f"{_HARNESS_CLI}:cmd_lock_project_skills",
    ),
    CatalogEntry(
        "list",
        "Список скиллов",
        ("harness", "list", "{repo}"),
        Reversibility.REVERSIBLE,
        f"{_HARNESS_CLI}:cmd_list",
    ),
    CatalogEntry(
        "health",
        "Health-отчёт",
        ("harness", "health", "{repo}"),
        Reversibility.REVERSIBLE,
        f"{_HARNESS_CLI}:cmd_health",
    ),
    CatalogEntry(
        "health-fix",
        "Health-отчёт с локальными фиксами",
        ("harness", "health", "{repo}", "--fix"),
        Reversibility.REVERSIBLE,
        f"{_HARNESS_CLI}:cmd_health",
    ),
    CatalogEntry(
        "health-online-fix",
        "Health-отчёт с онлайн-проверками и созданием лейблов",
        ("harness", "health", "{repo}", "--online", "--fix"),
        Reversibility.EXTERNAL_CHANGE,
        f"{_HARNESS_CLI}:cmd_health",
    ),
    CatalogEntry(
        "cleanup",
        "План очистки .harness",
        ("harness", "cleanup", "{repo}"),
        Reversibility.REVERSIBLE,
        f"{_HARNESS_CLI}:cmd_cleanup",
    ),
    CatalogEntry(
        "cleanup-apply",
        "Очистить .harness (soft)",
        ("harness", "cleanup", "{repo}", "--apply"),
        Reversibility.DELETES_LOCAL_DATA,
        f"{_HARNESS_CLI}:cmd_cleanup",
    ),
    CatalogEntry(
        "cleanup-hard-apply",
        "Очистить .harness (hard)",
        (
            "harness",
            "cleanup",
            "{repo}",
            "--mode",
            "hard",
            "--apply",
            "--confirm",
            "HARD",
        ),
        Reversibility.DELETES_LOCAL_DATA,
        f"{_HARNESS_CLI}:cmd_cleanup",
        typed_confirmation="HARD",
    ),
    CatalogEntry(
        "repo-map",
        "Построить Repo Map для HEAD",
        (
            "python",
            ".harness/repo_map/repo_map.py",
            "--repo",
            "{repo}",
            "--commit",
            "HEAD",
        ),
        Reversibility.REVERSIBLE,
        "harness/repo_map/repo_map.py:main",
    ),
    CatalogEntry(
        "parser-bundle",
        "Собрать parser bundle из wheelhouse",
        (
            "python",
            "scripts/build_parser_bundle.py",
            "--wheelhouse",
            "{wheelhouse}",
            "--out",
            ".harness/.sandboxes/cache/repo_map/parser_bundle/registry",
        ),
        Reversibility.REVERSIBLE,
        "scripts/build_parser_bundle.py:main",
    ),
    CatalogEntry(
        "verify",
        "Полная проверка (verify)",
        ("python", "scripts/verify.py"),
        Reversibility.REVERSIBLE,
        "scripts/verify.py:main",
        dev_environment=True,
    ),
    CatalogEntry(
        "worktree-remove",
        "Удалить worktree",
        ("git", "-C", "{repo}", "worktree", "remove", "{worktree}"),
        Reversibility.DELETES_LOCAL_DATA,
        "git:worktree remove",
    ),
    CatalogEntry(
        "ledger-migrate",
        "Мигрировать ledger",
        ("python", _COORDINATOR_SCRIPT, "--repo", "{repo}", "ledger", "migrate"),
        Reversibility.REVERSIBLE,
        f"{_COORDINATOR}:migrate_ledger",
    ),
    CatalogEntry(
        "ledger-clean",
        "Удалить осиротевшие записи ledger",
        ("python", _COORDINATOR_SCRIPT, "--repo", "{repo}", "ledger", "clean"),
        Reversibility.DELETES_LOCAL_DATA,
        f"{_COORDINATOR}:clean_ledger",
    ),
    CatalogEntry(
        "ledger-reset",
        "Сбросить ledger на пустое поколение",
        (
            "python",
            _COORDINATOR_SCRIPT,
            "--repo",
            "{repo}",
            "ledger",
            "reset",
            "--confirm",
            "RESET",
        ),
        Reversibility.DELETES_LOCAL_DATA,
        f"{_COORDINATOR}:reset_ledger",
        typed_confirmation="RESET",
    ),
)
