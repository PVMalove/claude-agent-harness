"""The qa-gate skill must work in a linked worktree, as its mark hook already does."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _text(path: Path) -> str:
    """The file's text with line wrapping collapsed, so a phrase may span lines."""
    return " ".join(path.read_text(encoding="utf-8").split())


SKILL = ROOT / "skills" / "first-party" / "pvmalove" / "qa-gate" / "SKILL.md"
DESCRIPTION = ROOT / "docs" / "skills" / "qa-gate.md"
HOOK = ROOT / "harness" / "project" / "hooks" / "qa-gate-state.py"


def test_the_skill_falls_back_to_the_main_worktree_for_the_gitignored_harness() -> None:
    text = _text(SKILL)
    assert "git worktree list --porcelain" in text
    assert "linked worktree" in text
    # Only the config and the wrapper come from the main worktree; commands run in this one.
    assert "working directory" in text
    assert "`.harness/project.json`" in text
    assert "test_summary.py" in text


def test_the_russian_description_states_the_same_rule() -> None:
    text = _text(DESCRIPTION)
    assert "git worktree list --porcelain" in text
    assert "linked worktree" in text


def test_the_mark_hook_uses_the_same_main_worktree_source() -> None:
    assert "worktrees(project)[0][0]" in HOOK.read_text(encoding="utf-8")
