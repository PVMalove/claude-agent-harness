"""Каталог команд консоли: декларативные структуры данных стандартной библиотеки, описывающие
каждую команду разделов консоли — отображаемый заголовок, эквивалентные аргументы CLI, класс
обратимости и функция CLI, в которую диспетчеризуется вызов. Описан и протестирован без зависимости от textual.

Консоль не реализует логику команд заново: она запускает аргументы CLI записи как подпроцесс (см.
`process_argv`), благодаря чему функция CLI, указанная в `function`, выполняет работу со всеми
существующими проверками (блокировки леджера, `--confirm RESET`, отказ при дрейфе файлов).
Тест tests/console/test_console_catalog.py проверяет, что каждый набор аргументов действительно передаётся в эту функцию.
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
    DOWNLOADS_FILES = "downloads-files"


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
        "Команда перезаписывает файлы харнесса в проекте (управляемые или seed) — незакоммиченные правки в них пропадут."
    ),
    Reversibility.DOWNLOADS_FILES: (
        "Команда скачивает файлы из сети (PyPI, нужны доступ и время) и заменяет локальный bundle парсеров проекта."
    ),
}

# Short presentation labels; the confirmation classes and their reasons remain authoritative.
REVERSIBILITY_LABELS = {
    Reversibility.REVERSIBLE: "✔ обратимо",
    Reversibility.DELETES_LOCAL_DATA: "✖ удаление",
    Reversibility.TERMINAL_COORDINATOR_ACTION: "⚠ решение",
    Reversibility.EXTERNAL_CHANGE: "⚠ внешнее",
    Reversibility.OVERWRITES_MANAGED_FILES: "⚠ перезапись",
    Reversibility.DOWNLOADS_FILES: "⚠ загрузка",
}

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
_REPO = "repo"


@dataclass(frozen=True)
class CatalogEntry:
    """Запись команды и её server-side safety metadata.

    ``reversibility`` describes the confirmation UX. ``mutates`` is a separate execution
    boundary: a command can be reversible while still writing a cache, ledger record, or
    generated project file, so those commands must still be serialized by the console server.
    """

    key: str
    title: str
    argv: tuple[str, ...]
    reversibility: Reversibility
    function: str
    typed_confirmation: str | None = None
    # Runs `python` under the repository's dev environment (`.harness/.venv`: pytest, mypy, ...)
    # instead of the console's own interpreter - only for this repository's verification script.
    dev_environment: bool = False
    # True when the command can write project state, including reversible caches or generated files.
    mutates: bool = False

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

    def accepts(self, values: Mapping[str, str]) -> bool:
        """Проверяет, что значения заданы ровно для входных полей команды и ни одно не пустое."""
        return set(values) == set(self.inputs) and all(
            value.strip() for value in values.values()
        )

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


def seed_update_entry(paths: Sequence[str]) -> CatalogEntry:
    """`harness update` that overwrites only the chosen seed files (`--force-seed-file` per path)."""
    flags = tuple(part for path in paths for part in ("--force-seed-file", path))
    return CatalogEntry(
        "update-seed-selected",
        "Обновить харнесс с перезаписью выбранных seed-файлов",
        ("harness", "update", "{repo}", *flags),
        Reversibility.OVERWRITES_MANAGED_FILES,
        "harness/bin/harness.py:cmd_update",
        mutates=True,
    )


def parsers_install_entry(languages: Sequence[str]) -> CatalogEntry:
    """`harness parsers install` for the chosen languages (`--language` per language)."""
    flags = tuple(part for key in languages for part in ("--language", key))
    return CatalogEntry(
        "parsers-install",
        "Установить парсеры для выбранных языков",
        ("harness", "parsers", "install", "{repo}", *flags),
        Reversibility.DOWNLOADS_FILES,
        "harness/bin/harness.py:cmd_parsers",
        mutates=True,
    )


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
        mutates=True,
    ),
    CatalogEntry(
        "update",
        "Обновить харнесс",
        ("harness", "update", "{repo}"),
        Reversibility.OVERWRITES_MANAGED_FILES,
        f"{_HARNESS_CLI}:cmd_update",
        mutates=True,
    ),
    CatalogEntry(
        "update-force-managed",
        "Обновить харнесс с перезаписью управляемых файлов",
        ("harness", "update", "{repo}", "--force-managed-files"),
        Reversibility.OVERWRITES_MANAGED_FILES,
        f"{_HARNESS_CLI}:cmd_update",
        mutates=True,
    ),
    CatalogEntry(
        "update-force-seed",
        "Обновить харнесс с перезаписью seed-файлов",
        ("harness", "update", "{repo}", "--force-seed-files"),
        Reversibility.OVERWRITES_MANAGED_FILES,
        f"{_HARNESS_CLI}:cmd_update",
        mutates=True,
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
        mutates=True,
    ),
    CatalogEntry(
        "cleanup",
        "Очистка .harness (soft): план",
        ("harness", "cleanup", "{repo}"),
        Reversibility.REVERSIBLE,
        f"{_HARNESS_CLI}:cmd_cleanup",
    ),
    CatalogEntry(
        "cleanup-hard",
        "Очистка .harness (hard): план",
        ("harness", "cleanup", "{repo}", "--mode", "hard"),
        Reversibility.REVERSIBLE,
        f"{_HARNESS_CLI}:cmd_cleanup",
    ),
    CatalogEntry(
        "cleanup-apply",
        "Очистка .harness (soft): применить",
        ("harness", "cleanup", "{repo}", "--apply"),
        Reversibility.DELETES_LOCAL_DATA,
        f"{_HARNESS_CLI}:cmd_cleanup",
        mutates=True,
    ),
    CatalogEntry(
        "cleanup-hard-apply",
        "Очистка .harness (hard): применить",
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
        mutates=True,
    ),
    CatalogEntry(
        "uninstall",
        "Удаление харнесса из проекта: план",
        ("harness", "uninstall", "{repo}"),
        Reversibility.REVERSIBLE,
        f"{_HARNESS_CLI}:cmd_uninstall",
    ),
    CatalogEntry(
        "uninstall-apply",
        "Удалить харнесс из проекта (с резервной копией изменённых файлов)",
        ("harness", "uninstall", "{repo}", "--apply", "--confirm", "UNINSTALL"),
        Reversibility.DELETES_LOCAL_DATA,
        f"{_HARNESS_CLI}:cmd_uninstall",
        typed_confirmation="UNINSTALL",
        mutates=True,
    ),
    CatalogEntry(
        "registry",
        "Пересобрать реестр скиллов",
        ("harness", "registry", "{repo}"),
        Reversibility.REVERSIBLE,
        f"{_HARNESS_CLI}:cmd_registry",
        mutates=True,
    ),
    CatalogEntry(
        "lock-project-skills",
        "Зафиксировать скиллы проекта",
        ("harness", "lock-project-skills", "{repo}"),
        Reversibility.REVERSIBLE,
        f"{_HARNESS_CLI}:cmd_lock_project_skills",
        mutates=True,
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
        mutates=True,
    ),
    CatalogEntry(
        "health-online-fix",
        "Health-отчёт с онлайн-проверками и созданием лейблов",
        ("harness", "health", "{repo}", "--online", "--fix"),
        Reversibility.EXTERNAL_CHANGE,
        f"{_HARNESS_CLI}:cmd_health",
        mutates=True,
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
        mutates=True,
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
        mutates=True,
    ),
    CatalogEntry(
        "verify",
        "Полная проверка (verify)",
        ("python", "scripts/verify.py"),
        Reversibility.REVERSIBLE,
        "scripts/verify.py:main",
        dev_environment=True,
        mutates=True,
    ),
    CatalogEntry(
        "worktree-remove",
        "Удалить worktree",
        ("harness", "worktree-remove", "{repo}", "{worktree}"),
        Reversibility.DELETES_LOCAL_DATA,
        f"{_HARNESS_CLI}:cmd_worktree_remove",
        mutates=True,
    ),
    CatalogEntry(
        "ledger-migrate",
        "Мигрировать ledger",
        ("python", _COORDINATOR_SCRIPT, "--repo", "{repo}", "ledger", "migrate"),
        Reversibility.REVERSIBLE,
        f"{_COORDINATOR}:migrate_ledger",
        mutates=True,
    ),
    CatalogEntry(
        "ledger-clean",
        "Удалить осиротевшие записи ledger",
        ("python", _COORDINATOR_SCRIPT, "--repo", "{repo}", "ledger", "clean"),
        Reversibility.DELETES_LOCAL_DATA,
        f"{_COORDINATOR}:clean_ledger",
        mutates=True,
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
        mutates=True,
    ),
)

# The Repo Map section's own commands; they are not listed in the Harness menu.
MEMORY_COMMANDS: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        "memory-build",
        "Инициализировать память проекта",
        ("harness", "memory", "build", "{repo}"),
        Reversibility.REVERSIBLE,
        f"{_HARNESS_CLI}:cmd_memory",
        mutates=True,
    ),
    CatalogEntry(
        "memory-sync",
        "Синхронизировать память проекта",
        ("harness", "memory", "sync", "{repo}"),
        Reversibility.REVERSIBLE,
        f"{_HARNESS_CLI}:cmd_memory",
        mutates=True,
    ),
    CatalogEntry(
        "memory-rebuild",
        "Пересобрать память проекта с нуля",
        ("harness", "memory", "rebuild", "{repo}"),
        Reversibility.REVERSIBLE,
        f"{_HARNESS_CLI}:cmd_memory",
        mutates=True,
    ),
)
