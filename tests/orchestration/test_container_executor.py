#!/usr/bin/env python3
"""The built-in ``docker`` runtime_access through the public coordinator commands (issue #704).

Real Git worktrees and the Lifecycle ledger: only ``container_executor.run_engine``, the lowest
container-engine command boundary, is replaced by a scripted engine that records every argv.
"""

from __future__ import annotations

import itertools
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
PROXY_CONTAINER_ID = "89abcdef01234567" * 4
NETWORK_ID = "fedcba9876543210" * 4
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
        # A command kind (info, inspect, create, start, rm, network-create, network-connect,
        # network-rm), or ``kind:object``, that raises or exits non-zero.
        self.failures: dict[str, BaseException | str] = {}

    def kinds(self) -> list[str]:
        return [self._kind(call) for call in self.calls]

    @staticmethod
    def _kind(argv: list[str]) -> str:
        """The command kind: ``inspect`` for images, ``network-<verb>`` for networks."""
        if argv[1] == "image":
            return argv[2]
        if argv[1] == "network":
            return f"network-{argv[2]}"
        return argv[1]

    @staticmethod
    def _target(argv: list[str], kind: str) -> str:
        """The object a command names: its ``--name`` for a create, otherwise its last argument."""
        return argv[argv.index("--name") + 1] if kind == "create" else argv[-1]

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = list(argv)
        self.calls.append(command)
        assert command[0] == "docker", command
        kind = self._kind(command)
        # ``kind:target`` fails one object only, for example the proxy but not the worker.
        failure = self.failures.get(f"{kind}:{self._target(command, kind)}")
        failure = self.failures.get(kind) if failure is None else failure
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
            proxy = self._target(command, kind).endswith("-proxy")
            return _done(
                command, (PROXY_CONTAINER_ID if proxy else CONTAINER_ID) + "\n"
            )
        if kind == "network-create":
            return _done(command, NETWORK_ID + "\n")
        if kind in ("start", "rm", "network-connect", "network-rm"):
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

    def expected_create(
        self,
        brief: JsonObject,
        brief_path: str,
        network: str = "none",
        proxy_env: Sequence[str] = (),
    ) -> list[str]:
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
            network,
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
            *[part for entry in proxy_env for part in ("--env", entry)],
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
        removals: tuple[tuple[BaseException | str | None, str], ...] = (
            (None, f"container {name} was removed"),
            ("conflict", f"removing container {name} failed: conflict"),
            (OSError("rm gone"), f"removing container {name} failed: rm gone"),
        )
        for (kind, failure, message), (removal, cleanup) in itertools.product(
            cases, removals
        ):
            with self.subTest(kind=kind, failure=failure, removal=removal):
                self.engine.reset()
                self.engine.failures = {kind: failure}
                if removal is not None:
                    self.engine.failures["rm"] = removal

                with self.assertRaises(runtime_access.AccessError) as refused:
                    self.send(brief["dispatch_id"])

                self.assertIn(message, refused.exception.message)
                self.assertIn(cleanup, refused.exception.message)
                if removal is not None:
                    self.assertNotIn("was removed", refused.exception.message)
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


PROXY_URL_ENV = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
)
BYPASS_ENV = ("NO_PROXY", "no_proxy")
HOSTS = ["api.example.com", "files.example.org"]


