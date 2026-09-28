"""Каталог команд раздела Orchestration: декларативные структуры данных стандартной библиотеки,
полученные интроспекцией активного парсера аргументов координатора (`harness.orchestration.coordinator.parser()`)
и охватывающие доступные оператору подкоманды координатора, перечисленные в `_TARGETS` (batch, decide,
decision packet, dispatch, qa status, risk, context-package, ledger status).

В отличие от `harness/console/catalog.py`, где `CatalogEntry.argv` задан вручную, каждый кортеж
`CoordinatorCommand.fields` строится обходом реального дерева `argparse.ArgumentParser` CLI координатора.
Поэтому обязательные аргументы, ограничения `choices=`, действие `append` или флаги без значений
подхватываются автоматически без ручного редактирования данного модуля.
Тест `tests/console/test_console_coordinator_catalog.py` проверяет синхронизацию с CLI (тест на дрейф). Консоль
не дублирует логику координатора: она запускает тот же процесс
`python .harness/orchestration/coordinator.py --repo {repo} <путь подкоманды> ...` (см. `process_argv`).
Класс `Reversibility` и причины `CONFIRMATION_REASONS` переиспользуются из `.catalog`.
"""

from __future__ import annotations

import argparse
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, cast

from harness.orchestration import coordinator

from .catalog import CONFIRMATION_REASONS as CONFIRMATION_REASONS
from .catalog import Reversibility
from .catalog import process_argv as process_argv

_COORDINATOR_SCRIPT = ".harness/orchestration/coordinator.py"

# Injected by `coordinator_cli._common()` on every subparser, plus the auto-added `-h/--help`: the
# console never asks for these, so they are never turned into a `CoordinatorField`.
_SKIPPED_DESTS = frozenset({"help", "repo", "state_dir"})


@dataclass(frozen=True)
class _Target:
    """Целевая подкоманда координатора: путь CLI, заголовок, группа меню и класс обратимости."""

    key: str
    path: tuple[str, ...]
    title: str
    group: str
    reversibility: Reversibility


_REVERSIBLE = Reversibility.REVERSIBLE
_TERMINAL = Reversibility.TERMINAL_COORDINATOR_ACTION

# Operator-facing subcommands only. Worker-side commands (dispatch heartbeat/self-report/checkpoint,
# report submit, qa run/evidence, ...) are sent by the dispatched role itself, never from the console.
# Terminal coordinator decisions and cancellation ask for confirmation, an external transport change
# asks too (a different reason), and everything else stays reversible.
_TARGETS: tuple[_Target, ...] = (
    _Target("batch-list", ("batch", "list"), "Список batch", "batch", _REVERSIBLE),
    _Target("batch-create", ("batch", "create"), "Создать batch", "batch", _REVERSIBLE),
    _Target(
        "batch-approve", ("batch", "approve"), "Утвердить batch", "batch", _TERMINAL
    ),
    _Target(
        "batch-abandon", ("batch", "abandon"), "Отказаться от batch", "batch", _TERMINAL
    ),
    _Target(
        "batch-resume", ("batch", "resume"), "Возобновить batch", "batch", _REVERSIBLE
    ),
    _Target(
        "batch-attention-check",
        ("batch", "attention", "check"),
        "Проверить, требует ли batch внимания",
        "batch",
        _REVERSIBLE,
    ),
    _Target(
        "batch-attention-resolve",
        ("batch", "attention", "resolve"),
        "Снять сигнал внимания с batch",
        "batch",
        _TERMINAL,
    ),
    _Target(
        "batch-decide", ("batch", "decide"), "Решение по batch", "decide", _TERMINAL
    ),
    _Target(
        "batch-decision-packet",
        ("batch", "decision-packet"),
        "Пакет решения",
        "packet",
        _REVERSIBLE,
    ),
    _Target(
        "dispatch-status",
        ("dispatch", "status"),
        "Статус dispatch",
        "dispatch",
        _REVERSIBLE,
    ),
    _Target(
        "dispatch-create",
        ("dispatch", "create"),
        "Создать dispatch",
        "dispatch",
        _TERMINAL,
    ),
    _Target(
        "dispatch-cancel",
        ("dispatch", "cancel"),
        "Отменить dispatch",
        "dispatch",
        _TERMINAL,
    ),
    _Target(
        "dispatch-send",
        ("dispatch", "send"),
        "Отправить dispatch",
        "dispatch",
        Reversibility.EXTERNAL_CHANGE,
    ),
    _Target("qa-status", ("qa", "status"), "Статус QA lane", "qa", _REVERSIBLE),
    _Target("risk-assess", ("risk", "assess"), "Оценить риск", "risk", _REVERSIBLE),
    _Target(
        "context-package-register",
        ("context-package", "register"),
        "Зарегистрировать Context Package",
        "context-package",
        _REVERSIBLE,
    ),
    _Target(
        "ledger-status", ("ledger", "status"), "Состояние ledger", "ledger", _REVERSIBLE
    ),
)


