"""The clean-room QA stage stays one full run (issue scope: the scenarios are never split).

``scripts/verify.py`` starts ``scripts/test_clean_room.py`` as one command without arguments, and
that script runs the whole ``SCENARIOS`` tuple in order, whatever its argv: no scenario subset can
be selected, whether the stage runs on the host or in a QA container.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import verify

ROOT = Path(__file__).resolve().parents[2]
# Imported by name, so the type checker does not follow the untyped clean-room scenarios.
test_clean_room = importlib.import_module("scripts.test_clean_room")


def test_the_clean_room_stage_is_one_command_without_a_scenario_selection() -> None:
    assert verify.clean_room_command() == [
        sys.executable,
        str(ROOT / "scripts" / "test_clean_room.py"),
    ]


def test_one_run_executes_the_whole_scenarios_tuple_in_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ran: list[object] = []
    for scenario in test_clean_room.SCENARIOS:

        def record(context: SimpleNamespace, scenario: object = scenario) -> None:
            ran.append(scenario)

        monkeypatch.setattr(scenario, "run", record)
    # A scenario name on the command line selects nothing.
    monkeypatch.setattr(sys, "argv", ["test_clean_room.py", "install_and_update"])
    monkeypatch.setenv("HARNESS_TEST_RUN_ROOT", str(tmp_path))

    test_clean_room.main()

    assert ran == list(test_clean_room.SCENARIOS)
    assert len(set(map(id, ran))) == len(ran)