class DockerEgressTests(DockerFixture):
    """A non-empty network.hosts runs the worker behind a per-dispatch egress-proxy sidecar (#706)."""

    def setUp(self) -> None:
        super().setUp()
        self.configure(
            container={"worker_image": WORKER_TAG, "proxy_image": PROXY_TAG},
            network={"hosts": HOSTS},
        )

    def approved(self) -> JsonObject:
        brief: JsonObject = self.fx._dispatch(self.batch_id, "architect")["brief"]
        self.engine.reset()
        return brief

    def labels(self, brief: JsonObject) -> list[str]:
        plan = brief["runtime_access"]
        root = {item["resource"]: item["path"] for item in plan["requirements"]}
        return [
            part
            for entry in (
                f"harness.dispatch={brief['dispatch_id']}",
                f"harness.repository={root['git_common']}",
                f"harness.plan_digest={plan['plan_digest']}",
            )
            for part in ("--label", entry)
        ]

    def topology(self, brief: JsonObject, sent: JsonObject) -> list[list[str]]:
        """Every engine call a send makes, in order, with the exact argv."""
        dispatch_id = brief["dispatch_id"]
        network = f"harness-{dispatch_id}-net"
        proxy = f"harness-{dispatch_id}-proxy"
        url = f"http://{proxy}:3128"
        proxy_env = [f"{name}={url}" for name in PROXY_URL_ENV] + [
            f"{name}=localhost,127.0.0.1,::1" for name in BYPASS_ENV
        ]
        return [
            ["docker", "info", "--format", "{{json .}}"],
            ["docker", "image", "inspect", "--format", "{{.Id}}", WORKER_TAG],
            ["docker", "image", "inspect", "--format", "{{.Id}}", PROXY_TAG],
            ["docker", "network", "create", "--internal", *self.labels(brief), network],
            [
                "docker",
                "create",
                "--pull",
                "never",
                "--name",
                proxy,
                "--network",
                network,
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--read-only",
                "--env",
                f"HARNESS_EGRESS_ALLOWED_HOSTS={','.join(HOSTS)}",
                *self.labels(brief),
                PROXY_ID,
            ],
            ["docker", "network", "connect", "bridge", PROXY_CONTAINER_ID],
            ["docker", "start", PROXY_CONTAINER_ID],
            self.expected_create(brief, sent["brief"], network, proxy_env),
            ["docker", "start", CONTAINER_ID],
        ]

    def test_preflight_verifies_the_hosts_and_the_proxy_image(self) -> None:
        verification = self.preflight()

        self.assertEqual(verification["status"], "verified", verification)
        evidence = verification["evidence"]
        self.assertEqual(list(evidence["hosts"]), HOSTS)
        self.assertEqual(
            (evidence["proxy_image"], evidence["proxy_image_id"]), (PROXY_TAG, PROXY_ID)
        )

    def test_hosts_without_a_proxy_image_are_blocked_with_the_remedy(self) -> None:
        self.configure(network={"hosts": HOSTS})

        verification = self.preflight()

        self.assertEqual(verification["status"], "unverified")
        self.assertIn("need an egress proxy", verification["reason"])
        self.assertIn("container.proxy_image", verification["remedy"])
        self.assertIn("Dockerfile.proxy", verification["remedy"])
        self.assert_no_container_was_created()

    def test_the_proxy_environment_names_are_reserved_only_with_hosts(self) -> None:
        os_names = {name: "x" for name in (*PROXY_URL_ENV, *BYPASS_ENV)}
        with mock.patch.dict(os.environ, os_names):
            for name in (*PROXY_URL_ENV, *BYPASS_ENV):
                with self.subTest(name=name, hosts=True):
                    self.configure(
                        container={
                            "worker_image": WORKER_TAG,
                            "proxy_image": PROXY_TAG,
                        },
                        network={"hosts": HOSTS},
                        environment=[name],
                    )

                    verification = self.preflight()

                    self.assertEqual(verification["status"], "unverified")
                    self.assertIn(name, verification["reason"])
                    self.assertIn("environment", verification["remedy"])
                with self.subTest(name=name, hosts=False):
                    self.configure(
                        container={
                            "worker_image": WORKER_TAG,
                            "proxy_image": PROXY_TAG,
                        },
                        environment=[name],
                    )

                    self.assertEqual(self.preflight()["status"], "verified")

    def test_send_builds_the_internal_network_the_sidecar_and_the_worker_in_order(
        self,
    ) -> None:
        brief = self.approved()

        sent = self.send(brief["dispatch_id"])

        self.assertEqual(self.engine.calls, self.topology(brief, sent))
        self.assertFalse(set(self.engine.kinds()) & FORBIDDEN)
        self.assertFalse(self.adapter_ran.exists())
        status = coordinator.dispatch_status(
            self.fx._args(dispatch=brief["dispatch_id"], batch=None, stale_after=None)
        )["dispatches"][0]
        self.assertEqual(status["state"], "dispatched")
        evidence = status["runtime_access"]["evidence"]
        self.assertEqual(evidence["hosts"], HOSTS)
        self.assertEqual(evidence["launch_id"], CONTAINER_ID)
        self.assertEqual(evidence["proxy_image_id"], PROXY_ID)

    def test_the_worker_is_on_the_internal_network_only_and_the_sidecar_uses_the_digest(
        self,
    ) -> None:
        brief = self.approved()

        self.send(brief["dispatch_id"])

        worker = next(
            call
            for call in self.engine.calls
            if call[1] == "create" and f"harness-{brief['dispatch_id']}" in call
        )
        proxy = next(call for call in self.engine.calls if call[-1] == PROXY_ID)
        self.assertEqual(worker.count("--network"), 1)
        self.assertEqual(
            worker[worker.index("--network") + 1], f"harness-{brief['dispatch_id']}-net"
        )
        self.assertNotIn("--publish", worker + proxy)
        self.assertNotIn(PROXY_TAG, proxy)
        self.assertNotIn(WORKER_TAG, worker)
        self.assertEqual(
            proxy[proxy.index("--network") + 1], worker[worker.index("--network") + 1]
        )

    def test_an_empty_host_list_keeps_no_network_and_creates_no_sidecar(self) -> None:
        self.configure(container={"worker_image": WORKER_TAG, "proxy_image": PROXY_TAG})
        brief = self.approved()

        sent = self.send(brief["dispatch_id"])

        self.assertEqual(
            self.engine.calls,
            [
                ["docker", "info", "--format", "{{json .}}"],
                ["docker", "image", "inspect", "--format", "{{.Id}}", WORKER_TAG],
                ["docker", "image", "inspect", "--format", "{{.Id}}", PROXY_TAG],
                self.expected_create(brief, sent["brief"]),
                ["docker", "start", CONTAINER_ID],
            ],
        )
        self.assertFalse(any(call[1] == "network" for call in self.engine.calls))

    def test_a_proxy_image_rebuilt_or_removed_after_approval_blocks_the_send(
        self,
    ) -> None:
        brief = self.approved()
        self.engine.images[PROXY_TAG] = REBUILT_ID

        with self.assertRaises(runtime_access.AccessError) as changed:
            self.send(brief["dispatch_id"])

        self.assertEqual(
            changed.exception.message,
            f"image {PROXY_TAG} changed after dispatch propose: pinned {PROXY_ID}, "
            f"now {REBUILT_ID}",
        )
        self.assertEqual(
            changed.exception.remedy, container_executor.CHANGED_IMAGE_REMEDY
        )
        del self.engine.images[PROXY_TAG]
        with self.assertRaises(runtime_access.AccessError) as missing:
            self.send(brief["dispatch_id"])

        self.assertIn(f"image {PROXY_TAG} is not present", missing.exception.message)
        self.assertIn("repeat dispatch preflight and propose", missing.exception.remedy)
        self.assertEqual(self.state(brief["dispatch_id"]), "approved")
        self.assert_no_network_or_container_was_created()

    def assert_no_network_or_container_was_created(self) -> None:
        self.assertFalse(
            set(self.engine.kinds()) & {"create", "start", "network-create"},
            self.engine.calls,
        )

    def test_a_failure_at_any_stage_removes_every_attempted_object_newest_first(
        self,
    ) -> None:
        brief = self.approved()
        dispatch_id = brief["dispatch_id"]
        worker = f"harness-{dispatch_id}"
        proxy = f"{worker}-proxy"
        network = f"{worker}-net"
        remove = {
            "worker": ["docker", "rm", "--force", worker],
            "proxy": ["docker", "rm", "--force", proxy],
            "network": ["docker", "network", "rm", network],
        }
        stages: tuple[tuple[str, str, list[str]], ...] = (
            ("network-create", "docker network create failed", ["network"]),
            (
                f"create:{proxy}",
                "docker create (egress proxy) failed",
                ["proxy", "network"],
            ),
            ("network-connect", "docker network connect failed", ["proxy", "network"]),
            (
                f"start:{PROXY_CONTAINER_ID}",
                "docker start (egress proxy) failed",
                ["proxy", "network"],
            ),
            (
                f"create:{worker}",
                "docker create failed",
                ["worker", "proxy", "network"],
            ),
            (
                f"start:{CONTAINER_ID}",
                "docker start failed",
                ["worker", "proxy", "network"],
            ),
        )
        for key, message, removed in stages:
            with self.subTest(stage=key):
                self.engine.reset()
                self.engine.failures = {key: "engine said no"}

                with self.assertRaises(runtime_access.AccessError) as refused:
                    self.send(dispatch_id)

                self.assertIn(
                    message.removesuffix(" failed"), refused.exception.message
                )
                self.assertIn("engine said no", refused.exception.message)
                self.assertEqual(
                    self.engine.calls[-len(removed) :],
                    [remove[item] for item in removed],
                )
                for item in removed:
                    kind, name = (
                        ("network", network)
                        if item == "network"
                        else ("container", proxy if item == "proxy" else worker)
                    )
                    self.assertIn(
                        f"{kind} {name} was removed", refused.exception.message
                    )
                self.assertEqual(self.state(dispatch_id), "approved")
        self.engine.reset()

        self.send(dispatch_id)

        self.assertEqual(self.state(dispatch_id), "dispatched")

    def test_a_failed_removal_is_named_and_does_not_stop_the_rest(self) -> None:
        brief = self.approved()
        dispatch_id = brief["dispatch_id"]
        worker = f"harness-{dispatch_id}"
        self.engine.failures = {
            f"start:{CONTAINER_ID}": "boom",
            f"rm:{worker}-proxy": "proxy is busy",
            "network-rm": OSError("daemon gone"),
        }

        with self.assertRaises(runtime_access.AccessError) as refused:
            self.send(dispatch_id)

        message = refused.exception.message
        self.assertIn(f"container {worker} was removed", message)
        self.assertIn(
            f"removing container {worker}-proxy failed: proxy is busy", message
        )
        self.assertIn(f"removing network {worker}-net failed: daemon gone", message)
        self.assertEqual(
            [call[1] for call in self.engine.calls[-3:]], ["rm", "rm", "network"]
        )
        self.assertEqual(self.state(dispatch_id), "approved")


