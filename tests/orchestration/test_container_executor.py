#!/usr/bin/env python3
"""The built-in ``docker`` runtime_access through the public coordinator commands (issue #704).

Real Git worktrees and the Lifecycle ledger: only ``container_executor.run_engine``, the lowest
container-engine command boundary, is replaced by a scripted engine that records every argv.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

from harness.orchestration import container_executor, coordinator, runtime_access
from harness.orchestration.core.utils import JsonObject
from tests.orchestration import test_coordinator as coordinator_tests

WORKER_TAG = "harness-worker:local"
PROXY_TAG = "harness-proxy:local"
WORKER_ID = "sha256:" + "a" * 64
PROXY_ID = "sha256:" + "b" * 64
DAEMON_ID = "5c5e2d3f-docker-daemon"
CONTAINER_ID = "0123456789abcdef" * 4
ROOTFUL_INFO: JsonObject = {
    "ID": DAEMON_ID,
    "ServerVersion": "29.9.0",
    "SecurityOptions": ["name=apparmor", "name=seccomp,profile=builtin"],
}
# Engine commands the executor must never run: it neither builds, pulls nor runs attached.
FORBIDDEN = {"pull", "build", "run", "buildx", "load", "import"}


def _done(argv: list[str], stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, 0, stdout, "")


class FakeEngine:
    """The Docker CLI at its command boundary: answers by argv and records every call."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.calls: list[list[str]] = []
        self.info: object = ROOTFUL_INFO
        self.images: dict[str, str] = {WORKER_TAG: WORKER_ID, PROXY_TAG: PROXY_ID}
        # A command kind (info, inspect, create, start, rm) that raises or exits non-zero.
        self.failures: dict[str, BaseException | str] = {}

    def kinds(self) -> list[str]:
        return [self._kind(call) for call in self.calls]

    @staticmethod
    def _kind(argv: list[str]) -> str:
        return argv[2] if argv[1] == "image" else argv[1]

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = list(argv)
        self.calls.append(command)
        assert command[0] == "docker", command
        kind = self._kind(command)
        failure = self.failures.get(kind)
        if isinstance(failure, BaseException):
            raise failure
        if failure is not None:
            return subprocess.CompletedProcess(command, 1, "", failure)
        if kind == "info":
            return _done(command, json.dumps(self.info) + "\n")
        if kind == "inspect":
            image = self.images.get(command[-1])
            if image is None:
                return subprocess.CompletedProcess(
                    command, 1, "", f"Error: No such image: {command[-1]}"
                )
            return _done(command, image + "\n")
        if kind == "create":
            return _done(command, CONTAINER_ID + "\n")
        if kind in ("start", "rm"):
            return _done(command, command[-1] + "\n")
        raise AssertionError(f"unexpected engine command {command}")


class DockerFixture(unittest.TestCase):
    """A batch whose roles go through an external adapter under the docker executor."""

    def setUp(self) -> None:
        self.fx = coordinator_tests.CoordinatorRetryRoutingTests()
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.repo = self.fx.repo
        self.engine = FakeEngine()
        patcher = mock.patch.object(container_executor, "run_engine", self.engine)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.adapter = self.fx.tmp / "adapter.sh"
        self.adapter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.adapter.chmod(0o755)
        self.configure()
        self.batch_id = self.fx._create_batch()["batch_id"]

    def configure(
        self,
        *,
        transport: str = "external",
        container: JsonObject | None = None,
        **defaults: object,
    ) -> None:
        profile = {
            "capabilities": [
                "architecture-analysis",
                "backend-development",
                "code-review",
            ],
            "fallback": [],
            "known_limitations": [],
        }
        runtimes = {
            "claude": {"profiles": ["worker"], "model": "sonnet", "effort": "high"}
        }
        config = {
            "provider_profiles": {"worker": profile},
            "assignment_plans": {
                role: {"transport": transport, "runtimes": runtimes}
                for role in ("architect", "developer", "code-review")
            },
            "concurrency_budget": 1,
            "verification_commands": ["true"],
            "access_policy": {
                "defaults": {
                    "mode": "sandbox",
                    "container": container or {"worker_image": WORKER_TAG},
                    **defaults,
                }
            },
            "extensions": {"runtime_access": "docker"},
        }
        (self.repo / ".harness/orchestration.json").write_text(
            json.dumps(config), encoding="utf-8"
        )

    def preflight(self) -> JsonObject:
        prepared = coordinator.preflight_dispatch(
            self.fx._args(
                batch=self.batch_id,
                role="architect",
                purpose="work",
                runtime="claude",
                candidate_commit=None,
            )
        )
        verification: JsonObject = prepared["decision_packet"]["runtime_access"][
            "verification"
        ]
        return verification

    def assert_no_container_was_created(self) -> None:
        kinds = set(self.engine.kinds())
        self.assertFalse(kinds & {"create", "start", *FORBIDDEN}, self.engine.calls)


