"""The built-in docker executor at seam 1: the coordinator CLI as its own process, the real
``harness-runtime`` and a scripted ``podman`` (ADR 0022, ADR 0025).

The runtime's ``PATH`` holds only the scripted engine, so no ``docker`` is reachable: every engine
command recorded here is one the runtime ran, and Python never builds an engine argv or starts an
engine itself.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

import pytest

from harness.json_types import JsonObject
from harness.orchestration import container_executor, runtime_access
from tests.orchestration.runtime_store_support import Supervised
from tests.orchestration.scripted_engine import WORKER_ID as WORKER_CONTAINER
from tests.orchestration.scripted_engine import ScriptedEngine

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="the docker executor runs only on Linux",
)

WORKER_TAG = "harness-worker:local"
# Podman prints a bare image id; the brief pins ``sha256:<id>`` as Docker prints it.
WORKER_BARE = "a" * 64
WORKER_ID = f"sha256:{WORKER_BARE}"
SECRET_NAME = "HARNESS_TEST_API_KEY"
SECRET = "synthetic-secret-value-5d1e"


def policy(**defaults: object) -> JsonObject:
    return {
        "defaults": {
            "mode": "sandbox",
            "container": {"engine": "podman", "worker_image": WORKER_TAG},
            "environment": [SECRET_NAME],
            **defaults,
        }
    }


class Podman(Protocol):
    def __call__(
        self,
        *,
        access_policy: JsonObject | None = ...,
        env: dict[str, str] | None = ...,
        create: bool = ...,
    ) -> tuple[Supervised, ScriptedEngine]: ...


@pytest.fixture
def podman(tmp_path: Path) -> Iterator[Podman]:
    """A cut-over repository under the docker executor, served by a runtime whose only engine is
    a scripted rootful ``podman`` that holds the worker image."""
    made: list[Supervised] = []

    def make(
        *,
        access_policy: JsonObject | None = None,
        env: dict[str, str] | None = None,
        create: bool = True,
    ) -> tuple[Supervised, ScriptedEngine]:
        root = tmp_path / f"s{len(made)}"
        engine = ScriptedEngine(root / "engine")
        engine.update(lambda state: state["images"].update({WORKER_TAG: WORKER_BARE}))
        repo = Supervised(
            root,
            env={"PATH": str(engine.bin), SECRET_NAME: SECRET, **(env or {})},
            access_policy=access_policy or policy(),
            create=create,
        )
        made.append(repo)
        return repo, engine

    yield make
    for repo in made:
        repo.close()


def answer(result: object) -> JsonObject:
    assert getattr(result, "returncode") == 0, getattr(result, "stderr")
    parsed: JsonObject = json.loads(getattr(result, "stdout"))
    return parsed


def dig(value: object, *keys: str) -> object:
    """The member at ``keys`` of nested JSON objects."""
    for key in keys:
        assert isinstance(value, dict), value
        value = value[key]
    return value


def flag_values(argv: list[str], flag: str) -> list[str]:
    return [value for name, value in zip(argv, argv[1:]) if name == flag]


def assert_only_podman(engine: ScriptedEngine) -> None:
    calls = engine.calls()
    assert calls and all(call[0] == "podman" for call in calls), calls
    assert not any("volume" in call for call in calls), calls
    assert SECRET not in json.dumps(calls)


def test_the_runtime_launches_the_approved_worker_with_the_adr_0025_argv(
    podman: Podman,
) -> None:
    repo, engine = podman()
    brief = repo.brief
    assert brief["runtime_images"] == {"worker": WORKER_ID, "proxy": None}
    runtime_access.validate_binding(brief)

    sent = answer(repo.send())

    execution = sent["execution"]
    assert isinstance(execution, dict)
    assert execution["kind"] == "container"
    assert execution["engine"] == "podman"
    assert execution["container_id"] == WORKER_CONTAINER
    assert repo.launches() == [], "the adapter ran on the host"
    [create] = [call for call in engine.calls() if call[1] == "create"]
    assert create[:4] == ["podman", "create", "--pull", "never"]
    assert flag_values(create, "--network") == ["none"]
    assert flag_values(create, "--cap-drop") == ["ALL"]
    assert flag_values(create, "--security-opt") == ["no-new-privileges"]
    requirements = dig(brief, "runtime_access", "requirements")
    assert isinstance(requirements, list)
    roots = {str(dig(item, "path")) for item in requirements}
    mounted = {
        value.split(",")[1].removeprefix("source=")
        for value in flag_values(create, "--mount")
    }
    assert mounted == roots
    assert SECRET_NAME in flag_values(create, "--env")
    assert flag_values(create, "--entrypoint") == [sys.executable]
    assert create[create.index(WORKER_ID) + 1] == str(repo.adapter)
    assert ["podman", "start", WORKER_CONTAINER] in engine.calls()
    assert_only_podman(engine)
    assert repo.wait_execution("running")["container_id"] == WORKER_CONTAINER


def test_status_follows_the_container_and_its_exit_removes_it_with_podman(
    podman: Podman,
) -> None:
    repo, engine = podman()
    answer(repo.send())
    repo.wait_execution("running")

    status = answer(repo.run("dispatch", "status", "--dispatch", repo.dispatch))
    [entry] = status["dispatches"]
    assert entry["execution"]["state"] == "running"
    assert "worker" not in entry

    def exit_worker(state: JsonObject) -> None:
        state["objects"][WORKER_CONTAINER].update(status="exited", exit_code=3)

    engine.update(exit_worker)
    ended = repo.wait_execution("exited")

    assert dig(ended, "exit", "exit_code") == 3
    stdout = Path(str(dig(ended, "receipt", "logs", "stdout")))
    assert stdout.read_text(encoding="utf-8") == "worker stdout\n"
    assert engine.state()["objects"] == {}
    assert any(
        call[1] == "logs" and call[-1] == WORKER_CONTAINER for call in engine.calls()
    )
    assert_only_podman(engine)


def test_an_image_rebuilt_after_approval_refuses_the_send_before_any_launch(
    podman: Podman,
) -> None:
    repo, engine = podman()
    engine.update(lambda state: state["images"].update({WORKER_TAG: "c" * 64}))

    refused = repo.send()

    assert refused.returncode != 0
    assert "changed after dispatch propose" in refused.stderr
    assert not any(call[1] in {"create", "start"} for call in engine.calls())
    assert repo.execution() is None
    assert repo.status()["state"] == "approved"


def test_a_name_missing_from_the_runtime_environment_blocks_propose_by_its_name(
    podman: Podman,
) -> None:
    repo, engine = podman(
        access_policy=policy(environment=[SECRET_NAME, "HARNESS_TEST_UNSET_NAME"]),
        create=False,
    )

    refused = repo.propose()

    assert refused.returncode != 0
    assert "no variable named: HARNESS_TEST_UNSET_NAME" in refused.stderr
    assert not any(call[1] in {"create", "start"} for call in engine.calls())


def test_a_plan_root_that_holds_the_runtime_directory_is_policy_denied_before_the_engine(
    podman: Podman,
) -> None:
    repo, engine = podman(create=False)
    config_path = repo.repo / ".harness" / "orchestration.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["access_policy"] = policy(
        filesystem=[
            {"resource": "cache", "path": str(repo.runtime.xdg), "access": "write"}
        ]
    )
    config_path.write_text(json.dumps(config), encoding="utf-8")

    refused = repo.propose()

    assert refused.returncode != 0
    assert "policy_denied" in refused.stderr
    assert engine.calls() == []


def test_the_executor_has_no_engine_boundary_of_its_own() -> None:
    source = Path(container_executor.__file__).read_text(encoding="utf-8")

    assert "run_engine" not in source
    assert "subprocess" not in source
    assert not hasattr(container_executor.DockerExecutor, "handoff")
