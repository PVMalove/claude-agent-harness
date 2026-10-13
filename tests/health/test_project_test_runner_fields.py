"""Тесты необязательных полей test_paths и test_runner в .harness/project.json (issue #553)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from harness.health import project_files

_SCHEMA = (
    Path(project_files.__file__).resolve().parents[1]
    / "project"
    / "project.schema.json"
)
_BASE = {
    "language": "ru",
    "base_branch": "master",
    "branch_pattern": "^feature/.+",
    "qa_gate_commands": ["make lint"],
}
_SECRET = "s3cr3t-value"


def _problems(tmp_path: Path, extra: dict[str, object]) -> list[str]:
    path = tmp_path / ".harness" / "project.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**_BASE, **extra}), encoding="utf-8")
    problems: list[str] = []
    project_files.validate_project_json(tmp_path, problems)
    return problems


def test_fields_are_optional(tmp_path: Path) -> None:
    assert _problems(tmp_path, {}) == []


@pytest.mark.parametrize(
    "value",
    [
        ["tests/**"],
        ["tests/**", "**/test_*.py", "src/*_test.go"],
        ["tests\\unit\\**"],
        ["a..b/**", "...hidden/**"],
    ],
)
def test_valid_test_paths(tmp_path: Path, value: list[str]) -> None:
    assert _problems(tmp_path, {"test_paths": value}) == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ([], "test_paths must be a non-empty list"),
        ("tests/**", "test_paths must be a non-empty list"),
        (None, "test_paths must be a non-empty list"),
        (["tests/**", ""], "test_paths[1] must be a relative glob"),
        (["tests/**", 1], "test_paths[1] must be a relative glob"),
        (["/abs/tests/**"], "test_paths[0] must be a relative glob"),
        (["\\abs\\tests"], "test_paths[0] must be a relative glob"),
        (["C:/tests/**"], "test_paths[0] must be a relative glob"),
        ([".."], "test_paths[0] must be a relative glob"),
        (["../tests/**"], "test_paths[0] must be a relative glob"),
        (["tests/../src/**"], "test_paths[0] must be a relative glob"),
        (["tests\\..\\src"], "test_paths[0] must be a relative glob"),
        (["tests/**\nsrc/**"], "test_paths[0] must be a relative glob"),
        (["tests/**", "tests/**"], "test_paths must not contain duplicate globs"),
    ],
)
def test_invalid_test_paths(tmp_path: Path, value: object, expected: str) -> None:
    problems = _problems(tmp_path, {"test_paths": value})
    assert any(expected in item for item in problems), problems


def test_test_paths_problems_never_echo_values() -> None:
    problems = project_files.test_paths_problems([f"/{_SECRET}/**", f"../{_SECRET}"])
    assert len(problems) == 2
    assert not any(_SECRET in item for item in problems)


@pytest.mark.parametrize(
    "command",
    [
        ["python3", "-m", "unittest", "{test_id}"],
        ["pytest", "{test_id}", "-q"],
    ],
)
def test_valid_test_runner(tmp_path: Path, command: list[str]) -> None:
    assert _problems(tmp_path, {"test_runner": {"command": command}}) == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (["pytest", "{test_id}"], "test_runner must be an object"),
        ({}, "test_runner missing required field(s): command"),
        (
            {"command": ["pytest", "{test_id}"], "shell": True},
            "test_runner has unknown field(s): shell",
        ),
        ({"command": "pytest {test_id}"}, "test_runner.command must be a non-empty"),
        ({"command": []}, "test_runner.command must be a non-empty"),
        ({"command": ["pytest", "", "{test_id}"]}, "test_runner.command[1] must be"),
        ({"command": ["pytest", 1, "{test_id}"]}, "test_runner.command[1] must be"),
        ({"command": ["pytest"]}, "exactly one '{test_id}' element"),
        ({"command": ["pytest", "{test_id}", "{test_id}"]}, "exactly one"),
        ({"command": ["{test_id}", "pytest"]}, "test_runner.command[0] must be the"),
        ({"command": ["pytest", "x{test_id}"]}, "exactly one"),
    ],
)
def test_invalid_test_runner(tmp_path: Path, value: object, expected: str) -> None:
    problems = _problems(tmp_path, {"test_runner": value})
    assert any(expected in item for item in problems), problems


def test_test_runner_problems_never_echo_values() -> None:
    problems = project_files.test_runner_problems(
        {"command": [_SECRET, "", f"--token={_SECRET}"]}
    )
    assert problems
    assert not any(_SECRET in item for item in problems)


def test_schema_matches_validator() -> None:
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    paths = schema["properties"]["test_paths"]
    assert paths["type"] == "array"
    assert paths["minItems"] == 1
    assert paths["uniqueItems"] is True
    assert paths["items"]["type"] == "string"
    assert paths["items"]["pattern"] == project_files.TEST_PATH_PATTERN
    runner = schema["properties"]["test_runner"]
    assert runner["additionalProperties"] is False
    assert tuple(runner["required"]) == project_files.TEST_RUNNER_FIELDS
    assert tuple(runner["properties"]) == project_files.TEST_RUNNER_FIELDS
    command = runner["properties"]["command"]
    assert command["contains"] == {"const": project_files.TEST_ID_PLACEHOLDER}
    assert command["items"][0]["not"] == {"const": project_files.TEST_ID_PLACEHOLDER}
    assert {"test_paths", "test_runner"} <= project_files.PROJECT_JSON_ALLOWED_FIELDS


@pytest.mark.parametrize(
    ("glob", "valid"),
    [
        ("tests/**", True),
        ("a..b/**", True),
        ("/tests", False),
        ("C:tests", False),
        ("x/../y", False),
        ("..", False),
    ],
)
def test_schema_pattern_agrees_with_validator(glob: str, valid: bool) -> None:
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    pattern = schema["properties"]["test_paths"]["items"]["pattern"]
    assert (re.search(pattern, glob) is not None) is valid
    assert (project_files.test_paths_problems([glob]) == []) is valid
