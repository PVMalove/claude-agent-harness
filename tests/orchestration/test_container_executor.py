#!/usr/bin/env python3
"""The built-in ``docker`` runtime_access at its Python boundary (ADR 0022, ADR 0025).

The executor checks the plan and compares the pinned digests; ``harness-runtime`` observes the
engine and launches the worker. These tests need no runtime: a plan the executor itself blocks
never reaches it. The engine path is covered at seam 1 in ``test_container_runtime``.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from harness.orchestration import container_executor, extensions, runtime_access
from harness.orchestration.core.utils import JsonObject

WORKER_TAG = "harness-worker:local"
PROXY_TAG = "harness-proxy:local"
WORKER_ID = "sha256:" + "a" * 64
PROXY_ID = "sha256:" + "b" * 64
REBUILT_ID = "sha256:" + "c" * 64
CONTAINER_ID = "0123456789abcdef" * 4


def _plan(repo: Path, config: JsonObject) -> JsonObject:
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return runtime_access.resolve_plan(repo, repo, config, "developer", "write")


def _docker(**defaults: object) -> JsonObject:
    return {
        "access_policy": {
            "defaults": {
                "mode": "sandbox",
                "container": {"worker_image": WORKER_TAG},
                **defaults,
            }
        },
        "extensions": {"runtime_access": "docker"},
    }


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


class DockerPlanSupportTests(unittest.TestCase):
    """The blockers the executor finds in the plan and the host before it asks the runtime."""

    def setUp(self) -> None:
        directory = self.enterContext(
            tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        )
        self.tmp = Path(directory)
        # The runtime is never asked for a plan the executor blocks itself.
        self.client = self.enterContext(
            mock.patch.object(
                container_executor,
                "RuntimeClient",
                side_effect=AssertionError("the runtime was asked"),
            )
        )

    def verify(self, config: JsonObject, transport: str = "external") -> JsonObject:
        plan = _plan(self.tmp / "repo", config)
        verification, observation = runtime_access.verify_plan(plan, transport)
        self.assertIsNone(observation)
        self.assertEqual(verification["status"], "unverified")
        self.client.assert_not_called()
        return verification

    def test_a_plan_without_a_container_is_blocked_without_asking_the_runtime(
        self,
    ) -> None:
        verification = self.verify(
            {
                "access_policy": {"defaults": {"mode": "sandbox"}},
                "extensions": {"runtime_access": "docker"},
            }
        )

        self.assertEqual(
            verification["reason"], "the access plan has no container component"
        )
        self.assertIn("access_policy.defaults.container", verification["remedy"])

    def test_the_file_ledger_blocks_the_executor_with_the_cutover_remedy(self) -> None:
        verification = self.verify(_docker())

        self.assertEqual(
            verification["reason"],
            "the docker executor runs only on the runtime store epoch, and this "
            "repository still uses the file ledger",
        )
        self.assertIn("harness-runtime migrate --repo", verification["remedy"])
        self.assertIn("harness-runtime serve --repo", verification["remedy"])

    def test_every_plan_property_the_executor_cannot_run_is_reported_at_once(
        self,
    ) -> None:
        verification = self.verify(
            _docker(
                mode="unsandboxed",
                network={"hosts": ["api.example.com"]},
                environment=["HOME", "HTTPS_PROXY"],
            ),
            transport="in-process",
        )

        reason = verification["reason"]
        for expected in (
            "only an external dispatch, not in-process",
            "network hosts api.example.com need an egress proxy",
            "mode unsandboxed cannot run in a container",
            "sets these environment names itself: HOME, HTTPS_PROXY",
            "runtime store epoch",
        ):
            self.assertIn(expected, reason)
        for remedy in (
            "transport to external",
            "network.hosts to []",
            "mode to sandbox or inherit",
            "remove these names from the access_policy environment",
        ):
            self.assertIn(remedy, verification["remedy"])

    def test_the_proxy_names_are_reserved_only_with_hosts(self) -> None:
        verification = self.verify(_docker(environment=["HTTPS_PROXY", "NO_PROXY"]))

        self.assertNotIn("environment names", verification["reason"])


class DockerApplyTests(unittest.TestCase):
    """``apply`` sends only the image digests that the approval pinned."""

    def observation(
        self, worker: str, proxy: str | None = None
    ) -> container_executor.ContainerObservation:
        return container_executor.ContainerObservation(
            plan_digest="digest",
            transport="external",
            supported_modes=container_executor.SUPPORTED_MODES,
            effective_mode="sandbox",
            mechanism="native-apply",
            environment_id="podman:podman-host",
            launch_id=extensions.LAUNCH_AT_HANDOFF,
            source="native-runtime",
            observed_at=datetime.now(UTC).isoformat(),
            hosts=(),
            filesystem=(),
            engine="podman",
            engine_mode="rootful",
            worker_image=WORKER_TAG,
            worker_image_id=worker,
            proxy_image=PROXY_TAG if proxy else None,
            proxy_image_id=proxy,
        )

    def test_the_pinned_images_are_applied(self) -> None:
        brief = {"runtime_images": {"worker": WORKER_ID, "proxy": PROXY_ID}}

        applied = container_executor.DockerExecutor().apply(
            brief, self.observation(WORKER_ID, PROXY_ID)
        )

        self.assertTrue(applied.applied)
        self.assertFalse(applied.handed_off)

    def test_an_image_rebuilt_after_approval_is_refused_with_a_new_propose(
        self,
    ) -> None:
        brief = {"runtime_images": {"worker": WORKER_ID, "proxy": PROXY_ID}}

        with self.assertRaises(extensions.RuntimeAccessBlocked) as refused:
            container_executor.DockerExecutor().apply(
                brief, self.observation(WORKER_ID, REBUILT_ID)
            )

        self.assertEqual(
            refused.exception.message,
            f"image {PROXY_TAG} changed after dispatch propose: pinned {PROXY_ID}, "
            f"now {REBUILT_ID}",
        )
        self.assertIn("dispatch propose", refused.exception.remedy)

    def test_another_providers_evidence_is_refused(self) -> None:
        foreign = extensions.RuntimeAccessObservation(
            **{
                key: value
                for key, value in vars(self.observation(WORKER_ID)).items()
                if key in extensions.RuntimeAccessObservation.__dataclass_fields__
            }
        )

        with self.assertRaises(extensions.RuntimeAccessBlocked):
            container_executor.DockerExecutor().apply({}, foreign)

    def test_only_digests_of_the_named_images_are_valid_pins(self) -> None:
        worker_only = {"worker_image": WORKER_TAG}
        with_proxy = {"worker_image": WORKER_TAG, "proxy_image": PROXY_TAG}

        self.assertTrue(
            container_executor.valid_pins(
                {"worker": WORKER_ID, "proxy": None}, worker_only
            )
        )
        self.assertTrue(
            container_executor.valid_pins(
                {"worker": WORKER_ID, "proxy": PROXY_ID}, with_proxy
            )
        )
        for images, container in (
            ({"worker": WORKER_ID, "proxy": PROXY_ID}, worker_only),
            ({"worker": WORKER_ID, "proxy": None}, with_proxy),
            ({"worker": WORKER_TAG, "proxy": None}, worker_only),
            ({"worker": WORKER_ID}, worker_only),
        ):
            with self.subTest(images=images):
                self.assertFalse(container_executor.valid_pins(images, container))

    def test_the_built_in_executor_observes_and_applies_but_never_launches(
        self,
    ) -> None:
        provider = extensions.runtime_access("docker")

        for method in ("observe", "apply"):
            self.assertTrue(callable(getattr(provider, method, None)), method)
        self.assertFalse(hasattr(provider, "handoff"))


if __name__ == "__main__":
    unittest.main()
