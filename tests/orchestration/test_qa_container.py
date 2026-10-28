"""Container QA at seam 1 (ADR 0022, section 3; ADR 0025): ``qa run`` of the coordinator CLI as
its own process, the real ``harness-runtime``, real Git worktrees and the real store; only the
container engine is the scripted ``podman`` on the runtime's ``PATH``.

Every QA command runs in its own foreground ``run --rm --init`` container that the runtime starts;
Python builds no engine argv. The streams of each command come back as separate stage evidence
with the command's real exit code, and the classified outcome decides the report: a failed gate is
``failed``, tooling, timeout and unknown endings are ``blocked`` with their domain, and an
infrastructure ending leaves no report but a new attempt of the same dispatch.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

import pytest

from harness.json_types import JsonObject
from tests.orchestration import test_coordinator as coordinator_tests
from tests.orchestration.runtime_store_support import (
    STATE_REL,
    ServingRuntime,
    coordinator_with,
    migrate,
    store_call,
    store_records,
)
from tests.orchestration.scripted_engine import ScriptedEngine

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="container QA runs only on Linux",
)

WORKER_TAG = "harness-worker:local"
WORKER_BARE = "a" * 64
GATE = ["make lint", "make test", "make docs"]
PREPARATION = ["make bootstrap"]
SECRET_NAME = "HARNESS_TEST_QA_TOKEN"
SECRET = "synthetic-qa-secret-7f3a"
PROXY_TAG = "harness-proxy:local"


def _git(repo: Path, *arguments: str) -> None:
    coordinator_tests._git(repo, *arguments)


class ContainerQa:
    """A cut-over repository whose approved QA dispatch pins a container ``operations.qa`` plan,
    the runtime that serves it and the scripted engine on its ``PATH``."""

    def __init__(
        self,
        tmp: Path,
        *,
        preparation: list[str] | None = None,
        timeout: int = 600,
        hosts: list[str] | None = None,
        cache: Path | None = None,
        lease_ttl: int | None = None,
    ) -> None:
        self.fx = coordinator_tests.CoordinatorRetryRoutingTests()
        self.fx.setUp()
        repo = self.repo = self.fx.repo
        (repo / ".harness/project.json").write_text(
            json.dumps({"qa_gate_commands": GATE}), encoding="utf-8"
        )
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "declare the QA gate")
        _git(repo, "push", "origin", "master")
        self.fx.state_dir = repo / STATE_REL
        self.batch = str(self.fx._create_batch()["batch_id"])
        self.fx._accepted_architect(self.batch)
        self.candidate = self.fx._accepted_candidate(self.batch)
        self.fx._reported_review(self.batch, self.candidate)
        self.fx._decide(self.batch, "accept")
        container: JsonObject = {"engine": "podman", "worker_image": WORKER_TAG}
        if hosts:
            container["proxy_image"] = PROXY_TAG
        qa: JsonObject = {"container": container, "environment": [SECRET_NAME]}
        if hosts is not None:
            qa["network"] = {"hosts": hosts}
        if cache is not None:
            qa["filesystem"] = [{"resource": "cache", "path": str(cache), "access": "write"}]
        config: JsonObject = {
            "access_policy": {
                "defaults": {"mode": "sandbox", "container": container},
                "operations": {"qa": qa},
            },
            "extensions": {"runtime_access": "docker"},
            "verification_commands": GATE,
            "execution_policy": {
                "qa_command_timeout_seconds": timeout,
                "stop_grace_seconds": 1,
                "dispatch_poll_interval_seconds": 1,
            },
        }
        if lease_ttl is not None:
            config["lease_ttl_seconds"] = lease_ttl
        if preparation is not None:
            config["qa_preparation"] = preparation
            config["qa_environment_probes"] = ["probe registry"]
            config["qa_project_file_checks"] = ["check lock"]
        (repo / ".harness/orchestration.json").write_text(
            json.dumps(config), encoding="utf-8"
        )
        (repo / ".harness/.sandboxes/scratch").mkdir(parents=True, exist_ok=True)
        self.engine = ScriptedEngine(tmp / "engine")
        self.engine.update(
            lambda state: state["images"].update({WORKER_TAG: WORKER_BARE, PROXY_TAG: "b" * 64})
        )
        assert migrate(repo).returncode == 0
        self.runtime = ServingRuntime(
            repo, env={"PATH": str(self.engine.bin), SECRET_NAME: SECRET}
        )
        self.dispatch = self.create()

    def run(self, *command: str) -> tuple[int, JsonObject | None, str]:
        result = coordinator_with(self.repo, self.runtime, *command)
        parsed = json.loads(result.stdout) if result.returncode == 0 else None
        return result.returncode, parsed, result.stderr

    def create(self) -> str:
        fields = [
            "--batch", self.batch, "--role", "qa", "--candidate-commit", self.candidate,
            "--runtime", "claude", "--model", "sonnet", "--effort", "high",
        ]  # fmt: skip
        code, proposal, err = self.run("dispatch", "propose", *fields)
        assert code == 0 and proposal is not None, err
        code, created, err = self.run(
            "dispatch", "create", *fields,
            "--transition-digest", str(proposal["transition_digest"]),
            "--approved-by", "Malove", "--approved-at", self.fx.APPROVED_AT,
        )  # fmt: skip
        assert code == 0 and created is not None, err
        self.brief: JsonObject = created["brief"]
        assert "qa_execution" in self.brief, self.brief
        return str(self.brief["dispatch_id"])

    def qa_run(self) -> tuple[int, JsonObject | None, str]:
        return self.run("qa", "run", "--dispatch", self.dispatch)

    def attempts(self) -> list[JsonObject]:
        """The ``qa-lane/attempts`` records of the dispatch, read from the store."""
        answer = store_call(self.repo, self.runtime, "state.read", {"collections": ["qa-lane"]})
        records = answer["records"]
        assert isinstance(records, dict)
        return [
            record
            for path, record in records.items()
            if path.startswith("qa-lane/attempts/")
            and isinstance(record, dict)
            and record.get("dispatch_id") == self.dispatch
        ]

    def status(self) -> JsonObject:
        path = f"dispatch-status/{self.dispatch}.json"
        return store_records(self.repo, self.runtime, path)[path]

    def report(self) -> JsonObject:
        path = f"reports/{self.dispatch}.json"
        return store_records(self.repo, self.runtime, path)[path]

    def lane(self) -> JsonObject:
        code, lane, err = self.run("qa", "status")
        assert code == 0 and lane is not None, err
        return lane

    def execution(self) -> JsonObject:
        answer = store_call(
            self.repo, self.runtime, "execution.observe", {"dispatch_id": self.dispatch}
        )
        execution = answer["execution"]
        assert isinstance(execution, dict), answer
        return execution

    def close(self) -> None:
        self.runtime.stop()
        self.fx.tearDown()


class Make(Protocol):
    def __call__(
        self,
        *,
        preparation: list[str] | None = ...,
        timeout: int = ...,
        hosts: list[str] | None = ...,
        cache: Path | None = ...,
        lease_ttl: int | None = ...,
    ) -> ContainerQa: ...


@pytest.fixture
def qa(tmp_path: Path) -> Iterator[Make]:
    made: list[ContainerQa] = []

    def make(
        *,
        preparation: list[str] | None = None,
        timeout: int = 600,
        hosts: list[str] | None = None,
        cache: Path | None = None,
        lease_ttl: int | None = None,
    ) -> ContainerQa:
        root = tmp_path / f"q{len(made)}"
        options = {"timeout": timeout, "hosts": hosts, "cache": cache, "lease_ttl": lease_ttl}
        made.append(ContainerQa(root, preparation=preparation, **options))  # type: ignore[arg-type]
        return made[-1]

    yield make
    for item in made:
        item.close()


def _flag(argv: list[str], flag: str) -> list[str]:
    return [value for name, value in zip(argv, argv[1:]) if name == flag]


def test_each_command_runs_in_its_own_container_and_a_failed_gate_fails_the_report(
    qa: Make,
) -> None:
    repo = qa()
    repo.engine.script("gate-1", stdout="lint ok\n", stderr="lint warn\n", exit_code=0)
    repo.engine.script("gate-2", stdout="2 failed\n", stderr="trace\n", exit_code=1)

    code, result, err = repo.qa_run()

    assert code == 0 and result is not None, err
    report = repo.report()
    assert report["outcome"] == "failed"
    assert [(c["command"], c["result"]) for c in report["checks_run"]] == [
        ("make lint", "pass"), ("make test", "fail"), ("make docs", "not_run"),
    ]
    runs = repo.engine.runs()
    assert len(runs) == 2
    for run in runs:
        assert run[:4] == ["podman", "run", "--rm", "--init"]
        assert _flag(run, "--network") == ["none"]
        assert "--detach" not in run and not any("volume" in part for part in run)
    assert runs[0][-3:] == ["-c", "make lint"][:0] + runs[0][-3:]
    assert not any(call[1] in ("commit", "push", "fetch") for call in repo.engine.calls())
    lane = repo.lane()
    assert (lane["lease"], lane["queue"]) == (None, [])
    execution = repo.execution()
    assert (execution["kind"], execution["state"], execution["attempt"]) == ("qa", "exited", 1)
    records = execution["exit"]["commands"]
    assert [(r["stage_id"], r["result"], r["exit_code"]) for r in records] == [
        ("gate-1", "ran", 0), ("gate-2", "ran", 1), ("gate-3", "not_run", None),
    ]


def test_an_infrastructure_ending_is_a_new_attempt_without_a_report_or_a_developer_retry(
    qa: Make,
) -> None:
    repo = qa()
    repo.engine.script("gate-1", stderr="Error: OCI runtime error\n", exit_code=125)

    code, _, err = repo.qa_run()

    assert code == 2 and "attempt 2" in err, err
    [attempt] = repo.attempts()
    assert (attempt["attempt"], attempt["domain"]) == (1, "infrastructure")
    assert attempt["stage_evidence"].startswith("stage-evidence: qa-artifacts/")
    assert repo.status() | {"updated_at": None} == {
        "dispatch_id": repo.dispatch, "state": "approved", "attempt": 1, "updated_at": None,
    }
    lane = repo.lane()
    assert (lane["lease"], lane["queue"]) == (None, [])
    first = repo.execution()["execution_id"]

    repo.engine.script("gate-1", exit_code=0)
    code, result, err = repo.qa_run()

    assert code == 0 and result is not None, err
    assert repo.report()["outcome"] == "completed"
    execution = repo.execution()
    assert execution["attempt"] == 2 and execution["execution_id"] != first
    path = f"batches/{repo.batch}.json"
    dispatches = store_records(repo.repo, repo.runtime, path)[path]["dispatches"]
    assert isinstance(dispatches, list)
    assert [d["role"] for d in dispatches if isinstance(d, dict)].count("developer") == 1


@pytest.mark.parametrize(
    ("code", "hang", "domain"), [(127, False, "tooling"), (126, False, "tooling"), (0, True, "timeout")]
)
def test_tooling_and_timeout_endings_block_the_report_with_their_domain(
    qa: Make, code: int, hang: bool, domain: str
) -> None:
    repo = qa(timeout=1)
    repo.engine.script("gate-2", stderr="sh: make: not found\n", exit_code=code, hang=hang)

    status, result, err = repo.qa_run()

    assert status == 0 and result is not None, err
    report = repo.report()
    assert report["outcome"] == "blocked"
    assert domain in report["blockers"]["details"]
    assert [c["result"] for c in report["checks_run"]] == ["pass", "fail", "not_run"]
    records = repo.execution()["exit"]["commands"]
    assert (records[1]["domain"], records[1]["exit_code"]) == (domain, 143 if hang else code)
    if hang:
        stops = [call for call in repo.engine.calls() if call[1] == "stop"]
        assert stops and stops[0][2:4] == ["--time", "1"]
    lane = repo.lane()
    assert (lane["lease"], lane["queue"]) == (None, [])


def test_a_failed_preparation_in_a_broken_environment_is_a_new_attempt(
    qa: Make,
) -> None:
    repo = qa(preparation=PREPARATION)
    repo.engine.script("preparation-1", stderr="network unreachable\n", exit_code=2)
    repo.engine.script("environment-probe-1", exit_code=6)

    code, _, err = repo.qa_run()

    assert code == 2, err
    [attempt] = repo.attempts()
    assert attempt["domain"] == "environment_preparation"
    stages = [call[call.index("--name") + 1].rsplit("-qa1-", 1)[1] for call in repo.engine.runs()]
    assert stages == ["preparation-1", "environment-probe-1", "project-file-check-1"]
    assert repo.status()["state"] == "approved"


def test_the_runs_share_one_sidecar_and_network_and_mount_the_pinned_cache(
    qa: Make, tmp_path: Path
) -> None:
    cache = tmp_path / "uv-cache"
    cache.mkdir()
    repo = qa(hosts=["pypi.org"], cache=cache)
    plan = repo.brief["runtime_access"]
    # A config edit after the approval widens nothing: the runtime applies the pinned plan.
    config_path = repo.repo / ".harness/orchestration.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["access_policy"]["operations"]["qa"]["filesystem"].append(
        {"resource": "cache", "path": str(tmp_path), "access": "write"}
    )
    config_path.write_text(json.dumps(config), encoding="utf-8")

    code, result, err = repo.qa_run()

    assert code == 0 and result is not None, err
    assert repo.report()["outcome"] == "completed"
    assert repo.brief["runtime_access"] == plan
    calls = repo.engine.calls()
    runs = repo.engine.runs()
    assert len(runs) == 3
    assert len([c for c in calls if c[1:3] == ["network", "create"]]) == 1
    assert len([c for c in calls if c[1] == "create" and "--name" in c]) == 1
    networks = {tuple(_flag(run, "--network")) for run in runs}
    assert len(networks) == 1 and networks != {("none",)}
    for run in runs:
        mounts = _flag(run, "--mount")
        assert f"type=bind,source={cache},target={cache}" in mounts
        assert not any(f"source={tmp_path}," in mount for mount in mounts)
        assert SECRET not in json.dumps(run) and _flag(run, "--env").count(SECRET_NAME) == 1
    assert not any("volume" in part for call in calls for part in call)
    assert repo.engine.state()["objects"] == {}


@pytest.mark.parametrize("change", ["rebuilt image", "missing environment"])
def test_a_rebuilt_image_or_a_missing_variable_blocks_the_run_before_any_command(
    qa: Make, change: str
) -> None:
    repo = qa()
    if change == "rebuilt image":
        repo.engine.update(lambda state: state["images"].update({WORKER_TAG: "c" * 64}))
    else:
        repo.runtime.stop()
        repo.runtime = ServingRuntime(repo.repo, env={"PATH": str(repo.engine.bin)})

    code, _, err = repo.qa_run()

    assert code == 2, err
    assert ("approval_mismatch" if change == "rebuilt image" else SECRET_NAME) in err
    assert repo.engine.runs() == []
    assert repo.status()["state"] == "approved"
    lane = repo.lane()
    assert (lane["lease"], lane["queue"]) == (None, [])
