"""The Red-Green-Refactor cycle protocol: its markers stay in the prose and the scenario stays wired."""

from __future__ import annotations

from pathlib import Path

import pytest

import importlib

isolated_test_runner = importlib.import_module(
    "scripts.clean_room.isolated_test_runner"
)
test_author_cycle = importlib.import_module("scripts.clean_room.test_author_cycle")

ROOT = Path(__file__).resolve().parents[2]
DEVELOPER = ROOT / "harness" / "orchestration" / "roles" / "developer.md"
COMMON = ROOT / "harness" / "orchestration" / "roles" / "_common.md"
AGENT = ROOT / "harness" / "project" / "agents" / "test-author.md"
IMPLEMENT = ROOT / "skills" / "first-party" / "engineering" / "implement" / "SKILL.md"
IMPLEMENT_DESCRIPTION = ROOT / "docs" / "skills" / "implement.md"
CHANGELOG = ROOT / "CHANGELOG.md"
FAST_IMPLEMENT = (
    ROOT / "skills" / "first-party" / "engineering" / "fast-implement" / "SKILL.md"
)
TDD = ROOT / "skills" / "first-party" / "engineering" / "tdd" / "SKILL.md"
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
FULL_ROUTE_MARKERS = (
    '"$CLAUDE_PROJECT_DIR/.claude/hooks/test-author-preflight.sh" --worktree',
    "checkout_mismatch",
    "transport_unsupported",
    "ordinary TDD route",
    "never a blocker",
)
FULL_ROUTE_GUIDE_MARKERS = (
    "Полный маршрут `/implement`",
    'test-author-preflight.sh" --worktree',
    "`checkout_mismatch`",
    "`transport_unsupported`",
    "обычный TDD",
    "не blocker",
)
FULL_ROUTE_DESCRIPTION_MARKERS = (
    "--worktree",
    "`checkout_mismatch`",
    "`transport_unsupported`",
    "обычный TDD",
)
FAST_MARKERS = (
    "Agree the seams before the first test",
    "test-author-preflight.sh",
    '{"status":"ok"}',
    "Neither route adds a coordinator, architect or QA step",
    "algorithm or implementation snippet",
    "needs_skeleton",
    "contract_gap",
    "never weakened",
    "Not a behavioral Red",
    "Green immediately",
    "three consecutive failed author calls",
    "no dispatch and no ledger state",
)
SHORT_ROUTE_MARKERS = ("/fast-implement", "short route")
AGENT_MARKERS = (
    "`status`",
    "needs_skeleton",
    "contract_gap",
    "failed assertion",
    "already Green",
)
GUIDE_MARKERS = (
    "needs_skeleton",
    "contract_gap",
    "три",
    "не ослабляется",
    "ledger",
    "Короткий маршрут `/fast-implement`",
    "Включение (opt-in)",
    "Защищённое окружение",
    "bubblewrap 0.9.0+",
    "Откат на обычный маршрут",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("path", "markers"),
    [
        (DEVELOPER, DEVELOPER_MARKERS),
        (DEVELOPER, SHORT_ROUTE_MARKERS),
        (DEVELOPER, FULL_ROUTE_MARKERS),
        (IMPLEMENT, FULL_ROUTE_MARKERS),
        (GUIDE, FULL_ROUTE_GUIDE_MARKERS),
        (IMPLEMENT_DESCRIPTION, FULL_ROUTE_DESCRIPTION_MARKERS),
        (TDD, SHORT_ROUTE_MARKERS),
        (FAST_IMPLEMENT, FAST_MARKERS),
        (AGENT, AGENT_MARKERS),
        (GUIDE, GUIDE_MARKERS),
    ],
    ids=lambda value: value.parent.name if isinstance(value, Path) else "",
)
def test_protocol_markers(path: Path, markers: tuple[str, ...]) -> None:
    text = " ".join(_text(path).split())
    missing = [marker for marker in markers if marker not in text]
    assert not missing, f"{path.name} lost cycle markers: {missing}"


def test_changelog_records_the_full_route_worktree_fix() -> None:
    unreleased = _text(CHANGELOG).split("\n## [", 2)[1]
    assert "#768" in unreleased
    assert "checkout_mismatch" in unreleased


def test_common_contract_points_to_the_cycle() -> None:
    assert "Red-Green-Refactor cycle in `developer.md`" in " ".join(
        _text(COMMON).split()
    )


def test_guide_copies_are_identical() -> None:
    assert _text(GUIDE) == _text(ROOT_GUIDE)


def test_target_guide_does_not_point_at_repository_root_docs() -> None:
    assert not [
        ref
        for ref in ("docs/hooks/", "docs/adr/", "docs/skills/")
        if ref in _text(GUIDE)
    ]


def test_cycle_scenario_passes_on_a_minimal_project(tmp_path: Path) -> None:
    for package in ("src", "tests"):
        (tmp_path / package).mkdir()
        (tmp_path / package / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness" / "project.json").write_text("{}", encoding="utf-8")
    test_author_cycle.verify_cycle(
        tmp_path, isolated_test_runner._direct_execute(tmp_path)
    )
    assert not list((tmp_path / "src").glob("cyc_*.py"))
    assert not list((tmp_path / "tests").glob("cyc_*.py"))


def test_cycle_scenario_limit_matches_the_protocol() -> None:
    assert test_author_cycle.MAX_FAILED_CALLS == 3
