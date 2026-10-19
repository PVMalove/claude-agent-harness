#!/usr/bin/env python3
"""Worker liveness and terminal cleanup of a docker dispatch through the public commands (#708).

Real Git worktrees and the Lifecycle ledger: only ``container_executor.run_engine`` is replaced by
the scripted engine of ``test_container_executor``. Status, wait, attention, report and cancel run
as the coordinator runs them.
"""

from __future__ import annotations

import json
from pathlib import Path

from harness.orchestration import coordinator
from harness.orchestration.core.utils import JsonObject
from tests.orchestration import test_container_executor as executor_tests

CONTAINER_ID = executor_tests.CONTAINER_ID


class LifecycleFixture(executor_tests.DockerFixture):
    """A sent, self-reported architect dispatch whose worker runs in the scripted engine."""

    def setUp(self) -> None:
        super().setUp()
        self.brief = self.fx._dispatch(self.batch_id, "architect")["brief"]
        self.dispatch_id = self.brief["dispatch_id"]
        sent = self.send(self.dispatch_id)
        coordinator.self_report_dispatch(
            self.fx._args(
                dispatch=self.dispatch_id,
                model="sonnet",
                worktree=sent["worker_worktree"],
            )
        )
        self.engine.calls.clear()

    def status(self, stale_after: int | None = None) -> JsonObject:
        view: JsonObject = coordinator.dispatch_status(
            self.fx._args(
                dispatch=self.dispatch_id, batch=None, stale_after=stale_after
            )
        )
        entry: JsonObject = view["dispatches"][0]
        return entry

    def record(self) -> JsonObject:
        path = self.fx._records() / "dispatch-status" / f"{self.dispatch_id}.json"
        loaded: JsonObject = json.loads(Path(path).read_text(encoding="utf-8"))
        return loaded

    def wait(self) -> JsonObject:
        event: JsonObject = coordinator.wait_dispatch(
            self.fx._args(
                dispatch=self.dispatch_id, timeout=5, poll_interval=1, stale_after=900
            )
        )
        return event

    def batch(self) -> JsonObject:
        return self.fx._batch_record(self.batch_id)

    def removals(self) -> list[list[str]]:
        return [
            c
            for c in self.engine.calls
            if c[1] in ("rm",) or c[1:3] == ["network", "rm"]
        ]


