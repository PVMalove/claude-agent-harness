"""Plan selection and pre-action verification of coordinator-executed operations."""

import subprocess
from pathlib import Path

import pytest

from harness.orchestration import operation_access, runtime_access
from harness.orchestration.core.utils import JsonObject


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def _brief(repo: Path, config: JsonObject) -> JsonObject:
    plan = runtime_access.resolve_plan(
        repo, repo, config, "qa", "read-only", operation="qa"
    )
    return {
        "role": "qa",
        "access": "read-only",
        "worktree": str(repo),
        "runtime_access": plan,
        "transition": {"runtime_access_sha256": plan["plan_digest"]},
    }


def test_a_pinned_brief_plan_is_used_even_after_the_live_config_changes(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    pinned = {"access_policy": {"defaults": {"mode": "inherit"}}}
    brief = _brief(repo, pinned)
    live = {"access_policy": {"defaults": {"mode": "unsandboxed"}}}
    plan = operation_access.select_plan(repo, live, "qa", brief=brief)
    assert plan == brief["runtime_access"]
    assert plan["mode"] == "inherit"


def test_a_pinned_plan_that_is_not_bound_to_its_approval_is_refused(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, {"access_policy": {"defaults": {"mode": "inherit"}}})
    brief["transition"] = {"runtime_access_sha256": "0" * 64}
    with pytest.raises(runtime_access.AccessError, match="not bound"):
        operation_access.select_plan(repo, {}, "qa", brief=brief)


def test_a_historical_brief_without_a_plan_selects_legacy_inherit(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    live = {"access_policy": {"defaults": {"mode": "sandbox"}}}
    plan = operation_access.select_plan(
        repo, live, "publish", brief={"role": "developer", "transition": {}}
    )
    assert plan["mode"] == "inherit"
    assert set(plan["sources"].values()) == {"legacy"}
    assert plan["requirements"] == []


def test_the_live_config_selects_the_operation_override_without_a_brief(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    config = {
        "access_policy": {
            "defaults": {"mode": "inherit"},
            "roles": {"developer": {"mode": "sandbox"}},
            "operations": {"git": {"network": {"hosts": ["github.com"]}}},
        }
    }
    plan = operation_access.select_plan(repo, config, "git", worktree=repo)
    assert plan["mode"] == "inherit"
    assert plan["network"] == {"hosts": ["github.com"]}
    assert plan["sources"]["network"] == "operations.git"
    checkout = next(i for i in plan["requirements"] if i["resource"] == "checkout")
    assert checkout["access"] == "write"


def test_a_project_without_authored_access_keeps_legacy_inherit(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    plan = operation_access.select_plan(repo, {}, "qa")
    assert plan["mode"] == "inherit"
    assert set(plan["sources"].values()) == {"legacy"}


def test_an_unknown_operation_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(
        runtime_access.AccessError, match="unknown coordinator operation"
    ):
        operation_access.select_plan(repo, {}, "deploy")
