"""harness.console.coordinator_catalog: каталог команд раздела Orchestration, интроспектируемый
из реального `coordinator.parser()`. Тесты проверяют синхронизацию дескрипторов команд с парсером,
соответствие классов обратимости архитектуре и точную диспетчеризацию CLI-аргументов в обработчики."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pytest

from harness.console.coordinator_catalog import (
    COORDINATOR_COMMANDS,
    CoordinatorCommand,
    subparser,
)
from harness.console.catalog import Reversibility
from harness.orchestration import coordinator

ROOT = Path(__file__).resolve().parents[2]
REPO = Path("/work/project")
ENTRIES = {entry.key: entry for entry in COORDINATOR_COMMANDS}

# Minimal values that satisfy every required (and required+repeatable) field of each target, used
# to feed `cli_argv` back into the real parser in the handler-identity test below.
MINIMAL_VALUES = {
    "batch-create": {
        "ticket": "#1",
        "branch": "feature/x",
        "worktree": "wt",
        "definition_of_done": "done",
        "prohibited_change": "nothing",
    },
    "batch-approve": {"batch": "batch-1", "approved_by": "dev", "approved_at": "now"},
    "batch-abandon": {
        "batch": "batch-1",
        "approved_by": "dev",
        "approved_at": "now",
        "reason": "why",
    },
    "batch-decide": {
        "batch": "batch-1",
        "decision": "accept",
        "approved_by": "dev",
        "approved_at": "now",
    },
    "batch-decision-packet": {"batch": "batch-1"},
    "dispatch-create": {"batch": "batch-1"},
    "dispatch-cancel": {
        "dispatch": "dispatch-1",
        "approved_by": "dev",
        "approved_at": "now",
        "reason": "why",
    },
    "dispatch-send": {"dispatch": "dispatch-1"},
    "risk-assess": {
        "batch": "batch-1",
        "candidate_commit": "abc123",
        "changed_file": "a.py",
    },
    "context-package-register": {"batch": "batch-1", "candidate_commit": "abc123"},
    "batch-list": {"open": "1"},
    "batch-resume": {"batch": "batch-1", "reason": "why"},
    "batch-attention-check": {"batch": "batch-1"},
    "batch-attention-resolve": {
        "batch": "batch-1",
        "note": "handled",
        "approved_by": "dev",
        "approved_at": "now",
    },
    "dispatch-status": {},
    "qa-status": {},
    "ledger-status": {},
}

EXPECTED_HANDLER = {
    "batch-create": "create_batch",
    "batch-approve": "approve_batch",
    "batch-abandon": "abandon_batch",
    "batch-decide": "decide_batch",
    "batch-decision-packet": "decision_packet",
    "dispatch-create": "create_dispatch",
    "dispatch-cancel": "cancel_dispatch",
    "dispatch-send": "send_dispatch",
    "risk-assess": "assess_risk",
    "context-package-register": "register_context_package",
    "batch-list": "list_batches",
    "batch-resume": "resume_batch",
    "batch-attention-check": "attention_check",
    "batch-attention-resolve": "attention_resolve",
    "dispatch-status": "dispatch_status",
    "qa-status": "qa_status",
    "ledger-status": "ledger_status",
}

EXPECTED_REVERSIBILITY = {
    "batch-create": Reversibility.REVERSIBLE,
    "batch-approve": Reversibility.TERMINAL_COORDINATOR_ACTION,
    "batch-abandon": Reversibility.TERMINAL_COORDINATOR_ACTION,
    "batch-decide": Reversibility.TERMINAL_COORDINATOR_ACTION,
    "batch-decision-packet": Reversibility.REVERSIBLE,
    "dispatch-create": Reversibility.TERMINAL_COORDINATOR_ACTION,
    "dispatch-cancel": Reversibility.TERMINAL_COORDINATOR_ACTION,
    "dispatch-send": Reversibility.EXTERNAL_CHANGE,
    "risk-assess": Reversibility.REVERSIBLE,
    "context-package-register": Reversibility.REVERSIBLE,
    "batch-list": Reversibility.REVERSIBLE,
    "batch-resume": Reversibility.REVERSIBLE,
    "batch-attention-check": Reversibility.REVERSIBLE,
    "batch-attention-resolve": Reversibility.TERMINAL_COORDINATOR_ACTION,
    "dispatch-status": Reversibility.REVERSIBLE,
    "qa-status": Reversibility.REVERSIBLE,
    "ledger-status": Reversibility.REVERSIBLE,
}


def test_catalog_covers_exactly_the_operator_facing_coordinator_subcommands() -> None:
    """Проверить, что каталог содержит ровно все подкоманды координатора для оператора."""
    assert set(ENTRIES) == set(EXPECTED_REVERSIBILITY)
    assert len(ENTRIES) == len(COORDINATOR_COMMANDS)


def test_reversibility_matches_the_approved_architect_design() -> None:
    """Проверить соответствие классов обратимости команд утверждённому дизайну архитектора."""
    assert {
        key: entry.reversibility for key, entry in ENTRIES.items()
    } == EXPECTED_REVERSIBILITY


def _live_actions(entry: CoordinatorCommand) -> list[argparse.Action]:
    """Извлечь список действий парсера аргументов для указанной команды координатора."""
    return list(subparser(coordinator.parser(), entry.path)._actions)  # noqa: SLF001


@pytest.mark.parametrize("entry", COORDINATOR_COMMANDS, ids=lambda e: e.key)
def test_required_fields_match_the_live_parser(entry: CoordinatorCommand) -> None:
    """Проверить совпадение обязательных полей команды с реальным парсером аргументов."""
    live_required = {
        action.dest
        for action in _live_actions(entry)
        if getattr(action, "required", False)
    }
    assert {field.dest for field in entry.fields if field.required} == live_required


@pytest.mark.parametrize("entry", COORDINATOR_COMMANDS, ids=lambda e: e.key)
def test_choices_and_repeatable_fields_match_the_live_parser(
    entry: CoordinatorCommand,
) -> None:
    """Проверить совпадение допустимых значений и повторяемых полей с парсером аргументов."""
    live_by_dest = {action.dest: action for action in _live_actions(entry)}
    for field in entry.fields:
        live = live_by_dest[field.dest]
        assert field.repeatable == isinstance(live, argparse._AppendAction)
        expected_choices = tuple(str(c) for c in live.choices) if live.choices else None
        assert field.choices == expected_choices


@pytest.mark.parametrize("entry", COORDINATOR_COMMANDS, ids=lambda e: e.key)
def test_cli_argv_round_trips_into_the_named_handler(entry: CoordinatorCommand) -> None:
    """Проверить, что сформированные аргументы командной строки попадают в ожидаемый обработчик."""
    argv = entry.cli_argv(REPO, MINIMAL_VALUES[entry.key])
    assert argv[:4] == [
        "python",
        ".harness/orchestration/coordinator.py",
        "--repo",
        str(REPO),
    ]
    args = coordinator.parser().parse_args(argv[4:])
    assert args.handler is getattr(coordinator, EXPECTED_HANDLER[entry.key])


def test_repeatable_field_serialises_one_flag_value_pair_per_non_empty_line() -> None:
    """Проверить сериализацию повторяемого поля в пары флаг-значение для каждой непустой строки."""
    entry = ENTRIES["risk-assess"]
    values = {
        "batch": "batch-1",
        "candidate_commit": "abc123",
        "changed_file": "a.py\n\n  b.py  \n",
    }
    argv = entry.cli_argv(REPO, values)
    assert argv.count("--changed-file") == 2
    assert argv[argv.index("--changed-file") + 1] == "a.py"
    assert argv[-1] == "b.py"


def test_coordinator_catalog_imports_without_textual() -> None:
    """Проверить, что каталог команд координатора импортируется без установленного textual."""
    code = "import sys; sys.modules['textual'] = None; import harness.console.coordinator_catalog"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_value_less_flag_is_passed_alone_only_when_set() -> None:
    """Проверить, что булев флаг без значения передаётся одиночным аргументом только при установке."""
    entry = ENTRIES["batch-list"]
    open_field = next(field for field in entry.fields if field.dest == "open")
    assert open_field.takes_value is False
    assert open_field.default is None
    assert entry.cli_argv(REPO, {"open": "1"})[-1] == "--open"
    assert "--open" not in entry.cli_argv(REPO, {"open": ""})