class WorkerLivenessTests(LifecycleFixture):
    def test_a_live_worker_is_shown_running_and_nothing_is_written_or_removed(
        self,
    ) -> None:
        before = self.record()

        entry = self.status()

        self.assertEqual(
            entry["worker"],
            {
                "launch_id": CONTAINER_ID,
                "state": "running",
                "exit_code": None,
                "finished_at": None,
                "source": "engine",
                "teardown": None,
            },
        )
        self.assertFalse(entry["stale"])
        self.assertEqual(self.record(), before)
        self.assertNotIn("worker_exit", self.record())
        self.assertEqual(self.removals(), [])
        # The engine is asked about exactly the container the receipt names.
        self.assertEqual(
            self.engine.calls,
            [
                [
                    "docker",
                    "container",
                    "inspect",
                    "--format",
                    "{{json .}}",
                    CONTAINER_ID,
                ]
            ],
        )

    def test_silence_beyond_the_threshold_is_stale_but_never_proof_that_the_worker_stopped(
        self,
    ) -> None:
        self.fx._age_heartbeat(self.dispatch_id, 7200)

        entry = self.status(stale_after=900)

        self.assertTrue(entry["stale"])
        self.assertEqual(entry["worker"]["state"], "running")
        self.assertNotIn("worker_exit", self.record())
        self.assertEqual(self.removals(), [])
        self.assertEqual(self.record()["state"], "working")

    def test_a_worker_that_exited_without_a_report_is_stale_with_its_exit_code(
        self,
    ) -> None:
        self.engine.exit_worker(self.dispatch_id, 3)

        entry = self.status()

        self.assertEqual(entry["worker"]["state"], "exited")
        self.assertEqual(entry["worker"]["exit_code"], 3)
        self.assertTrue(entry["stale"])
        self.assertEqual(
            coordinator.dispatch_status(
                self.fx._args(dispatch=self.dispatch_id, batch=None, stale_after=None)
            )["stale"],
            [self.dispatch_id],
        )
        record = self.record()
        self.assertEqual(record["state"], "working")
        self.assertEqual(
            {
                key: record["worker_exit"][key]
                for key in ("launch_id", "state", "exit_code", "oom_killed")
            },
            {
                "launch_id": CONTAINER_ID,
                "state": "exited",
                "exit_code": 3,
                "oom_killed": False,
            },
        )
        # Not a terminal dispatch: nothing is removed.
        self.assertEqual(self.removals(), [])

    def test_the_first_proof_is_permanent(self) -> None:
        self.engine.exit_worker(self.dispatch_id, 3)
        self.status()
        first = self.record()["worker_exit"]
        self.engine.calls.clear()

        self.engine.exit_worker(self.dispatch_id, 9)
        entry = self.status()

        self.assertEqual(self.record()["worker_exit"], first)
        self.assertEqual(entry["worker"]["exit_code"], 3)
        self.assertEqual(entry["worker"]["source"], "ledger")
        self.assertEqual(self.engine.calls, [])

    def test_a_container_the_engine_no_longer_has_is_a_stop_proof_without_an_exit_code(
        self,
    ) -> None:
        self.engine.objects.clear()

        entry = self.status()

        self.assertEqual(entry["worker"]["state"], "missing")
        self.assertIsNone(entry["worker"]["exit_code"])
        self.assertTrue(entry["stale"])
        self.assertEqual(self.record()["worker_exit"]["state"], "missing")

    def test_an_unreachable_engine_or_foreign_labels_prove_nothing(self) -> None:
        foreign = {
            "harness.dispatch": self.dispatch_id,
            "harness.repository": "/somewhere/else/.git",
        }
        wrong_dispatch = {
            **foreign,
            "harness.repository": self.engine.objects[CONTAINER_ID]["labels"][
                "harness.repository"
            ],
            "harness.dispatch": "dispatch-other",
        }
        for case in ("engine down", "foreign repository", "foreign dispatch"):
            with self.subTest(case):
                self.engine.exit_worker(self.dispatch_id, 3)
                saved = dict(self.engine.objects[CONTAINER_ID])
                if case == "engine down":
                    self.engine.failures["container-inspect"] = (
                        "Cannot connect to the Docker daemon"
                    )
                else:
                    self.engine.objects[CONTAINER_ID]["labels"] = (
                        foreign if case == "foreign repository" else wrong_dispatch
                    )
                before = self.record()

                entry = self.status()

                self.assertEqual(entry["worker"]["state"], "unknown")
                self.assertFalse(entry["stale"])
                self.assertEqual(self.record(), before)
                self.assertNotIn("worker_exit", self.record())
                self.assertEqual(self.removals(), [])
                self.engine.failures.clear()
                self.engine.objects[CONTAINER_ID] = saved

    def test_wait_returns_stale_and_the_batch_needs_attention_for_an_exited_worker(
        self,
    ) -> None:
        self.engine.exit_worker(self.dispatch_id, 3)

        event = self.wait()

        self.assertEqual(event["event"], "stale")
        self.assertEqual(event["worker"]["exit_code"], 3)
        batch = self.batch()
        self.assertTrue(batch["needs_attention"])
        self.assertEqual(batch["attention_reason"], "stale-dispatch")
        self.assertIn("exit code 3", batch["last_safe_action"])
        self.assertIn("--runtime-stopped", batch["recommended_human_action"])
        self.assertEqual(self.removals(), [])

    def test_wait_keeps_waiting_while_the_worker_is_alive(self) -> None:
        event = coordinator.wait_dispatch(
            self.fx._args(
                dispatch=self.dispatch_id, timeout=1, poll_interval=1, stale_after=900
            )
        )

        self.assertEqual(event["event"], "timeout")

    def test_attention_check_raises_stale_dispatch_for_an_exited_worker_before_the_threshold(
        self,
    ) -> None:
        self.engine.exit_worker(self.dispatch_id, 3)
        self.status()

        flagged = self.fx._attention(self.batch_id)

        self.assertTrue(flagged["needs_attention"])
        self.assertEqual(flagged["findings"], [f"stale-dispatch:{self.dispatch_id}"])
        self.assertIn("exit code 3", self.batch()["last_safe_action"])

    def test_attention_check_stays_quiet_for_a_silent_worker_before_the_threshold(
        self,
    ) -> None:
        flagged = self.fx._attention(self.batch_id)

        self.assertFalse(flagged["needs_attention"])

    def test_cancelling_after_the_stop_keeps_the_receipt_and_the_exit_evidence(
        self,
    ) -> None:
        self.engine.exit_worker(self.dispatch_id, 3)
        self.status()
        exit_evidence = self.record()["worker_exit"]
        receipt = self.record()["runtime_access"]

        coordinator.cancel_dispatch(
            self.fx._args(
                dispatch=self.dispatch_id,
                reason="engine reports the worker exited",
                runtime_stopped=True,
                **self.fx._approval(),
            )
        )

        record = self.record()
        self.assertEqual(record["state"], "cancelled")
        self.assertEqual(record["runtime_access"], receipt)
        self.assertEqual(record["worker_exit"], exit_evidence)
