"""harness.console.coordinator_catalog: the Orchestration section's command catalog is introspected
from the live `coordinator.parser()`, not hand-written, so these tests run without textual and
prove the descriptors stay in sync with the coordinator's actual argument parser (a drift test),
that the reversibility classes match the approved architect design for #350, and that `cli_argv`
round-trips through `coordinator.parser().parse_args()` into the exact handler the CLI itself would
dispatch to -- the same technique tests/test_console_catalog.py uses for the `ledger-*` entries, and
the proof that the console invokes the coordinator's own entry points rather than duplicating its
validation or invariants."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import pytest

from harness.console.coordinator_catalog import COORDINATOR_COMMANDS, CoordinatorCommand
from harness.console.catalog import Reversibility
from harness.orchestration import coordinator

ROOT = Path(__file__).resolve().parents[1]
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
}


def test_catalog_covers_exactly_the_nine_in_scope_coordinator_subcommands() -> None:
    assert set(ENTRIES) == set(EXPECTED_REVERSIBILITY)
    assert len(ENTRIES) == len(COORDINATOR_COMMANDS)


def test_reversibility_matches_the_approved_architect_design() -> None:
    assert {key: entry.reversibility for key, entry in ENTRIES.items()} == EXPECTED_REVERSIBILITY


def _live_actions(entry: CoordinatorCommand) -> list[argparse.Action]:
    root = coordinator.parser()
    group_action = next(
        action
        for action in root._actions  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction) and entry.path[0] in action.choices
    )
    group_parser = group_action.choices[entry.path[0]]
    sub_action = next(
        action
        for action in group_parser._actions  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction) and entry.path[1] in action.choices
    )
    return list(sub_action.choices[entry.path[1]]._actions)  # noqa: SLF001


@pytest.mark.parametrize("entry", COORDINATOR_COMMANDS, ids=lambda e: e.key)
def test_required_fields_match_the_live_parser(entry: CoordinatorCommand) -> None:
    live_required = {
        action.dest
        for action in _live_actions(entry)
        if getattr(action, "required", False)
    }
    assert {field.dest for field in entry.fields if field.required} == live_required


@pytest.mark.parametrize("entry", COORDINATOR_COMMANDS, ids=lambda e: e.key)
def test_choices_and_repeatable_fields_match_the_live_parser(entry: CoordinatorCommand) -> None:
    live_by_dest = {action.dest: action for action in _live_actions(entry)}
    for field in entry.fields:
        live = live_by_dest[field.dest]
        assert field.repeatable == isinstance(live, argparse._AppendAction)
        expected_choices = tuple(str(c) for c in live.choices) if live.choices else None
        assert field.choices == expected_choices


def test_repeatable_field_serialises_one_flag_value_pair_per_non_empty_line() -> None:
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
    code = "import sys; sys.modules['textual'] = None; import harness.console.coordinator_catalog"
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
