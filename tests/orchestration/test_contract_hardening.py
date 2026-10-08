"""Hardening tests for the portable orchestration contract (`contract.py`)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.orchestration import contract
from harness.orchestration.contract import ContractError, load_role_manifest

_MANIFEST = (
    "---\n"
    "name: {name}\n"
    "mode: read-only\n"
    "required_capabilities:\n"
    "  - review\n"
    "risk_triggers:\n"
    "  - {trigger}\n"
    "---\n"
)


def test_a_non_utf8_role_manifest_is_a_contract_error(tmp_path: Path) -> None:
    manifest = tmp_path / "reviewer.md"
    manifest.write_bytes(
        _MANIFEST.format(name="reviewer", trigger="проверка").encode("cp1251")
    )

    with pytest.raises(ContractError) as caught:
        load_role_manifest(manifest)

    assert caught.value.message == "role manifest 'reviewer.md' is not UTF-8 text"
    assert caught.value.remedy == f"save {manifest} as UTF-8"


def test_health_reports_a_non_utf8_role_manifest_instead_of_crashing(
    tmp_path: Path,
) -> None:
    roles = tmp_path / "roles"
    roles.mkdir()
    (roles / "reviewer.md").write_bytes(
        _MANIFEST.format(name="reviewer", trigger="проверка").encode("cp1251")
    )
    config = tmp_path / "orchestration.json"
    config.write_text(json.dumps({"access_policy": {}}), encoding="utf-8")

    problems = contract.health_problems(config, roles)

    assert "orchestration role manifest 'reviewer.md' is not UTF-8 text" in problems


@pytest.mark.parametrize("requested", [None, " claude "])
def test_an_unusable_runtime_entry_names_the_resolved_runtime(
    requested: object,
) -> None:
    config = {
        "provider_profiles": {"p": {"capabilities": ["review"]}},
        "assignment_plans": {"reviewer": {"runtimes": {"claude": {"profiles": []}}}},
    }
    role = {
        "name": "reviewer",
        "mode": "read-only",
        "required_capabilities": ["review"],
    }

    with pytest.raises(ContractError) as caught:
        contract.resolve_assignment(config, role, "reviewer", requested)

    assert caught.value.message == "role 'reviewer' has no provider profile"
    assert caught.value.remedy == (
        "add a non-empty profiles list to "
        "assignment_plans['reviewer'].runtimes['claude']"
    )
