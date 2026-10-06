"""Тесты необязательного поля ci_required_checks в .harness/project.json (issue #535)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.health import project_files

_SCHEMA = Path(project_files.__file__).resolve().parents[1] / "project" / "project.schema.json"
_BASE = {
    "language": "ru",
    "base_branch": "master",
    "branch_pattern": "^feature/.+",
    "qa_gate_commands": ["make lint"],
}


def _problems(tmp_path: Path, extra: dict[str, object]) -> list[str]:
    path = tmp_path / ".harness" / "project.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**_BASE, **extra}), encoding="utf-8")
    problems: list[str] = []
    project_files.validate_project_json(tmp_path, problems)
    return problems


@pytest.mark.parametrize("value", [[], ["lint"], ["lint", "tests (3.12)"]])
def test_valid_ci_required_checks(tmp_path: Path, value: list[str]) -> None:
    assert _problems(tmp_path, {"ci_required_checks": value}) == []


def test_field_is_optional(tmp_path: Path) -> None:
    assert _problems(tmp_path, {}) == []


@pytest.mark.parametrize(
    "value",
    ["lint", None, {"a": 1}, [""], ["  "], [1], ["lint", "lint"], [["lint"]]],
)
def test_invalid_ci_required_checks(tmp_path: Path, value: object) -> None:
    problems = _problems(tmp_path, {"ci_required_checks": value})
    assert any("ci_required_checks" in item for item in problems)


def test_schema_matches_validator() -> None:
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    field = schema["properties"]["ci_required_checks"]
    assert field["type"] == "array"
    assert field["uniqueItems"] is True
    assert field["items"] == {"type": "string", "minLength": 1}
    assert "ci_required_checks" in project_files.PROJECT_JSON_ALLOWED_FIELDS