class DockerPreflightTests(DockerFixture):
    def test_preflight_verifies_a_rootful_engine_and_the_present_images(self) -> None:
        self.configure(container={"worker_image": WORKER_TAG, "proxy_image": PROXY_TAG})

        verification = self.preflight()

        self.assertEqual(verification["status"], "verified", verification)
        evidence = verification["evidence"]
        self.assertEqual(
            {
                key: evidence[key]
                for key in (
                    "engine",
                    "engine_mode",
                    "worker_image",
                    "worker_image_id",
                    "proxy_image",
                    "proxy_image_id",
                    "environment_id",
                    "mechanism",
                    "transport",
                )
            },
            {
                "engine": "docker",
                "engine_mode": "rootful",
                "worker_image": WORKER_TAG,
                "worker_image_id": WORKER_ID,
                "proxy_image": PROXY_TAG,
                "proxy_image_id": PROXY_ID,
                "environment_id": f"docker:{DAEMON_ID}",
                "mechanism": "native-apply",
                "transport": "external",
            },
        )
        self.assertEqual(
            self.engine.calls,
            [
                ["docker", "info", "--format", "{{json .}}"],
                ["docker", "image", "inspect", "--format", "{{.Id}}", WORKER_TAG],
                ["docker", "image", "inspect", "--format", "{{.Id}}", PROXY_TAG],
            ],
        )

    def test_every_unsupported_environment_is_an_explicit_blocker_before_any_worker(
        self,
    ) -> None:
        rootless = {
            **ROOTFUL_INFO,
            "SecurityOptions": ["name=seccomp,profile=builtin", "name=rootless"],
        }
        cases: tuple[tuple[str, JsonObject, JsonObject, str, str, str], ...] = (
            (
                "in-process transport",
                {"transport": "in-process"},
                {},
                "only an external dispatch, not in-process",
                "transport to external",
                "rootful",
            ),
            (
                "podman engine",
                {"container": {"engine": "podman", "worker_image": WORKER_TAG}},
                {},
                "container engine podman is not supported",
                "engine to docker",
                "not-probed",
            ),
            (
                "network hosts",
                {"network": {"hosts": ["api.example.com"]}},
                {},
                "network hosts api.example.com need an egress proxy",
                "network.hosts to []",
                "rootful",
            ),
            (
                "unsandboxed mode",
                {"mode": "unsandboxed"},
                {},
                "mode unsandboxed cannot run in a container",
                "mode to sandbox or inherit",
                "rootful",
            ),
            (
                "engine cannot start",
                {},
                {"failures": {"info": FileNotFoundError(2, "No such file", "docker")}},
                "the docker engine is unavailable",
                "start the rootful Docker daemon",
                "unavailable",
            ),
            (
                "engine refuses",
                {},
                {"failures": {"info": "Cannot connect to the Docker daemon"}},
                "Cannot connect to the Docker daemon",
                "start the rootful Docker daemon",
                "unavailable",
            ),
            (
                "engine times out",
                {},
                {"failures": {"info": subprocess.TimeoutExpired(["docker"], 30)}},
                "the docker engine is unavailable",
                "start the rootful Docker daemon",
                "unavailable",
            ),
            (
                "rootless docker",
                {},
                {"info": rootless},
                "rootless Docker is not supported",
                "rootful Docker daemon",
                "rootless",
            ),
            (
                "docker-compatible shim",
                {},
                {"info": {"host": {"security": {"rootless": True}}}},
                "mode cannot be determined",
                "Podman and docker-compatible shims are not supported",
                "unknown",
            ),
            (
                "missing worker image",
                {},
                {"images": {}},
                f"image {WORKER_TAG} is not present",
                f"build or pull {WORKER_TAG} yourself",
                "rootful",
            ),
            (
                "missing proxy image",
                {"container": {"worker_image": WORKER_TAG, "proxy_image": "gone:1"}},
                {},
                "image gone:1 is not present",
                "build or pull gone:1 yourself",
                "rootful",
            ),
        )
        for name, config, engine, reason, remedy, mode in cases:
            with self.subTest(name):
                self.configure(**config)
                self.engine.reset()
                for attribute, value in engine.items():
                    setattr(self.engine, attribute, value)

                verification = self.preflight()

                self.assertEqual(verification["status"], "unverified")
                self.assertIn(reason, verification["reason"])
                self.assertIn(remedy, verification["remedy"])
                facts = verification["environment"]
                self.assertEqual(facts["engine_mode"], mode)
                self.assertTrue(facts["blockers"])
                self.assertNotIn("evidence", verification)
                self.assert_no_container_was_created()
                if mode in ("not-probed", "unavailable"):
                    self.assertNotIn("inspect", self.engine.kinds())

    def test_preflight_reports_every_blocker_at_once(self) -> None:
        self.configure(transport="in-process", network={"hosts": ["api.example.com"]})
        self.engine.images = {}

        verification = self.preflight()

        reasons = [item["reason"] for item in verification["environment"]["blockers"]]
        self.assertEqual(len(reasons), 3, reasons)
        self.assertEqual(verification["reason"], "; ".join(reasons))
        self.assertIsNone(verification["environment"]["worker_image_id"])


