"""The Red-Green-Refactor cycle protocol: its markers stay in the prose and the scenario stays wired."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from scripts.clean_room import isolated_test_runner, test_author_cycle

ROOT = Path(__file__).resolve().parents[2]
DEVELOPER = ROOT / "harness" / "orchestration" / "roles" / "developer.md"
COMMON = ROOT / "harness" / "orchestration" / "roles" / "_common.md"
AGENT = ROOT / "harness" / "project" / "agents" / "test-author.md"
GUIDE = ROOT / "harness" / "project" / "docs-agents" / "test-author.md"
ROOT_GUIDE = ROOT / "docs" / "agents" / "test-author.md"

DEVELOPER_MARKERS = (
    "Red-Green-Refactor",
    "needs_skeleton",
    "contract_gap",
    "never weakened",
    "three consecutive failed author calls",
    "no new dispatch",
    "no ledger state",
    "Not a behavioral Red",
    "immediately",
    "ordinary blocker path",
)
AGENT_MARKERS = ("`status`", "needs_skeleton", "contract_gap", "failed assertion", "already Green")
GUIDE_MARKERS = ("needs_skeleton", "contract_gap", "три", "не ослабляется", "ledger")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("path", "markers"),
    [(DEVELOPER, DEVELOPER_MARKERS), (AGENT, AGENT_MARKERS), (GUIDE, GUIDE_MARKERS)],
    ids=lambda value: value.name if isinstance(value, Path) else "",
)
def test_protocol_markers(path: Path, markers: tuple[str, ...]) -> None:
    text = " ".join(_text(path).split())
    missing = [marker for marker in markers if marker not in text]
    assert not missing, f"{path.name} lost cycle markers: {missing}"


def test_common_contract_points_to_the_cycle() -> None:
    assert "Red-Green-Refactor cycle in `developer.md`" in " ".join(_text(COMMON).split())


def test_guide_copies_are_identical() -> None:
    assert _text(GUIDE) == _text(ROOT_GUIDE)


def test_cycle_scenario_runs_from_the_runner_scenario() -> None:
    source = inspect.getsource(isolated_test_runner.run)
    assert "verify(project, _runner_execute(project))" in source
    assert "verify(project, _direct_execute(project))" in source


def test_cycle_scenario_limit_matches_the_protocol() -> None:
    assert test_author_cycle.MAX_FAILED_CALLS == 3
