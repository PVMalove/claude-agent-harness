"""Тесты необязательного поля hook_policy в .harness/project.json (issue #794)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.configuration_schema import value_problems
from harness.health import project_files

_PROJECT = Path(project_files.__file__).resolve().parents[1] / "project"
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


def test_field_is_optional(tmp_path: Path) -> None:
    assert _problems(tmp_path, {}) == []


@pytest.mark.parametrize(
    "value",
    [
        {"profile": "minimal"},
        {"profile": "standard", "escalations": []},
        {
            "profile": "standard",
            "escalations": [
                {"paths": ["harness/**"], "roles": ["developer"], "profile": "strict"},
                {"roles": ["qa", "review"], "profile": "standard"},
                {"paths": ["docs/**"], "profile": "strict"},
            ],
        },
    ],
)
def test_valid_hook_policy(tmp_path: Path, value: object) -> None:
    assert _problems(tmp_path, {"hook_policy": value}) == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("standard", "hook_policy must be an object"),
        ({}, "hook_policy.profile must be one of"),
        ({"profile": "lax"}, "hook_policy.profile must be one of"),
        (
            {"profile": "standard", "mode": "x"},
            "hook_policy has unknown field(s): mode",
        ),
        ({"profile": "standard", "escalations": {}}, "escalations must be a list"),
        (
            {"profile": "standard", "escalations": ["strict"]},
            "escalations[0] must be an object",
        ),
        (
            {"profile": "standard", "escalations": [{"profile": "minimal"}]},
            "escalations[0].profile must not be lower than the base profile",
        ),
        (
            {"profile": "strict", "escalations": [{"profile": "standard"}]},
            "must not be lower than the base profile",
        ),
        (
            {"profile": "standard", "escalations": [{"profile": "loud"}]},
            "escalations[0].profile must be one of",
        ),
        (
            {
                "profile": "standard",
                "escalations": [{"profile": "strict", "paths": []}],
            },
            "escalations[0].paths must be a non-empty list",
        ),
        (
            {
                "profile": "standard",
                "escalations": [{"profile": "strict", "paths": ["../x"]}],
            },
            "escalations[0].paths[0] must be a relative glob",
        ),
        (
            {
                "profile": "standard",
                "escalations": [{"profile": "strict", "roles": ["admin"]}],
            },
            "escalations[0].roles[0] must be one of",
        ),
        (
            {
                "profile": "standard",
                "escalations": [{"profile": "strict", "roles": []}],
            },
            "escalations[0].roles must be a non-empty list",
        ),
        (
            {"profile": "standard", "escalations": [{"profile": "strict", "x": 1}]},
            "escalations[0] has unknown field(s): x",
        ),
    ],
)
def test_invalid_hook_policy(tmp_path: Path, value: object, expected: str) -> None:
    problems = _problems(tmp_path, {"hook_policy": value})
    assert any(expected in item for item in problems), problems


def test_problems_never_echo_values() -> None:
    problems = project_files.hook_policy_problems(
        {
            "profile": _SECRET,
            "escalations": [
                {"profile": _SECRET, "paths": [f"../{_SECRET}"], "roles": [_SECRET]}
            ],
        }
    )
    assert len(problems) == 4
    assert not any(_SECRET in item for item in problems)


@pytest.mark.parametrize(
    "value",
    [
        {"profile": "standard"},
        {"profile": "lax"},
        {
            "profile": "standard",
            "escalations": [{"profile": "strict", "paths": ["../x"]}],
        },
        {
            "profile": "standard",
            "escalations": [{"profile": "strict", "roles": ["admin"]}],
        },
        {
            "profile": "standard",
            "escalations": [{"profile": "strict", "roles": ["qa"]}],
        },
        {"profile": "standard", "extra": 1},
        {"escalations": []},
    ],
)
def test_schema_and_health_agree(value: object) -> None:
    schema = json.loads((_PROJECT / "project.schema.json").read_text(encoding="utf-8"))
    node = schema["properties"]["hook_policy"]
    assert (value_problems(value, node, schema) == []) is (
        project_files.hook_policy_problems(value) == []
    )


def test_schema_lists_match_validator() -> None:
    schema = json.loads((_PROJECT / "project.schema.json").read_text(encoding="utf-8"))
    node = schema["properties"]["hook_policy"]["properties"]
    escalation = node["escalations"]["items"]["properties"]
    assert tuple(node["profile"]["enum"]) == project_files.HOOK_PROFILES
    assert tuple(escalation["profile"]["enum"]) == project_files.HOOK_PROFILES
    assert (
        tuple(escalation["roles"]["items"]["enum"]) == project_files.HOOK_POLICY_ROLES
    )


def test_template_sets_standard_profile_explicitly() -> None:
    rendered = (_PROJECT / "project.json.tmpl").read_text(encoding="utf-8")
    for key, value in {
        "LANGUAGE": "ru",
        "PR_BASE_BRANCH": "main",
        "BRANCH_PATTERN": "^feature/.+",
        "QA_GATE_COMMANDS": "[]",
        "TRACKER_FIELD": "",
        "TEST_FIELDS": "",
    }.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    assert json.loads(rendered)["hook_policy"] == {"profile": "standard"}