@dataclass(frozen=True)
class CoordinatorField:
    """Аргумент подкоманды координатора, объявленный в argparse."""

    dest: str
    flag: str
    required: bool
    repeatable: bool
    choices: tuple[str, ...] | None
    help: str
    default: str | None
    # False for a value-less flag such as `--open` (store_true): it is passed alone when set.
    takes_value: bool = True


@dataclass(frozen=True)
class CoordinatorCommand:
    """Подкоманда координатора, доступная для запуска из раздела Orchestration консоли."""

    key: str
    title: str
    group: str
    path: tuple[str, ...]
    reversibility: Reversibility
    fields: tuple[CoordinatorField, ...]

    @property
    def needs_confirmation(self) -> bool:
        """Определяет, требует ли команда координатора подтверждения перед выполнением."""
        return self.reversibility is not Reversibility.REVERSIBLE

    def cli_argv(
        self, repo: Path, values: Mapping[str, str] | None = None
    ) -> list[str]:
        """Формирует список аргументов CLI команды с подстановкой переданных значений полей."""
        filled = values or {}
        argv = ["python", _COORDINATOR_SCRIPT, "--repo", str(repo), *self.path]
        for field in self.fields:
            raw = filled.get(field.dest, "")
            if not field.takes_value:
                if raw.strip():
                    argv.append(field.flag)
            elif field.repeatable:
                for line in raw.splitlines():
                    line = line.strip()
                    if line:
                        argv.extend([field.flag, line])
            else:
                value = raw.strip()
                if value:
                    argv.extend([field.flag, value])
        return argv

    def cli_line(self, repo: Path, values: Mapping[str, str] | None = None) -> str:
        """Возвращает экранированную строковую команду CLI для отображения в интерфейсе."""
        return shlex.join(self.cli_argv(repo, values))


def subparser(
    root: argparse.ArgumentParser, path: tuple[str, ...]
) -> argparse.ArgumentParser:
    """Находит вложенный парсер аргументов координатора по цепочке имён подкоманд в дереве argparse."""
    parser = root
    for name in path:
        for action in parser._actions:  # noqa: SLF001 - introspecting argparse's own tree by design
            if (
                isinstance(action, argparse._SubParsersAction)
                and name in action.choices
            ):
                parser = cast(argparse.ArgumentParser, action.choices[name])
                break
        else:
            raise KeyError(f"no such coordinator subcommand: {' '.join(path)}")
    return parser


def _fields(sub_parser: argparse.ArgumentParser) -> tuple[CoordinatorField, ...]:
    """Извлекает список полей параметров из парсера подкоманды argparse."""
    fields: list[CoordinatorField] = []
    for action in sub_parser._actions:  # noqa: SLF001
        if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
            continue
        if action.dest in _SKIPPED_DESTS:
            continue
        flag = action.option_strings[-1] if action.option_strings else action.dest
        takes_value = action.nargs != 0
        default = None
        if (
            takes_value
            and action.default is not None
            and action.default is not argparse.SUPPRESS
        ):
            default = str(action.default)
        choices = (
            tuple(str(choice) for choice in action.choices) if action.choices else None
        )
        fields.append(
            CoordinatorField(
                dest=action.dest,
                flag=flag,
                required=bool(action.required),
                repeatable=isinstance(action, argparse._AppendAction),
                choices=choices,
                help=action.help or "",
                default=default,
                takes_value=takes_value,
            )
        )
    return tuple(fields)


def _build_commands() -> tuple[CoordinatorCommand, ...]:
    """Строит полный кортеж доступных команд координатора на основе дерева аргументов."""
    root = coordinator.parser()
    commands: list[CoordinatorCommand] = []
    for target in _TARGETS:
        commands.append(
            CoordinatorCommand(
                key=target.key,
                title=target.title,
                group=target.group,
                path=target.path,
                reversibility=target.reversibility,
                fields=_fields(subparser(root, target.path)),
            )
        )
    return tuple(commands)


COORDINATOR_COMMANDS: tuple[CoordinatorCommand, ...] = _build_commands()
