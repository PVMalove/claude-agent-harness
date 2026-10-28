"""Container QA is pinned at propose (ADR 0022, section 3; ADR 0025): a QA dispatch whose
``access_policy.operations.qa`` authors a container pins its image digests and ``qa_execution``,
binding validation refuses a container QA brief without them, and every other QA brief keeps the
historical host path."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from harness.orchestration import container_executor, runtime_access
from harness.orchestration.core.config import _execution_policy
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.workflow import dispatch as dispatch_workflow

WORKER_ID = "sha256:" + "a" * 64


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def _config(*, operations: bool) -> JsonObject:
    container = {"engine": "podman", "worker_image": "harness-worker:local"}
    policy: JsonObject = {"defaults": {"mode": "sandbox"}}
    if operations:
        policy["operations"] = {"qa": {"container": container}}
    else:
        policy["defaults"] = {"mode": "sandbox", "container": container}
    return {"access_policy": policy, "extensions": {"runtime_access": "docker"}}


def _plan(repo: Path, *, operations: bool = True) -> JsonObject:
    return runtime_access.resolve_plan(
        repo, repo, _config(operations=operations), "qa", "read-only", operation="qa"
    )


def _observation(plan: JsonObject, transport: str) -> container_executor.ContainerObservation:
    return container_executor.ContainerObservation(
        plan_digest=str(plan["plan_digest"]),
        transport=transport,
        supported_modes=container_executor.SUPPORTED_MODES,
        effective_mode="sandbox",
        mechanism="native-apply",
        environment_id="podman-host",
        launch_id="at-handoff",
        source="native-runtime",
        observed_at=datetime.now(UTC).isoformat(),
        hosts=(),
        filesystem=tuple((item["path"], item["access"]) for item in plan["requirements"]),
        engine="podman",
        worker_image="harness-worker:local",
        worker_image_id=WORKER_ID,
    )


def test_only_an_operations_qa_container_makes_a_container_qa_plan(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    assert runtime_access.qa_container(_plan(repo), "qa")
    assert not runtime_access.qa_container(_plan(repo), "developer")
    # A project with one defaults.container keeps the historical host QA.
    assert not runtime_access.qa_container(_plan(repo, operations=False), "qa")


def test_a_container_qa_plan_pins_the_images_it_observes_as_the_qa_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    plan = _plan(repo)
    seen: list[str] = []

    def observe(
        self: object, observed: JsonObject, transport: str
    ) -> container_executor.ContainerObservation:
        seen.append(transport)
        return _observation(observed, transport)

    monkeypatch.setattr(container_executor.DockerExecutor, "observe", observe)

    images = runtime_access.pin_images(plan, "in-process", "qa", "work")

    assert images == {"worker": WORKER_ID, "proxy": None}
    assert seen == [container_executor.QA_OPERATION_TRANSPORT]
    assert runtime_access.pin_images(_plan(repo, operations=False), "in-process", "qa", "work") is None


def test_the_docker_executor_proves_the_qa_operation_but_no_other_coordinator_transport(
    tmp_path: Path,
) -> None:
    plan = _plan(_repo(tmp_path))
    blocked: list[str] = []
    container_executor._plan_support(
        plan, container_executor.QA_OPERATION_TRANSPORT, lambda reason, remedy: blocked.append(reason)
    )
    assert blocked == []
    container_executor._plan_support(plan, "in-process", lambda reason, remedy: blocked.append(reason))
    assert any("only an external dispatch" in reason for reason in blocked)


def test_the_qa_operation_access_proves_a_container_qa_mode_through_the_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.orchestration import operation_access

    plan = _plan(_repo(tmp_path))
    seen: list[str] = []

    def observe(
        self: object, observed: JsonObject, transport: str
    ) -> container_executor.ContainerObservation:
        seen.append(transport)
        return _observation(observed, transport)

    monkeypatch.setattr(container_executor.DockerExecutor, "observe", observe)

    assert operation_access._mode_check("qa", plan)["state"] == "verified"
    operation_access._mode_check("git", plan)
    assert seen == [container_executor.QA_OPERATION_TRANSPORT, "in-process"]


def _brief(plan: JsonObject, repo: Path) -> JsonObject:
    return {
        "role": "qa",
        "purpose": "work",
        "access": "read-only",
        "worktree": str(repo),
        "runtime_access": plan,
        "runtime_images": {"worker": WORKER_ID, "proxy": None},
        "qa_execution": {
            "supervisor": "harness-runtime",
            "command_timeout_seconds": 3600,
            "stop_grace_seconds": 30,
            "poll_interval_seconds": 5,
        },
        "transition": {"runtime_access_sha256": plan["plan_digest"]},
    }


def test_binding_refuses_a_container_qa_brief_without_its_pins_or_qa_execution(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    brief = _brief(_plan(repo), repo)
    runtime_access.validate_binding(brief)

    for missing in ("runtime_images", "qa_execution"):
        incomplete = {key: value for key, value in brief.items() if key != missing}
        with pytest.raises(runtime_access.AccessError):
            runtime_access.validate_binding(incomplete)
    broken = {**brief, "qa_execution": {**brief["qa_execution"], "command_timeout_seconds": 0}}
    with pytest.raises(runtime_access.AccessError, match="qa_execution"):
        runtime_access.validate_binding(broken)


def test_a_historical_qa_brief_keeps_its_host_path_without_any_pin(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    plan = _plan(repo, operations=False)
    brief = {
        "role": "qa",
        "purpose": "work",
        "access": "read-only",
        "worktree": str(repo),
        "runtime_access": plan,
        "transition": {"runtime_access_sha256": plan["plan_digest"]},
    }
    runtime_access.validate_binding(brief)
    with pytest.raises(runtime_access.AccessError):
        runtime_access.validate_binding({**brief, "qa_execution": _brief(plan, repo)["qa_execution"]})


class _Hello:
    def __init__(self, features: list[str]) -> None:
        self.hello = {"features": features}


class _Client:
    features: list[str] = []

    def __init__(self, repo: Path) -> None:
        self.paths = replace(type("P", (), {})(), ) if False else type("P", (), {"repo_id": "r"})()

    def call(self, operation: str, **_: object) -> _Hello:
        return _Hello(self.features)


def test_qa_execution_pins_the_command_timeout_only_where_the_runtime_serves_qa_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    plan = _plan(repo)
    brief = _brief(plan, repo)
    config: JsonObject = {"execution_policy": {"qa_command_timeout_seconds": 900}}
    monkeypatch.setattr(dispatch_workflow, "store_is_authority", lambda root: True)
    from harness.orchestration import runtime_client

    monkeypatch.setattr(runtime_client, "RuntimeClient", _Client)
    _Client.features = ["status", "qa.run", "qa.stop"]

    pinned = dispatch_workflow._qa_execution_section(repo, tmp_path, config, brief)

    assert pinned == {
        "supervisor": "harness-runtime",
        "command_timeout_seconds": 900,
        "stop_grace_seconds": _execution_policy(config)["stop_grace_seconds"],
        "poll_interval_seconds": _execution_policy(config)["dispatch_poll_interval_seconds"],
    }
    assert _execution_policy({})["qa_command_timeout_seconds"] == 3600
    historical = {**brief, "runtime_access": _plan(repo, operations=False)}
    assert dispatch_workflow._qa_execution_section(repo, tmp_path, config, historical) is None

    _Client.features = ["status", "execution.launch"]
    with pytest.raises(CoordinatorError, match="qa.run"):
        dispatch_workflow._qa_execution_section(repo, tmp_path, config, brief)
    monkeypatch.setattr(dispatch_workflow, "store_is_authority", lambda root: False)
    with pytest.raises(CoordinatorError, match="runtime store"):
        dispatch_workflow._qa_execution_section(repo, tmp_path, config, brief)