class DockerEnvironmentTests(DockerFixture):
    """Variables reach the worker by name only; their values never leave the host (issue #705)."""

    SECRET = "synthetic-secret-value-5d1e"
    OTHER_SECRET = "synthetic-proxy-value-9b2c"

    def setUp(self) -> None:
        environ = mock.patch.dict(
            os.environ, {"LLM_API_KEY": self.SECRET, "HTTPS_PROXY": self.OTHER_SECRET}
        )
        environ.start()
        self.addCleanup(environ.stop)
        for name in ("MISSING_KEY", "EXTRA_KEY"):
            os.environ.pop(name, None)
        super().setUp()
        self.configure(environment=["LLM_API_KEY", "HTTPS_PROXY"])

    def passed_names(self) -> list[str]:
        [create] = [call for call in self.engine.calls if call[1] == "create"]
        return [
            value
            for flag, value in zip(create, create[1:])
            if flag == "--env" and "=" not in value
        ]

    def assert_no_value_anywhere(self, *texts: str) -> None:
        for text in (*texts, *(json.dumps(call) for call in self.engine.calls)):
            self.assertNotIn(self.SECRET, text)
            self.assertNotIn(self.OTHER_SECRET, text)
        for path in self.fx.tmp.rglob("*"):
            if path.is_file():
                content = path.read_bytes()
                self.assertNotIn(self.SECRET.encode(), content, path)
                self.assertNotIn(self.OTHER_SECRET.encode(), content, path)

    def test_send_passes_each_approved_name_to_the_engine_without_its_value(
        self,
    ) -> None:
        dispatched = self.fx._dispatch(self.batch_id, "architect")
        self.engine.reset()

        sent = self.send(dispatched["brief"]["dispatch_id"])

        self.assertEqual(self.passed_names(), ["HTTPS_PROXY", "LLM_API_KEY"])
        self.assertEqual(
            dispatched["brief"]["runtime_access"]["environment"],
            ["HTTPS_PROXY", "LLM_API_KEY"],
        )
        status = coordinator.dispatch_status(
            self.fx._args(dispatch=sent["dispatch_id"], batch=None, stale_after=None)
        )
        self.assertEqual(status["dispatches"][0]["state"], "dispatched")
        self.assert_no_value_anywhere(
            json.dumps(dispatched), json.dumps(sent), json.dumps(status)
        )

    def test_a_missing_variable_blocks_preflight_and_propose_by_its_name(
        self,
    ) -> None:
        self.configure(environment=["LLM_API_KEY", "MISSING_KEY"])

        verification = self.preflight()
        with self.assertRaises(runtime_access.AccessError) as refused:
            self.fx._propose(self.batch_id, "architect")

        self.assertEqual(verification["status"], "unverified")
        self.assertIn("MISSING_KEY", verification["reason"])
        self.assertIn("MISSING_KEY", verification["remedy"])
        self.assertNotIn("LLM_API_KEY", verification["reason"])
        self.assertIn("MISSING_KEY", refused.exception.message)
        self.assert_no_container_was_created()
        self.assert_no_value_anywhere(
            json.dumps(verification),
            refused.exception.message,
            refused.exception.remedy,
        )

    def test_names_changed_or_lost_after_approval_never_change_the_approved_launch(
        self,
    ) -> None:
        brief = self.fx._dispatch(self.batch_id, "architect")["brief"]
        os.environ["EXTRA_KEY"] = "extra"
        self.configure(environment=["LLM_API_KEY", "HTTPS_PROXY", "EXTRA_KEY"])
        del os.environ["LLM_API_KEY"]

        with self.assertRaises(runtime_access.AccessError) as missing:
            self.send(brief["dispatch_id"])

        self.assertIn("LLM_API_KEY", missing.exception.message)
        self.assertEqual(self.state(brief["dispatch_id"]), "approved")
        self.assert_no_container_was_created()

        os.environ["LLM_API_KEY"] = self.SECRET
        self.engine.reset()
        self.send(brief["dispatch_id"])

        self.assertEqual(self.passed_names(), ["HTTPS_PROXY", "LLM_API_KEY"])
        self.assertEqual(self.state(brief["dispatch_id"]), "dispatched")

    def test_a_name_the_executor_sets_itself_is_blocked(self) -> None:
        for name in (
            "HOME",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_KEY_0",
            "GIT_CONFIG_VALUE_0",
            "HARNESS_EXECUTOR",
        ):
            with self.subTest(name=name):
                self.configure(environment=[name])

                verification = self.preflight()

                self.assertEqual(verification["status"], "unverified")
                self.assertIn(name, verification["reason"])
                self.assertIn("environment", verification["remedy"])
        self.assert_no_container_was_created()


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
