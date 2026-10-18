"""Configuration drafts are read-only until explicit application."""

from __future__ import annotations

import json
from pathlib import Path

from harness.console.configuration import ConfigurationDraft


def test_editing_and_validating_a_draft_preserves_the_active_file(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".harness"
    root.mkdir()
    original = {
        "language": "ru",
        "base_branch": "main",
        "branch_pattern": "^feature/issue-[0-9]+-.+",
        "qa_gate_commands": ["pytest"],
        "tracker": {"type": "local"},
    }
    path = root / "project.json"
    path.write_text(json.dumps(original), encoding="utf-8")
    before = path.read_bytes()
    draft = ConfigurationDraft.load(tmp_path, ("project.json",))
    draft.set_value("project.json", ("language",), "en")
    assert draft.problems() == []
    assert draft.documents["project.json"]["tracker"] == {"type": "local"}
    assert '"en"' in draft.diff()
    assert path.read_bytes() == before
