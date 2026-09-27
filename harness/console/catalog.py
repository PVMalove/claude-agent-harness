"""The console's command catalog: plain stdlib data describing every command a console section
can run - display title, the CLI argv shown to the user as its equivalent, a reversibility class,
and the CLI function that argv dispatches to. Listed and tested without textual.

The console never reimplements a command: it runs the entry's CLI argv as a process (see
`process_argv`), so the CLI function named in `function` does the work with every guard it
already has (ledger locks, `--confirm RESET`, drift refusal). tests/test_console_catalog.py
proves each argv really dispatches to that function.
"""

from __future__ import annotations

import re
import shlex
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping, Sequence

from .launcher import BIN_HARNESS_PATH


class Reversibility(Enum):
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
    """`argv` is the CLI equivalent with `{repo}` and `{<input>}` placeholders; every placeholder
    other than `{repo}` is a value the console asks for before running. `function` is
    `<module or script path>:<name>` of the CLI function `argv` dispatches to. A
    `typed_confirmation` word must be typed literally before the command runs."""

    key: str
    title: str
    argv: tuple[str, ...]
    reversibility: Reversibility
    function: str
    typed_confirmation: str | None = None

    @property
    def needs_confirmation(self) -> bool:
        return self.reversibility is not Reversibility.REVERSIBLE

    @property
    def inputs(self) -> tuple[str, ...]:
        names: list[str] = []
        for part in self.argv:
            for name in _PLACEHOLDER.findall(part):
                if name != _REPO and name not in names:
                    names.append(name)
        return tuple(names)

    def cli_argv(self, repo: Path, values: Mapping[str, str] | None = None) -> list[str]:
        """The CLI equivalent with placeholders filled; an input not given stays `<name>`."""
        known = {_REPO: str(repo), **(values or {})}
        return [
            _PLACEHOLDER.sub(lambda match: known.get(match[1], f"<{match[1]}>"), part)
            for part in self.argv
        ]

    def cli_line(self, repo: Path, values: Mapping[str, str] | None = None) -> str:
        return shlex.join(self.cli_argv(repo, values))


def harness_python(repo: Path | None) -> str:
    """The interpreter for a `python ...` CLI equivalent: the repository's own `.harness/.venv`
    when it exists (the environment `make verify` uses - it has pytest, mypy and the rest of the
    dev group), else the interpreter the console runs under. The console itself runs in a one-off
    `uv run --with textual` environment that carries textual only."""
    if repo is not None:
        for candidate in (
            repo / ".harness" / ".venv" / "bin" / "python",
            repo / ".harness" / ".venv" / "Scripts" / "python.exe",
        ):
            if candidate.is_file():
                return str(candidate)
    return sys.executable


def process_argv(cli_argv: Sequence[str], repo: Path | None = None) -> list[str]:
    """The process that runs a CLI equivalent: `harness` is this harness checkout's CLI and
    `python` is `harness_python(repo)`."""
    head, *rest = cli_argv
    if head == "harness":
        return [sys.executable, str(BIN_HARNESS_PATH), *rest]
    if head == "python":
        return [harness_python(repo), *rest]
    return list(cli_argv)


_HARNESS_CLI = "harness/bin/harness"
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
        ("harness", "cleanup", "{repo}", "--mode", "hard", "--apply", "--confirm", "HARD"),
        Reversibility.DELETES_LOCAL_DATA,
        f"{_HARNESS_CLI}:cmd_cleanup",
        typed_confirmation="HARD",
    ),
    CatalogEntry(
        "repo-map",
        "Построить Repo Map для HEAD",
        ("python", ".harness/repo_map/repo_map.py", "--repo", "{repo}", "--commit", "HEAD"),
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
        ("python", _COORDINATOR_SCRIPT, "--repo", "{repo}", "ledger", "reset", "--confirm", "RESET"),
        Reversibility.DELETES_LOCAL_DATA,
        f"{_COORDINATOR}:reset_ledger",
        typed_confirmation="RESET",
    ),
)
