"""Memory configuration is optional and rejects unsafe opt-in values."""

import json
from pathlib import Path

import pytest

from harness.health.project_files import validate_project_json

BASE = {
    "language": "ru",
    "base_branch": "main",
    "branch_pattern": "^feature/.+",
    "qa_gate_commands": [],
}
POLICY = {
    "source_types": ["adr", "glossary"],
    "allow_paths": ["docs/adr/*.md", "CONTEXT.md"],
    "redact_rules": ["secret=[^ ]+"],
    "min_similarity": 0.5,
    "top_k": 5,
    "max_tokens": 1000,
}


def problems(repo: Path, extra: dict[str, object]) -> list[str]:
    """Exercise the same contract consumed by harness health."""
    config = repo / ".harness/project.json"
    config.parent.mkdir(exist_ok=True)
    config.write_text(json.dumps({**BASE, **extra}), encoding="utf-8")
    result: list[str] = []
    validate_project_json(repo, result)
    return result


def test_legacy_and_explicit_memory_config_are_valid(tmp_path: Path) -> None:
    """Existing projects and explicit valid policy remain healthy."""
    assert problems(tmp_path, {}) == []
    assert (
        problems(tmp_path, {"memory": {"enabled": True}, "memory_policy": POLICY}) == []
    )
    assert problems(tmp_path, {"memory": {"enabled": False}}) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_types", ["ticket"]),
        ("source_types", "adr"),
        ("allow_paths", ["../secret.md"]),
        ("allow_paths", ["/etc/passwd"]),
        ("allow_paths", ["C:/docs/x.md"]),
        ("allow_paths", [r"\\server\x"]),
        ("allow_paths", ["docs//x.md"]),
        ("allow_paths", ["./CONTEXT.md"]),
        ("allow_paths", ["docs/"]),
        ("allow_paths", [""]),
        ("redact_rules", ["["]),
        ("redact_rules", ["x" * 513]),
        ("redact_rules", [""]),
        ("redact_rules", [True]),
        ("min_similarity", float("nan")),
        ("min_similarity", float("inf")),
        ("min_similarity", True),
        ("min_similarity", -0.1),
        ("min_similarity", 1.1),
        ("min_similarity", 10**999),
        ("top_k", True),
        ("top_k", 0),
        ("top_k", 1.5),
        ("max_tokens", 0),
        ("max_tokens", True),
    ],
)
def test_memory_policy_rejects_unsafe_values(
    tmp_path: Path, field: str, value: object
) -> None:
    """Health reports the same unsafe configuration runtime would refuse."""
    result = problems(tmp_path, {"memory_policy": {**POLICY, field: value}})
    assert len(result) == 1
    assert f"memory_policy.{field}" in result[0]


@pytest.mark.parametrize(
    "extra",
    [
        {"memory": True},
        {"memory": {"enabled": 1}},
        {"memory": {"enabled": True, "extra": 0}},
        {"memory": {}},
        {"memory_policy": {}},
        {"memory_policy": {**POLICY, "extra": 0}},
    ],
)
def test_memory_sections_have_strict_shape(
    tmp_path: Path, extra: dict[str, object]
) -> None:
    """Unknown fields and partial policies are not silently interpreted."""
    assert problems(tmp_path, extra)


def test_empty_allowlists_and_disabled_policy_deny_ingest() -> None:
    """Opt-in alone cannot grant source access."""
    from harness.memory.policy import parse_policy

    assert not parse_policy({}).active
    assert not parse_policy({"memory": {"enabled": True}}).active
    for field in ("source_types", "allow_paths"):
        assert not parse_policy(
            {"memory": {"enabled": True}, "memory_policy": {**POLICY, field: []}}
        ).active
    assert not parse_policy({"memory_policy": POLICY}).active
