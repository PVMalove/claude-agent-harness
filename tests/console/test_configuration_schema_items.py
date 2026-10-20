"""Draft-07 ``items`` forms in the console schema checker (issue #767)."""

from __future__ import annotations

import json

import pytest

from harness.configuration_schema import PACKAGE, value_problems

TUPLE = {
    "type": "array",
    "items": [{"type": "string"}],
    "additionalItems": {"type": "integer"},
}


@pytest.mark.parametrize(
    "value,expected",
    [
        ([], []),
        (["a", 1, 2], []),
        ([1], ["значение[0]: неверный тип поля"]),
        (["a", "b"], ["значение[1]: неверный тип поля"]),
    ],
)
def test_tuple_items_check_each_position_and_the_rest_by_additional_items(
    value: list[object], expected: list[str]
) -> None:
    problems = value_problems(value, TUPLE, TUPLE)

    assert problems == expected


def test_tuple_items_allow_any_extra_element_without_additional_items() -> None:
    node = {"items": [{"type": "string"}]}

    problems = value_problems(["a", {"x": 1}, None], node, node)

    assert problems == []


@pytest.mark.parametrize(
    "value,expected",
    [
        (["a"], []),
        (["a", "b"], ["значение[1]: поле запрещено схемой"]),
    ],
)
def test_tuple_items_with_false_additional_items_reject_an_extra_element(
    value: list[object], expected: list[str]
) -> None:
    node = {"items": [{"type": "string"}], "additionalItems": False}

    problems = value_problems(value, node, node)

    assert problems == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        (["a", "b"], []),
        (["a", 1], ["значение[1]: неверный тип поля"]),
    ],
)
def test_object_items_check_every_element_and_ignore_additional_items(
    value: list[object], expected: list[str]
) -> None:
    node = {"items": {"type": "string"}, "additionalItems": False}

    problems = value_problems(value, node, node)

    assert problems == expected


@pytest.mark.parametrize(
    "command,expected",
    [
        (["python3", "-m", "pytest", "{test_id}"], []),
        (["{test_id}", "pytest"], ["значение.command[0]: сочетание полей запрещено"]),
    ],
)
def test_project_test_runner_command_rejects_test_id_as_the_first_argument(
    command: list[str], expected: list[str]
) -> None:
    schema = json.loads((PACKAGE / "project" / "project.schema.json").read_text())
    node = schema["properties"]["test_runner"]

    problems = value_problems({"command": command}, node, schema)

    assert problems == expected
