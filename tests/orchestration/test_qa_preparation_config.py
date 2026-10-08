"""The optional ``qa_preparation`` key: schema, health validation and the config accessor."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.orchestration import contract
from harness.orchestration.core.utils import CoordinatorError
from harness.orchestration.core.config import _qa_preparation_commands

ROOT = Path(__file__).resolve().parents[2]


def _problems(tmp_path: Path, extra: dict[str, object]) -> list[str]:
    path = tmp_path / "orchestration.json"
    path.write_text(
        json.dumps({"concurrency_budget": 1, "verification_commands": ["x"], **extra}),
        encoding="utf-8",
    )
    return contract.health_problems(path, ROOT / "harness/orchestration/roles")


def _mentions_preparation(problems: list[str]) -> list[str]:
    return [item for item in problems if "qa_preparation" in item]


def test_the_schema_declares_qa_preparation_as_a_list_of_commands() -> None:
    schema = json.loads(
        (ROOT / "harness/orchestration/orchestration.schema.json").read_text(
            encoding="utf-8"
        )
    )
    declared = schema["properties"]["qa_preparation"]
    assert declared["type"] == "array"
    assert declared["items"] == {"type": "string", "minLength": 1}
    assert "qa_preparation" not in schema.get("required", [])


def test_a_config_without_qa_preparation_stays_valid(tmp_path: Path) -> None:
    assert _mentions_preparation(_problems(tmp_path, {})) == []


def test_a_list_of_commands_is_valid(tmp_path: Path) -> None:
    assert (
        _mentions_preparation(_problems(tmp_path, {"qa_preparation": ["uv sync"]}))
        == []
    )


@pytest.mark.parametrize("value", ["uv sync", [""], [1], {"cmd": "uv sync"}])
def test_a_malformed_value_is_a_health_problem(tmp_path: Path, value: object) -> None:
    problems = _mentions_preparation(_problems(tmp_path, {"qa_preparation": value}))
    assert problems == [
        "orchestration qa_preparation must be a list of strings when provided"
    ]


def test_the_accessor_returns_the_declared_commands_or_none() -> None:
    assert _qa_preparation_commands({}) == []
    assert _qa_preparation_commands({"qa_preparation": ["a", "b"]}) == ["a", "b"]
    assert _qa_preparation_commands({"qa_preparation": []}) == []


def test_the_accessor_refuses_a_malformed_value_with_a_remedy() -> None:
    with pytest.raises(CoordinatorError) as raised:
        _qa_preparation_commands({"qa_preparation": "uv sync"})
    assert "qa_preparation" in raised.value.message
    assert raised.value.remedy


@pytest.mark.parametrize("key", ["qa_environment_probes", "qa_project_file_checks"])
def test_the_independent_fact_keys_are_declared_validated_and_read(
    tmp_path: Path, key: str
) -> None:
    schema = json.loads(
        (ROOT / "harness/orchestration/orchestration.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert schema["properties"][key]["items"] == {"type": "string", "minLength": 1}
    assert key not in schema.get("required", [])
    assert [p for p in _problems(tmp_path, {key: ["check"]}) if key in p] == []
    assert [p for p in _problems(tmp_path, {key: "check"}) if key in p] == [
        f"orchestration {key} must be a list of strings when provided"
    ]
    assert _qa_preparation_commands({}, key) == []
    assert _qa_preparation_commands({key: ["a"]}, key) == ["a"]
    with pytest.raises(CoordinatorError):
        _qa_preparation_commands({key: "a"}, key)