def _plan(repo: Path, config: JsonObject) -> JsonObject:
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return runtime_access.resolve_plan(repo, repo, config, "developer", "write")


class DockerPlanSupportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = FakeEngine()
        patcher = mock.patch.object(container_executor, "run_engine", self.engine)
        patcher.start()
        self.addCleanup(patcher.stop)
        directory = self.enterContext(
            tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        )
        self.tmp = Path(directory)

    def test_a_plan_without_a_container_is_blocked_without_probing_the_engine(
        self,
    ) -> None:
        plan = _plan(
            self.tmp / "repo",
            {
                "access_policy": {"defaults": {"mode": "sandbox"}},
                "extensions": {"runtime_access": "docker"},
            },
        )

        verification, observation = runtime_access.verify_plan(plan, "external")

        self.assertIsNone(observation)
        self.assertEqual(
            verification["reason"], "the access plan has no container component"
        )
        self.assertIn("access_policy.defaults.container", verification["remedy"])
        self.assertEqual(self.engine.calls, [])

    def test_a_root_that_could_inject_mount_options_is_blocked(self) -> None:
        plan = _plan(
            self.tmp / "re,po",
            {
                "access_policy": {
                    "defaults": {
                        "mode": "sandbox",
                        "container": {"worker_image": WORKER_TAG},
                    }
                },
                "extensions": {"runtime_access": "docker"},
            },
        )

        verification, _ = runtime_access.verify_plan(plan, "external")

        self.assertIn("cannot be mounted safely", verification["reason"])
        self.assertIn("without commas", verification["remedy"])


if __name__ == "__main__":
    unittest.main()
