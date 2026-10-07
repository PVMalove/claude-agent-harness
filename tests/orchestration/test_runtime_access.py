"""Real-path and replacement semantics of the project access plan."""

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
