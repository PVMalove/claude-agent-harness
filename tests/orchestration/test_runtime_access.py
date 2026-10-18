"""Real-path and replacement semantics of the project access plan."""

import hashlib
import json
import subprocess
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from harness.orchestration import extensions, runtime_access
from harness.orchestration.core.utils import JsonObject


def test_role_components_replace_defaults_and_keep_operational_roots(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    plan = runtime_access.resolve_plan(
        repo,
        repo,
        {
            "access_policy": {
                "defaults": {
                    "mode": "sandbox",
                    "network": {"hosts": ["github.com"]},
                    "filesystem": [
                        {"resource": "cache", "path": ".cache/uv", "access": "write"}
                    ],
                },
                "roles": {"developer": {"network": {"hosts": []}, "filesystem": []}},
            }
        },
        "developer",
        "write",
    )
    assert plan["mode"] == "sandbox"
    assert plan["network"] == {"hosts": []}
    assert plan["filesystem"] == []
    assert plan["sources"]["network"] == "roles.developer"
    assert {item["path"] for item in plan["requirements"]} == {
        str(repo),
        str(repo / ".git"),
        str(repo / ".harness"),
        str(repo / ".harness/.sandboxes/scratch"),
    }
    assert len(plan["plan_digest"]) == 64


def test_native_apply_requires_matching_worker_proof_and_runs_native_action(
    tmp_path: Path,
) -> None:
    from dataclasses import replace
    from datetime import UTC, datetime

    from harness.orchestration import extensions

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    native_modes: list[str] = []

    class NativeRuntime:
        def observe(
            self, plan: JsonObject, transport: str
        ) -> extensions.RuntimeAccessObservation:
            return extensions.RuntimeAccessObservation(
                plan_digest=plan["plan_digest"],
                transport=transport,
                supported_modes=("sandbox",),
                effective_mode="sandbox",
                mechanism="native-apply",
                environment_id="worker-session",
                launch_id="reserved-worker",
                source="native-runtime",
                observed_at=datetime.now(UTC).isoformat(),
                hosts=(),
                filesystem=tuple(
                    (item["path"], item["access"]) for item in plan["requirements"]
                ),
            )

        def apply(
            self, brief: JsonObject, observation: extensions.RuntimeAccessObservation
        ) -> extensions.RuntimeAccessObservation:
            native_modes.append(brief["runtime_access"]["mode"])
            return replace(observation, applied=True)

    extensions.register("runtime_access", "test-native", NativeRuntime())
    try:
        plan = runtime_access.resolve_plan(
            repo,
            repo,
            {
                "access_policy": {"defaults": {"mode": "sandbox"}},
                "extensions": {"runtime_access": "test-native"},
            },
            "developer",
            "write",
        )
        proof = runtime_access.apply_plan({"runtime_access": plan}, "in-process")
        assert proof["evidence"]["applied"] is True
        assert native_modes == ["sandbox"]
    finally:
        extensions.unregister("runtime_access", "test-native")


def test_access_never_grants_the_filesystem_root_the_home_directory_or_its_parents(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    home = Path.home().resolve()
    for forbidden in (Path(home.anchor), home, home.parent):
        config = {
            "access_policy": {
                "defaults": {
                    "mode": "sandbox",
                    "filesystem": [
                        {"resource": "cache", "path": str(forbidden), "access": "write"}
                    ],
                }
            }
        }
        with pytest.raises(
            runtime_access.AccessError, match="entire filesystem or home directory"
        ):
            runtime_access.resolve_plan(repo, repo, config, "developer", "write")


def test_a_failing_provider_leaves_the_plan_unverified_instead_of_crashing_preflight(
    tmp_path: Path,
) -> None:
    from harness.orchestration import extensions

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    class Failing:
        def observe(self, plan: JsonObject, transport: str) -> None:
            raise RuntimeError("native runtime unavailable")

    extensions.register("runtime_access", "test-failing", Failing())
    try:
        plan = runtime_access.resolve_plan(
            repo,
            repo,
            {
                "access_policy": {"defaults": {"mode": "sandbox"}},
                "extensions": {"runtime_access": "test-failing"},
            },
            "developer",
            "write",
        )
        summary, observation = runtime_access.verify_plan(plan, "in-process")
    finally:
        extensions.unregister("runtime_access", "test-failing")
    assert observation is None
    assert summary["status"] == "unverified"
    assert summary["remedy"]


def test_operation_override_follows_the_dispatch_role_and_purpose(
    tmp_path: Path,
) -> None:
    assert runtime_access.dispatch_operation("qa", "work") == "qa"
    assert runtime_access.dispatch_operation("developer", "publish") == "publish"
    assert runtime_access.dispatch_operation("developer", "work") is None
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    config = {
        "access_policy": {
            "defaults": {"mode": "sandbox", "network": {"hosts": ["github.com"]}},
            "operations": {"qa": {"network": {"hosts": []}}},
        }
    }
    qa = runtime_access.resolve_plan(
        repo,
        repo,
        config,
        "qa",
        "read-only",
        operation=runtime_access.dispatch_operation("qa", "work"),
    )
    developer = runtime_access.resolve_plan(
        repo,
        repo,
        config,
        "developer",
        "write",
        operation=runtime_access.dispatch_operation("developer", "work"),
    )
    assert qa["network"] == {"hosts": []}
    assert qa["sources"]["network"] == "operations.qa"
    assert developer["network"] == {"hosts": ["github.com"]}


_Observation = extensions.RuntimeAccessObservation

_REFUSALS: list[tuple[Callable[[_Observation], _Observation], str]] = [
    (lambda o: replace(o, plan_digest="f" * 64), "different plan or transport"),
    (lambda o: replace(o, transport="external"), "different plan or transport"),
    (
        lambda o: replace(o, source="parent-process"),
        "launch identity or application mechanism",
    ),
    (
        lambda o: replace(o, observed_at="2000-01-01T00:00:00+00:00"),
        "stale",
    ),
    (lambda o: replace(o, observed_at="not-a-time"), "timestamp is invalid"),
    (lambda o: replace(o, supported_modes=()), "does not support requested mode"),
    (lambda o: replace(o, effective_mode="unsandboxed"), "effective mode differs"),
    (lambda o: replace(o, hosts=()), "network host requirements are unverified"),
    (lambda o: replace(o, filesystem=()), "filesystem requirements are unverified"),
]


@pytest.mark.parametrize(("mutate", "reason"), _REFUSALS)
def test_a_worker_proof_that_does_not_match_the_plan_is_refused(
    tmp_path: Path,
    mutate: Callable[[_Observation], _Observation],
    reason: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    class Mutated:
        def observe(
            self, plan: JsonObject, transport: str
        ) -> extensions.RuntimeAccessObservation:
            honest = extensions.RuntimeAccessObservation(
                plan_digest=plan["plan_digest"],
                transport=transport,
                supported_modes=("sandbox",),
                effective_mode="sandbox",
                mechanism="native-apply",
                environment_id="worker-session",
                launch_id="reserved-worker",
                source="native-runtime",
                observed_at=datetime.now(UTC).isoformat(),
                hosts=tuple(plan["network"]["hosts"]),
                filesystem=tuple(
                    (item["path"], item["access"]) for item in plan["requirements"]
                ),
            )
            return mutate(honest)

    extensions.register("runtime_access", "test-mutated", Mutated())
    try:
        plan = runtime_access.resolve_plan(
            repo,
            repo,
            {
                "access_policy": {
                    "defaults": {
                        "mode": "sandbox",
                        "network": {"hosts": ["github.com"]},
                    }
                },
                "extensions": {"runtime_access": "test-mutated"},
            },
            "developer",
            "write",
        )
        summary, _ = runtime_access.verify_plan(plan, "in-process")
        with pytest.raises(runtime_access.AccessError, match=reason):
            runtime_access.apply_plan({"runtime_access": plan}, "in-process")
    finally:
        extensions.unregister("runtime_access", "test-mutated")
    assert summary["status"] == "unverified"
    assert reason in str(summary["reason"])


def test_a_historical_brief_keeps_inherit_and_a_read_only_role_cannot_write(
    tmp_path: Path,
) -> None:
    runtime_access.validate_binding({"role": "architect", "transition": {}})
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    plan = runtime_access.resolve_plan(
        repo,
        repo,
        {"access_policy": {"defaults": {"mode": "sandbox"}}},
        "developer",
        "write",
    )
    brief = {
        "access": "read-only",
        "worktree": str(repo),
        "runtime_access": plan,
        "transition": {"runtime_access_sha256": plan["plan_digest"]},
    }
    with pytest.raises(
        runtime_access.AccessError, match="checkout or read-only boundary"
    ):
        runtime_access.validate_binding(brief)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def test_a_coordinator_operation_ignores_role_overrides_and_selects_its_own(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    config = {
        "access_policy": {
            "defaults": {"mode": "inherit", "network": {"hosts": ["github.com"]}},
            "roles": {
                "qa": {"mode": "sandbox", "network": {"hosts": []}},
                "developer": {"mode": "unsandboxed"},
            },
            "operations": {"git": {"mode": "unsandboxed"}},
        }
    }
    qa = runtime_access.resolve_plan(
        repo, repo, config, "qa", "read-only", operation="qa"
    )
    publish = runtime_access.resolve_plan(
        repo, repo, config, "developer", "write", operation="publish"
    )
    git = runtime_access.resolve_plan(
        repo, repo, config, None, "write", operation="git"
    )
    role = runtime_access.resolve_plan(repo, repo, config, "qa", "read-only")
    assert (qa["mode"], qa["sources"]["mode"]) == ("inherit", "defaults")
    assert qa["network"] == {"hosts": ["github.com"]}
    assert (publish["mode"], publish["sources"]["mode"]) == ("inherit", "defaults")
    assert (git["mode"], git["sources"]["mode"]) == ("unsandboxed", "operations.git")
    # A worker role still receives its own override.
    assert (role["mode"], role["sources"]["mode"]) == ("sandbox", "roles.qa")


def test_a_coordinator_operation_requires_write_to_git_metadata_and_storage(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    config = {"access_policy": {"defaults": {"mode": "inherit"}}}
    qa = runtime_access.resolve_plan(
        repo, repo, config, "qa", "read-only", operation="qa"
    )
    worker = runtime_access.resolve_plan(repo, repo, config, "qa", "read-only")
    writes = {
        item["resource"] for item in qa["requirements"] if item["access"] == "write"
    }
    assert {"git_common", "shared_storage", "artifacts"} <= writes
    assert "checkout" not in writes
    assert not {"git_common", "shared_storage"} & {
        item["resource"] for item in worker["requirements"] if item["access"] == "write"
    }


def test_a_coordinator_operation_may_write_where_a_read_only_role_may_not(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    config = {
        "access_policy": {
            "defaults": {
                "mode": "inherit",
                "filesystem": [{"resource": "git_common", "access": "write"}],
            }
        }
    }
    with pytest.raises(runtime_access.AccessError, match="read-only role"):
        runtime_access.resolve_plan(repo, repo, config, "qa", "read-only")
    plan = runtime_access.resolve_plan(
        repo, repo, config, "qa", "read-only", operation="qa"
    )
    assert plan["filesystem"] == [
        {"resource": "git_common", "access": "write", "path": str(repo / ".git")}
    ]


def test_a_qa_brief_binds_the_write_requirements_of_its_coordinator_operation(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    config = {"access_policy": {"defaults": {"mode": "inherit"}}}
    plan = runtime_access.resolve_plan(
        repo, repo, config, "qa", "read-only", operation="qa"
    )
    brief = {
        "role": "qa",
        "access": "read-only",
        "worktree": str(repo),
        "runtime_access": plan,
        "transition": {"runtime_access_sha256": plan["plan_digest"]},
    }
    runtime_access.validate_binding(brief)
    with pytest.raises(
        runtime_access.AccessError, match="checkout or read-only boundary"
    ):
        runtime_access.validate_binding({**brief, "role": "code-review"})


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired(["git"], 60),
        FileNotFoundError(2, "No such file or directory", "git"),
    ],
)
def test_a_git_metadata_lookup_that_hangs_or_cannot_start_is_an_access_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    timeouts: list[object] = []

    def stalled(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        timeouts.append(kwargs.get("timeout"))
        raise failure

    monkeypatch.setattr(subprocess, "run", stalled)
    with pytest.raises(runtime_access.AccessError) as caught:
        runtime_access.resolve_plan(
            tmp_path,
            tmp_path,
            {"access_policy": {"defaults": {"mode": "inherit"}}},
            "qa",
            "read-only",
            operation="qa",
        )

    assert "shared Git metadata" in caught.value.message
    assert caught.value.__cause__ is failure
    assert caught.value.remedy.strip()
    assert len(timeouts) == 1
    assert isinstance(timeouts[0], int) and timeouts[0] > 0


_HISTORICAL_PLAN_FIELDS = {
    "mode",
    "network",
    "filesystem",
    "sources",
    "requirements",
    "runtime_extension",
    "plan_digest",
}


def _redigest(plan: JsonObject) -> JsonObject:
    """A tampered plan with a matching checksum, so only its shape can refuse it."""
    plan["plan_digest"] = runtime_access._digest(plan)
    return plan


def test_the_container_component_is_replaced_whole_by_each_layer_without_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    commands: list[list[str]] = []
    real_run = subprocess.run

    def recorded(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(list(command))
        return real_run(command, capture_output=True, text=True, check=False)

    monkeypatch.setattr(subprocess, "run", recorded)
    config = {
        "access_policy": {
            "defaults": {
                "mode": "sandbox",
                "container": {"worker_image": "worker:base", "proxy_image": "proxy:1"},
            },
            "roles": {"developer": {"container": {"worker_image": "worker:dev"}}},
            "operations": {
                "qa": {"container": {"engine": "podman", "worker_image": "worker:qa"}}
            },
        },
        "extensions": {"runtime_access": "docker"},
    }

    architect = runtime_access.resolve_plan(
        repo, repo, config, "architect", "read-only"
    )
    developer = runtime_access.resolve_plan(repo, repo, config, "developer", "write")
    qa = runtime_access.resolve_plan(
        repo, repo, config, "developer", "read-only", operation="qa"
    )

    assert (architect["container"], architect["sources"]["container"]) == (
        {"engine": "docker", "worker_image": "worker:base", "proxy_image": "proxy:1"},
        "defaults",
    )
    assert (developer["container"], developer["sources"]["container"]) == (
        {"engine": "docker", "worker_image": "worker:dev"},
        "roles.developer",
    )
    assert (qa["container"], qa["sources"]["container"]) == (
        {"engine": "podman", "worker_image": "worker:qa"},
        "operations.qa",
    )
    for plan in (architect, developer, qa):
        runtime_access.validate_plan(plan)
        assert plan["runtime_extension"] == "docker"
    assert commands and all(command[0] == "git" for command in commands)


def test_a_plan_without_a_container_keeps_its_historical_shape(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    authored = runtime_access.resolve_plan(
        repo,
        repo,
        {"access_policy": {"defaults": {"mode": "sandbox"}}},
        "qa",
        "read-only",
    )
    legacy = runtime_access.resolve_plan(repo, repo, {}, "qa", "read-only")

    for plan in (authored, legacy):
        runtime_access.validate_plan(plan)
        assert set(plan) == _HISTORICAL_PLAN_FIELDS
        assert set(plan["sources"]) == {"mode", "network", "filesystem"}
        assert (
            plan["plan_digest"]
            == hashlib.sha256(
                json.dumps(
                    {key: value for key, value in plan.items() if key != "plan_digest"},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        )


def _tampered_container_plans(plan: JsonObject) -> list[JsonObject]:
    def changed(edit: Callable[[JsonObject], object]) -> JsonObject:
        copy: JsonObject = json.loads(json.dumps(plan))
        edit(copy)
        return _redigest(copy)

    legacy: JsonObject = {
        "mode": "inherit",
        "network": {"hosts": []},
        "filesystem": [],
        "sources": {
            key: "legacy" for key in ("mode", "network", "filesystem", "container")
        },
        "requirements": [],
        "runtime_extension": "none",
        "container": {"engine": "docker", "worker_image": "w"},
    }
    return [
        changed(lambda p: p["sources"].pop("container")),
        changed(lambda p: p.pop("container")),
        changed(lambda p: p["container"].pop("engine")),
        changed(lambda p: p["container"].update(engine="kubernetes")),
        changed(lambda p: p["container"].update(worker_image="-privileged")),
        changed(lambda p: p["container"].update(environment=["TOKEN"])),
        _redigest(legacy),
    ]


def test_a_tampered_container_plan_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    plan = runtime_access.resolve_plan(
        repo,
        repo,
        {
            "access_policy": {
                "defaults": {"mode": "sandbox", "container": {"worker_image": "w"}}
            }
        },
        "developer",
        "write",
    )
    runtime_access.validate_plan(plan)
    for tampered in _tampered_container_plans(plan):
        with pytest.raises(runtime_access.AccessError):
            runtime_access.validate_plan(tampered)


def test_the_built_in_docker_name_selects_the_shipped_executor_and_cannot_be_taken() -> (
    None
):
    from harness.orchestration import container_executor

    provider = extensions.runtime_access("docker")

    assert isinstance(provider, container_executor.DockerExecutor)
    assert extensions.runtime_access("docker") is provider
    with pytest.raises(extensions.ExtensionError, match="invalid extension name"):
        extensions.register("runtime_access", "docker", provider)


def test_a_provider_blocker_reaches_preflight_and_send_with_its_remedy_and_facts(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)

    class Blocking:
        def observe(self, plan: JsonObject, transport: str) -> None:
            raise extensions.RuntimeAccessBlocked(
                "the worker engine is gone",
                remedy="start the worker engine",
                facts={"engine": "test", "blockers": ["gone"]},
            )

    extensions.register("runtime_access", "test-blocking", Blocking())
    try:
        plan = runtime_access.resolve_plan(
            repo,
            repo,
            {
                "access_policy": {"defaults": {"mode": "sandbox"}},
                "extensions": {"runtime_access": "test-blocking"},
            },
            "developer",
            "write",
        )
        summary, observation = runtime_access.verify_plan(plan, "external")
        with pytest.raises(runtime_access.AccessError) as refused:
            runtime_access.apply_plan({"runtime_access": plan}, "external")
    finally:
        extensions.unregister("runtime_access", "test-blocking")

    assert observation is None
    assert (summary["status"], summary["reason"], summary["remedy"]) == (
        "unverified",
        "the worker engine is gone",
        "start the worker engine",
    )
    assert summary["environment"] == {"engine": "test", "blockers": ["gone"]}
    assert (refused.value.message, refused.value.remedy) == (
        "the worker engine is gone",
        "start the worker engine",
    )


@pytest.mark.parametrize("policy", ["not an object", {"defaults": "not an object"}])
def test_a_policy_shape_the_validator_missed_is_an_internal_invariant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: object
) -> None:
    from harness.errors import INTERNAL_INVARIANT_REMEDY

    monkeypatch.setattr(
        runtime_access, "access_policy_problems", lambda value, roles: []
    )
    with pytest.raises(runtime_access.AccessError) as caught:
        runtime_access.resolve_plan(
            tmp_path, tmp_path, {"access_policy": policy}, None, "read-only"
        )

    assert caught.value.remedy == INTERNAL_INVARIANT_REMEDY
