#!/usr/bin/env python3
"""The built-in ``docker`` runtime_access through the public coordinator commands (issue #704).

Real Git worktrees and the Lifecycle ledger: only ``container_executor.run_engine``, the lowest
container-engine command boundary, is replaced by a scripted engine that records every argv.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from harness.orchestration import (
    container_executor,
    coordinator,
    extensions,
    operational_guards,
    runtime_access,
)
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from tests.orchestration import test_coordinator as coordinator_tests

WORKER_TAG = "harness-worker:local"
PROXY_TAG = "harness-proxy:local"
WORKER_ID = "sha256:" + "a" * 64
PROXY_ID = "sha256:" + "b" * 64
REBUILT_ID = "sha256:" + "c" * 64
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
        # The adapter marks a run on the host: under the executor it must only run in the container.
        self.adapter_ran = self.fx.tmp / "adapter-ran"
        self.adapter = self.fx.tmp / "adapter.sh"
        self.adapter.write_text(
            f"#!/bin/sh\ntouch '{self.adapter_ran}'\n", encoding="utf-8"
        )
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

    def send(self, dispatch_id: str) -> JsonObject:
        sent: JsonObject = coordinator.send_dispatch(
            self.fx._args(
                dispatch=dispatch_id,
                adapter=str(self.adapter),
                adapter_arg=None,
                checkout=None,
            )
        )
        return sent

    def state(self, dispatch_id: str) -> str:
        status = coordinator.dispatch_status(
            self.fx._args(dispatch=dispatch_id, batch=None, stale_after=None)
        )
        return str(status["dispatches"][0]["state"])

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


class DockerApprovalTests(DockerFixture):
    """Propose pins the image digests; approval and send never reuse them for another image."""

    def test_propose_pins_the_digests_outside_the_plan_and_approval_binds_them(
        self,
    ) -> None:
        first = self.fx._propose(self.batch_id, "architect")
        self.engine.images[WORKER_TAG] = REBUILT_ID
        second = self.fx._propose(self.batch_id, "architect")

        self.assertEqual(first["runtime_images"], {"worker": WORKER_ID, "proxy": None})
        self.assertEqual(
            first["transition"]["runtime_images_sha256"],
            operational_guards.runtime_images_digest(first["runtime_images"]),
        )
        self.assertNotIn("runtime_images", first["runtime_access"])
        self.assertEqual(
            first["runtime_access"]["plan_digest"],
            second["runtime_access"]["plan_digest"],
        )
        self.assertNotEqual(first["transition_digest"], second["transition_digest"])
        with self.assertRaises(CoordinatorError) as refused:
            self.fx._dispatch(
                self.batch_id, "architect", digest=first["transition_digest"]
            )
        self.assertIn("approval digest does not match", refused.exception.message)
        self.assertIn("pinned runtime image", refused.exception.remedy)

        brief = self.fx._dispatch(
            self.batch_id, "architect", digest=second["transition_digest"]
        )["brief"]

        self.assertEqual(brief["runtime_images"], {"worker": REBUILT_ID, "proxy": None})
        self.assertEqual(brief["transition"], second["transition"])
        self.assert_no_container_was_created()

    def test_a_proxy_image_is_pinned_when_the_container_names_one(self) -> None:
        self.configure(container={"worker_image": WORKER_TAG, "proxy_image": PROXY_TAG})

        brief = self.fx._dispatch(self.batch_id, "architect")["brief"]

        self.assertEqual(
            brief["runtime_images"], {"worker": WORKER_ID, "proxy": PROXY_ID}
        )

    def test_propose_refuses_an_environment_it_cannot_pin(self) -> None:
        self.engine.images = {}

        with self.assertRaises(runtime_access.AccessError) as refused:
            self.fx._propose(self.batch_id, "architect")

        self.assertIn(f"image {WORKER_TAG} is not present", refused.exception.message)
        self.assertIn(f"build or pull {WORKER_TAG}", refused.exception.remedy)

    def test_send_refuses_an_image_changed_or_missing_after_approval(self) -> None:
        brief = self.fx._dispatch(self.batch_id, "architect")["brief"]
        self.engine.images[WORKER_TAG] = REBUILT_ID

        with self.assertRaises(runtime_access.AccessError) as changed:
            self.send(brief["dispatch_id"])

        self.assertEqual(
            changed.exception.message,
            f"image {WORKER_TAG} changed after dispatch propose: pinned {WORKER_ID}, "
            f"now {REBUILT_ID}",
        )
        self.assertEqual(
            changed.exception.remedy, container_executor.CHANGED_IMAGE_REMEDY
        )
        self.assertIn("repeat dispatch propose", changed.exception.remedy)
        self.assertEqual(self.state(brief["dispatch_id"]), "approved")

        del self.engine.images[WORKER_TAG]
        with self.assertRaises(runtime_access.AccessError) as missing:
            self.send(brief["dispatch_id"])

        self.assertIn(f"image {WORKER_TAG} is not present", missing.exception.message)
        self.assertIn("repeat dispatch preflight and propose", missing.exception.remedy)
        self.assertEqual(self.state(brief["dispatch_id"]), "approved")
        self.assert_no_container_was_created()

    def test_only_a_docker_send_brief_carries_exactly_its_pinned_images(self) -> None:
        brief = self.fx._dispatch(self.batch_id, "architect")["brief"]
        runtime_access.validate_binding(brief)
        unpinned = {
            key: value for key, value in brief.items() if key != "runtime_images"
        }
        tampered: list[JsonObject] = [
            unpinned,
            {**brief, "runtime_images": {"worker": WORKER_ID, "proxy": PROXY_ID}},
            {**brief, "runtime_images": {"worker": WORKER_TAG, "proxy": None}},
            {**brief, "runtime_images": {"worker": WORKER_ID}},
            {**brief, "purpose": "publish"},
        ]
        for invalid in tampered:
            with (
                self.subTest(invalid=invalid.get("runtime_images")),
                self.assertRaises(runtime_access.AccessError) as refused,
            ):
                runtime_access.validate_binding(invalid)
            self.assertIn("not pinned exactly", refused.exception.message)
        legacy = {
            key: value
            for key, value in unpinned.items()
            if key not in ("runtime_access", "transition")
        }
        runtime_access.validate_binding(legacy)
        with self.assertRaises(runtime_access.AccessError):
            runtime_access.validate_binding(
                {**legacy, "runtime_images": brief["runtime_images"]}
            )


class DockerHandoffTests(DockerFixture):
    """Send starts the worker detached in its own container and records the linked receipt."""

    def expected_create(self, brief: JsonObject, brief_path: str) -> list[str]:
        plan = brief["runtime_access"]
        root = {item["resource"]: item["path"] for item in plan["requirements"]}
        self.assertEqual(
            {
                item["access"]
                for item in plan["requirements"]
                if item["resource"] != "artifacts"
            },
            {"read"},
        )
        dispatch_id = brief["dispatch_id"]
        return [
            "docker",
            "create",
            "--pull",
            "never",
            "--name",
            f"harness-{dispatch_id}",
            "--network",
            "none",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/harness-home:rw,exec,nosuid,nodev,mode=1777",
            "--env",
            "HOME=/harness-home",
            "--env",
            "GIT_CONFIG_COUNT=1",
            "--env",
            "GIT_CONFIG_KEY_0=safe.directory",
            "--env",
            "GIT_CONFIG_VALUE_0=*",
            "--env",
            "HARNESS_EXECUTOR=docker",
            "--label",
            f"harness.dispatch={dispatch_id}",
            "--label",
            f"harness.repository={root['git_common']}",
            "--label",
            f"harness.plan_digest={plan['plan_digest']}",
            "--workdir",
            root["checkout"],
            # Parents first: the worktree, Git metadata, storage, then its nested scratch.
            "--mount",
            f"type=bind,source={root['checkout']},target={root['checkout']},readonly",
            "--mount",
            f"type=bind,source={root['git_common']},target={root['git_common']},readonly",
            "--mount",
            f"type=bind,source={root['shared_storage']},"
            f"target={root['shared_storage']},readonly",
            "--mount",
            f"type=bind,source={root['artifacts']},target={root['artifacts']}",
            "--entrypoint",
            str(self.adapter.resolve()),
            WORKER_ID,
            "dispatch",
            "--repo",
            str(self.repo.resolve()),
            "--brief",
            brief_path,
        ]

    def test_send_starts_the_worker_detached_with_exactly_the_plan_mounts(self) -> None:
        brief = self.fx._dispatch(self.batch_id, "architect")["brief"]
        self.engine.reset()

        sent = self.send(brief["dispatch_id"])

        create = self.expected_create(brief, sent["brief"])
        self.assertEqual(
            self.engine.calls,
            [
                ["docker", "info", "--format", "{{json .}}"],
                ["docker", "image", "inspect", "--format", "{{.Id}}", WORKER_TAG],
                create,
                ["docker", "start", CONTAINER_ID],
            ],
        )
        mounted = {
            value.split(",")[1].removeprefix("source=")
            for flag, value in zip(create, create[1:])
            if flag == "--mount"
        }
        self.assertEqual(
            mounted, {item["path"] for item in brief["runtime_access"]["requirements"]}
        )
        self.assertFalse(self.adapter_ran.exists())
        self.assertFalse(set(self.engine.kinds()) & FORBIDDEN)
        status = coordinator.dispatch_status(
            self.fx._args(dispatch=brief["dispatch_id"], batch=None, stale_after=None)
        )["dispatches"][0]
        self.assertEqual(status["state"], "dispatched")
        proof = status["runtime_access"]
        self.assertEqual(proof, json.loads(json.dumps(sent["runtime_access"])))
        self.assertEqual(
            (proof["status"], proof["dispatch_id"], proof["plan_digest"]),
            ("applied", brief["dispatch_id"], brief["runtime_access"]["plan_digest"]),
        )
        evidence = proof["evidence"]
        self.assertEqual(
            {
                key: evidence[key]
                for key in (
                    "launch_id",
                    "dispatch_id",
                    "applied",
                    "handed_off",
                    "engine",
                    "engine_mode",
                    "worker_image_id",
                    "proxy_image_id",
                    "mechanism",
                    "environment_id",
                )
            },
            {
                "launch_id": CONTAINER_ID,
                "dispatch_id": brief["dispatch_id"],
                "applied": True,
                "handed_off": True,
                "engine": "docker",
                "engine_mode": "rootful",
                "worker_image_id": WORKER_ID,
                "proxy_image_id": None,
                "mechanism": "native-apply",
                "environment_id": f"docker:{DAEMON_ID}",
            },
        )

    def test_a_failed_launch_removes_the_container_and_keeps_the_dispatch_approved(
        self,
    ) -> None:
        brief = self.fx._dispatch(self.batch_id, "architect")["brief"]
        name = f"harness-{brief['dispatch_id']}"
        cases: tuple[tuple[str, BaseException | str, str], ...] = (
            ("create", "invalid mount config", "docker create failed: invalid mount"),
            (
                "create",
                OSError("docker vanished"),
                "docker create failed: docker vanished",
            ),
            (
                "create",
                subprocess.TimeoutExpired(["docker"], 30),
                "docker create failed",
            ),
            ("start", "exec format error", "docker start failed: exec format error"),
            ("start", OSError("daemon gone"), "docker start failed: daemon gone"),
        )
        for kind, failure, message in cases:
            with self.subTest(kind=kind, failure=failure):
                self.engine.reset()
                self.engine.failures = {kind: failure, "rm": "no such container"}

                with self.assertRaises(runtime_access.AccessError) as refused:
                    self.send(brief["dispatch_id"])

                self.assertIn(message, refused.exception.message)
                self.assertIn(
                    f"container {name} was removed", refused.exception.message
                )
                self.assertIn("send the dispatch again", refused.exception.remedy)
                self.assertEqual(
                    self.engine.calls[-1], ["docker", "rm", "--force", name]
                )
                self.assertEqual(self.state(brief["dispatch_id"]), "approved")
        self.engine.reset()

        self.send(brief["dispatch_id"])

        self.assertEqual(self.state(brief["dispatch_id"]), "dispatched")

    def test_a_create_without_a_container_id_is_a_failed_launch(self) -> None:
        brief = self.fx._dispatch(self.batch_id, "architect")["brief"]
        engine = self.engine

        def unnamed(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
            if list(argv[:2]) == ["docker", "create"]:
                engine.calls.append(list(argv))
                return _done(list(argv), "WARNING: something\n")
            return engine(argv)

        with (
            mock.patch.object(container_executor, "run_engine", unnamed),
            self.assertRaises(runtime_access.AccessError) as refused,
        ):
            self.send(brief["dispatch_id"])

        self.assertIn(
            "docker create returned no container id", refused.exception.message
        )
        self.assertEqual(engine.kinds()[-2:], ["create", "rm"])
        self.assertEqual(self.state(brief["dispatch_id"]), "approved")

    def test_the_built_in_executor_offers_the_whole_provider_contract(self) -> None:
        provider = extensions.runtime_access("docker")

        for method in ("observe", "apply", "handoff"):
            self.assertTrue(callable(getattr(provider, method, None)), method)


class HandoffLaunchIdentityTests(unittest.TestCase):
    """Only a handoff-time identity may become another launch id in the receipt."""

    def receipt_check(self, observed: str, received: str) -> None:
        directory = self.enterContext(
            tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        )
        plan = _plan(
            Path(directory) / "repo",
            {
                "access_policy": {"defaults": {"mode": "sandbox"}},
                "extensions": {"runtime_access": "test-launch-identity"},
            },
        )

        class Native:
            def observe(
                self, plan: JsonObject, transport: str
            ) -> extensions.RuntimeAccessObservation:
                return extensions.RuntimeAccessObservation(
                    plan_digest=plan["plan_digest"],
                    transport=transport,
                    supported_modes=("sandbox",),
                    effective_mode="sandbox",
                    mechanism="native-apply",
                    environment_id="worker-host",
                    launch_id=observed,
                    source="native-runtime",
                    observed_at=datetime.now(UTC).isoformat(),
                    hosts=(),
                    filesystem=tuple(
                        (item["path"], item["access"]) for item in plan["requirements"]
                    ),
                )

            def apply(
                self,
                brief: JsonObject,
                observation: extensions.RuntimeAccessObservation,
            ) -> extensions.RuntimeAccessObservation:
                return replace(observation, applied=True)

            def handoff(
                self,
                brief: JsonObject,
                observation: extensions.RuntimeAccessObservation,
                command: tuple[str, ...] | None,
            ) -> extensions.RuntimeAccessObservation:
                return replace(
                    observation,
                    dispatch_id=brief["dispatch_id"],
                    handed_off=True,
                    launch_id=received,
                )

        extensions.register("runtime_access", "test-launch-identity", Native())
        self.addCleanup(extensions.unregister, "runtime_access", "test-launch-identity")
        runtime_access.apply_plan(
            {"dispatch_id": "dispatch-1", "runtime_access": plan},
            "external",
            handoff=True,
            command=("adapter",),
        )

    def test_a_handoff_time_identity_becomes_the_real_launch_id(self) -> None:
        self.receipt_check(extensions.LAUNCH_AT_HANDOFF, CONTAINER_ID)
        self.receipt_check("reserved-launch", "reserved-launch")

    def test_any_other_change_of_launch_identity_is_refused(self) -> None:
        for observed, received in (
            ("reserved-launch", CONTAINER_ID),
            (extensions.LAUNCH_AT_HANDOFF, extensions.LAUNCH_AT_HANDOFF),
        ):
            with (
                self.subTest(observed=observed, received=received),
                self.assertRaises(runtime_access.AccessError) as refused,
            ):
                self.receipt_check(observed, received)
            self.assertIn(
                "does not bind the approved dispatch", refused.exception.message
            )


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
