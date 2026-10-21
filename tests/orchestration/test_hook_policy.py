"""Тесты resolver политики hooks (issue #794)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from harness.errors import PolicyError
from harness.orchestration import hook_policy
from harness.orchestration.core.utils import CoordinatorError
from harness.orchestration.workflow import recovery

_BASE = {
    "language": "ru",
    "base_branch": "master",
    "branch_pattern": "^feature/.+",
    "qa_gate_commands": [],
}


def _repo(tmp_path: Path, **extra: object) -> Path:
    path = tmp_path / ".harness" / "project.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**_BASE, **extra}), encoding="utf-8")
    return tmp_path


def test_missing_file_and_missing_key_resolve_to_standard(tmp_path: Path) -> None:
    for repo in (tmp_path, _repo(tmp_path)):
        policy = hook_policy.load_policy(repo)
        assert policy == {"profile": "standard", "escalations": []}
        assert hook_policy.resolve_profile(policy, role="developer") == "standard"


def test_base_profile_is_read_from_project_json(tmp_path: Path) -> None:
    repo = _repo(tmp_path, hook_policy={"profile": "minimal"})
    policy = hook_policy.load_policy(repo)
    assert hook_policy.resolve_profile(policy, role="qa", paths=["a.py"]) == "minimal"


_ESCALATIONS = {
    "profile": "minimal",
    "escalations": [
        {"paths": ["harness/**"], "profile": "standard"},
        {"roles": ["developer"], "paths": ["harness/health/*.py"], "profile": "strict"},
        {"roles": ["qa"], "profile": "standard"},
    ],
}


@pytest.mark.parametrize(
    ("role", "paths", "expected"),
    [
        ("review", [], "minimal"),
        ("review", ["docs/a.md"], "minimal"),
        ("review", ["harness/x/y.py"], "standard"),
        ("developer", ["harness/health/a.py"], "strict"),
        ("developer", ["harness/health/sub/a.py"], "standard"),
        ("developer", ["docs/a.md", "harness/health/a.py"], "strict"),
        ("coordinator", ["harness/health/a.py"], "standard"),
        ("qa", [], "standard"),
        ("qa", ["./harness/health/a.py"], "standard"),
        (None, [], "minimal"),
    ],
)
def test_strictest_matching_escalation_wins(
    role: str | None, paths: list[str], expected: str
) -> None:
    policy = hook_policy.normalize_policy(_ESCALATIONS)
    assert hook_policy.resolve_profile(policy, role=role, paths=paths) == expected


def test_escalation_never_weakens_the_base_profile() -> None:
    policy = {
        "profile": "strict",
        "escalations": [{"paths": [], "roles": [], "profile": "minimal"}],
    }
    assert hook_policy.resolve_profile(policy, role="qa") == "strict"


def test_globs_match_by_segment() -> None:
    policy = hook_policy.normalize_policy(
        {
            "profile": "minimal",
            "escalations": [{"paths": ["**/secrets.*", "a/?/c"], "profile": "strict"}],
        }
    )

    def profile(path: str) -> str:
        return hook_policy.resolve_profile(policy, paths=[path])

    assert profile("secrets.env") == "strict"
    assert profile("x/y/secrets.env") == "strict"
    assert profile("a/b/c") == "strict"
    assert profile("a/bb/c") == "minimal"
    assert profile("a/b/c/d") == "minimal"


def test_environment_and_unknown_input_are_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, hook_policy={"profile": "strict"})
    before = hook_policy.load_policy(repo)
    for name in ("HOOK_PROFILE", "HARNESS_HOOK_PROFILE", "CLAUDE_HOOK_PROFILE"):
        monkeypatch.setenv(name, "minimal")
    after = hook_policy.load_policy(repo)
    assert after == before
    assert hook_policy.resolve_profile(after, role="qa") == "strict"
    assert hook_policy.project_hook_policy_sha256(
        repo
    ) == hook_policy.hook_policy_sha256(before)


def test_hash_is_stable_and_order_independent() -> None:
    first = hook_policy.normalize_policy(_ESCALATIONS)
    reordered = hook_policy.normalize_policy(
        {
            "profile": "minimal",
            "escalations": list(reversed(_ESCALATIONS["escalations"])),
        }
    )
    assert hook_policy.hook_policy_sha256(first) == hook_policy.hook_policy_sha256(
        reordered
    )
    digest = hook_policy.hook_policy_sha256(first)
    assert len(digest) == 64 and int(digest, 16) >= 0
    assert digest == hook_policy.hook_policy_sha256(first)


def test_hash_changes_with_the_policy() -> None:
    digests = {
        hook_policy.hook_policy_sha256(hook_policy.normalize_policy(value))
        for value in (
            None,
            {"profile": "standard"},
            {"profile": "minimal"},
            {"profile": "strict"},
            {
                "profile": "standard",
                "escalations": [{"roles": ["qa"], "profile": "strict"}],
            },
        )
    }
    # None and an explicit standard profile are the same policy.
    assert len(digests) == 4


@pytest.mark.parametrize(
    "value",
    [
        "strict",
        {"profile": "lax"},
        {"profile": "strict", "escalations": [{"profile": "minimal"}]},
    ],
)
def test_invalid_policy_is_an_error(tmp_path: Path, value: object) -> None:
    with pytest.raises(PolicyError):
        hook_policy.load_policy(_repo(tmp_path, hook_policy=value))


@pytest.mark.parametrize("content", ["{not json", "[]", '"x"'])
def test_unreadable_project_json_is_an_error(tmp_path: Path, content: str) -> None:
    path = tmp_path / ".harness" / "project.json"
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    with pytest.raises(PolicyError):
        hook_policy.load_policy(tmp_path)


def _restart(original: dict[str, str], renewed: dict[str, str]) -> None:
    batch = {"environmental_restart": {"dispatch_id": "dispatch-1"}}
    with mock.patch.object(recovery, "_load_dispatch", return_value=original):
        recovery.require_restart_contract(Path("."), batch, renewed)


def test_an_environment_restart_keeps_the_pinned_hook_policy() -> None:
    pinned = {"hook_policy_sha256": "a" * 64}

    _restart(pinned, dict(pinned))
    with pytest.raises(CoordinatorError, match="hook_policy_sha256"):
        _restart(pinned, {"hook_policy_sha256": "b" * 64})
    with pytest.raises(CoordinatorError, match="hook_policy_sha256"):
        _restart(pinned, {})


def test_an_environment_restart_of_a_brief_without_a_pin_may_gain_one() -> None:
    _restart({}, {"hook_policy_sha256": "a" * 64})
