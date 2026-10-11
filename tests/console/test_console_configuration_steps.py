"""Schema-driven steps cover advanced values without hiding allowed choices."""

from __future__ import annotations

from pathlib import Path
import pytest

from harness.console.configuration import ConfigurationDraft, PACKAGE, parse_object
from harness.console.configuration_steps import steps_for


def test_role_effort_choices_and_approval_help_are_available_without_losing_values(
    tmp_path: Path,
) -> None:
    document = parse_object(
        (PACKAGE / "orchestration.example.json").read_bytes(), "example"
    )
    draft = ConfigurationDraft(
        tmp_path, {"orchestration.json": document}, {"orchestration.json": None}
    )
    steps = steps_for(draft)
    effort = next(s for s in steps if s.path[-1] == "effort")
    assert set(effort.choices) == {
        "none",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
        "ultra",
    }
    assert all(len(effort.choice_page(page)) <= 4 for page in range(3))
    approval = next(s for s in steps if s.path == ("approval_policy",))
    assert approval.recommendation(draft)[0] == "manual_all"
    assert "руч" in approval.recommendation(draft)[1].lower()
    assert "auto" in approval.description
    assert draft.documents["orchestration.json"] == document


def test_absent_optional_parent_does_not_require_children(tmp_path: Path) -> None:
    draft = ConfigurationDraft(
        tmp_path, {"project.json": {"language": "ru"}}, {"project.json": None}
    )
    steps = steps_for(draft)
    assert any(s.path == ("memory_policy",) and not s.required for s in steps)
    assert not any(s.path == ("memory_policy", "top_k") for s in steps)
    draft.set_value("project.json", ("memory_policy",), {"source_types": ["adr"]})
    sources = next(
        s for s in steps_for(draft) if s.path == ("memory_policy", "source_types")
    )
    assert len(sources.choices) == 6
    assert len(sources.choice_page(0)) == 4
    sources.validate(["adr", "glossary"])
    with pytest.raises(ValueError):
        sources.validate(["unsupported"])


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity"])
def test_numeric_steps_reject_non_json_numbers(text: str) -> None:
    from harness.console.configuration_steps import ConfigurationStep

    step = ConfigurationStep(
        "orchestration.json", ("threshold",), {"type": "number"}, "Threshold", True
    )
    with pytest.raises(ValueError, match="JSON"):
        step.parse(text)
    with pytest.raises(ValueError, match="JSON"):
        step.validate(float(text))


def test_nested_nonfinite_values_are_rejected_before_draft_rendering() -> None:
    from harness.console.configuration_steps import ConfigurationStep

    step = ConfigurationStep(
        "project.json", ("policy",), {"type": "object"}, "Policy", True
    )
    with pytest.raises(ValueError, match="JSON"):
        step.parse('{"threshold": NaN}')
    with pytest.raises(ValueError, match="JSON"):
        parse_object(b'{"threshold": NaN}', "project.json")
