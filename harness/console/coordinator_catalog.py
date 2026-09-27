"""The Orchestration section's command catalog: plain stdlib data introspected from the live
coordinator argument parser (`harness.orchestration.coordinator.parser()`), covering the nine
in-scope coordinator subcommands (batch create/approve/abandon/decide/decision-packet, dispatch
create/cancel/send, risk assess, context-package register).

Unlike `harness/console/catalog.py`, whose `CatalogEntry.argv` is hand-written, every
`CoordinatorCommand.fields` tuple here is built by walking the real `argparse.ArgumentParser` the
coordinator CLI itself builds, so a required argument, a `choices=` restriction or an `append`
action added to the coordinator's parser is reflected here without hand-editing this module.
`tests/test_console_coordinator_catalog.py` proves this stays in sync (a drift test). The console
never reimplements coordinator logic: it runs the same `python .harness/orchestration/coordinator.py
--repo {repo} <group> <sub> ...` CLI as a process (see `process_argv`), exactly the convention
`harness/console/catalog.py` already uses for the `ledger-*` entries.

`Reversibility` and `CONFIRMATION_REASONS` are reused from `.catalog`, not copied."""

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

# (key, cli_group, cli_sub, title, ui_group) for the nine in-scope coordinator subcommands. The
# primary name is used even where the parser also registers an alias (`batch create`/`plan`,
# `dispatch create`/`approve`): `argparse` shares one subparser instance across every alias, so
# looking it up by either name returns the identical object.
_TARGETS: tuple[tuple[str, str, str, str, str], ...] = (
    ("batch-create", "batch", "create", "Создать batch", "batch"),
    ("batch-approve", "batch", "approve", "Утвердить batch", "batch"),
    ("batch-abandon", "batch", "abandon", "Отказаться от batch", "batch"),
    ("batch-decide", "batch", "decide", "Решение по batch", "decide"),
    ("batch-decision-packet", "batch", "decision-packet", "Пакет решения", "packet"),
    ("dispatch-create", "dispatch", "create", "Создать dispatch", "dispatch"),
    ("dispatch-cancel", "dispatch", "cancel", "Отменить dispatch", "dispatch"),
    ("dispatch-send", "dispatch", "send", "Отправить dispatch", "dispatch"),
    ("risk-assess", "risk", "assess", "Оценить риск", "risk"),
    (
        "context-package-register",
        "context-package",
        "register",
        "Зарегистрировать Context Package",
        "context-package",
    ),
)

# Confirmation classes for the nine in-scope subcommands (approved architect design for #350):
# terminal coordinator decisions and cancellation ask for confirmation, an external transport
# change asks too (a different reason), and everything else stays reversible.
_REVERSIBILITY: dict[tuple[str, str], Reversibility] = {
    ("batch", "approve"): Reversibility.TERMINAL_COORDINATOR_ACTION,
    ("batch", "abandon"): Reversibility.TERMINAL_COORDINATOR_ACTION,
    ("batch", "decide"): Reversibility.TERMINAL_COORDINATOR_ACTION,
    ("dispatch", "create"): Reversibility.TERMINAL_COORDINATOR_ACTION,
    ("dispatch", "cancel"): Reversibility.TERMINAL_COORDINATOR_ACTION,
    ("dispatch", "send"): Reversibility.EXTERNAL_CHANGE,
    ("batch", "create"): Reversibility.REVERSIBLE,
    ("batch", "decision-packet"): Reversibility.REVERSIBLE,
    ("risk", "assess"): Reversibility.REVERSIBLE,
    ("context-package", "register"): Reversibility.REVERSIBLE,
}


@dataclass(frozen=True)
class CoordinatorField:
    """One argument of a coordinator subcommand, as `argparse` declared it."""

    dest: str
    flag: str
    required: bool
    repeatable: bool
    choices: tuple[str, ...] | None
    help: str
    default: str | None


@dataclass(frozen=True)
class CoordinatorCommand:
    """A coordinator subcommand the console's Orchestration section can run. `path` is the
    `(group, sub)` pair passed to the coordinator CLI, e.g. `("batch", "decide")`."""

    key: str
    title: str
    group: str
    path: tuple[str, str]
    reversibility: Reversibility
    fields: tuple[CoordinatorField, ...]

    @property
    def needs_confirmation(self) -> bool:
        return self.reversibility is not Reversibility.REVERSIBLE

    def cli_argv(self, repo: Path, values: Mapping[str, str] | None = None) -> list[str]:
        """The CLI equivalent for this command with `values` (keyed by field `dest`) filled in.
        A repeatable field's value is a multi-line string; each non-empty stripped line becomes
        its own `--flag value` pair. A field left out, or blank, contributes nothing."""
        filled = values or {}
        argv = ["python", _COORDINATOR_SCRIPT, "--repo", str(repo), *self.path]
        for field in self.fields:
            raw = filled.get(field.dest, "")
            if field.repeatable:
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
        return shlex.join(self.cli_argv(repo, values))


def _subparser(root: argparse.ArgumentParser, group: str, sub: str) -> argparse.ArgumentParser:
    for action in root._actions:  # noqa: SLF001 - introspecting argparse's own tree by design
        if isinstance(action, argparse._SubParsersAction) and group in action.choices:
            group_parser = action.choices[group]
            for inner in group_parser._actions:  # noqa: SLF001
                if isinstance(inner, argparse._SubParsersAction) and sub in inner.choices:
                    return cast(argparse.ArgumentParser, inner.choices[sub])
    raise KeyError(f"no such coordinator subcommand: {group} {sub}")


def _fields(sub_parser: argparse.ArgumentParser) -> tuple[CoordinatorField, ...]:
    fields: list[CoordinatorField] = []
    for action in sub_parser._actions:  # noqa: SLF001
        if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
            continue
        if action.dest in _SKIPPED_DESTS:
            continue
        flag = action.option_strings[-1] if action.option_strings else action.dest
        default = None
        if action.default is not None and action.default is not argparse.SUPPRESS:
            default = str(action.default)
        choices = tuple(str(choice) for choice in action.choices) if action.choices else None
        fields.append(
            CoordinatorField(
                dest=action.dest,
                flag=flag,
                required=bool(action.required),
                repeatable=isinstance(action, argparse._AppendAction),
                choices=choices,
                help=action.help or "",
                default=default,
            )
        )
    return tuple(fields)


def _build_commands() -> tuple[CoordinatorCommand, ...]:
    root = coordinator.parser()
    commands: list[CoordinatorCommand] = []
    for key, group, sub, title, ui_group in _TARGETS:
        sub_parser = _subparser(root, group, sub)
        commands.append(
            CoordinatorCommand(
                key=key,
                title=title,
                group=ui_group,
                path=(group, sub),
                reversibility=_REVERSIBILITY[(group, sub)],
                fields=_fields(sub_parser),
            )
        )
    return tuple(commands)


COORDINATOR_COMMANDS: tuple[CoordinatorCommand, ...] = _build_commands()
