"""Schema-driven steps cover advanced values without hiding allowed choices."""

from __future__ import annotations

from pathlib import Path
import pytest

from harness.configuration import parse_object
from harness.configuration_schema import PACKAGE
from harness.console.configuration import ConfigurationDraft
from harness.console.configuration_steps import steps_for


@pytest.mark.parametrize("provider", ["none", "native_access", "runtime:factory"])
def test_extensions_step_accepts_runtime_access(tmp_path: Path, provider: str) -> None:
    document = parse_object(
        (PACKAGE / "orchestration.example.json").read_bytes(), "example"
    )
    document["extensions"]["runtime_access"] = provider
    draft = ConfigurationDraft(
        tmp_path, {"orchestration.json": document}, {"orchestration.json": None}
    )
    step = next(s for s in steps_for(draft) if s.path == ("extensions",))
    step.validate(document["extensions"])
    assert step.recommendation(draft)[0] == document["extensions"]
    with pytest.raises(ValueError, match="unknown_extension"):
        step.validate({"unknown_extension": "none"})


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


def test_recommendations_use_project_facts_and_options_describe_limits(
    tmp_path: Path,
) -> None:
    from harness.console.configuration_steps import step_for_problem
    from harness.console.project_facts import project_facts

    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text("verify:\n\t@true\n", encoding="utf-8")
    draft = ConfigurationDraft(
        tmp_path,
        {"project.json": {"language": "ru", "qa_gate_commands": []}},
        {"project.json": None},
    )
    assert project_facts(tmp_path).qa_commands == ("make verify",)
    steps = steps_for(draft)
    qa = next(s for s in steps if s.path == ("qa_gate_commands",))
    value, why = qa.recommendation(draft)
    assert value == ["make verify"] and "pyproject.toml" in why
    assert qa.recommended(draft) == value
    effect = steps[0].effect("auto")
    assert "Ограничение:" in effect and "Когда выбирать:" in effect
    language = next(s for s in steps if s.path == ("language",))
    assert step_for_problem(
        steps, "project.json.language: значение вне допустимых"
    ) == (steps.index(language))
    assert step_for_problem(steps, "что-то без имени поля") is None
