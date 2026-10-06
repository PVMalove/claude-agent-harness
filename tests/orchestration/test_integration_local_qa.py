"""Public local integration QA, with real Git and terminal ledger history."""

from __future__ import annotations

import argparse
import json
import threading
import unittest
from unittest import mock

from harness.gate_runner.gate_runner import GateResult

from harness.orchestration import coordinator, qa_lane
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.ledger import LifecycleLedger
from tests.orchestration.test_integration_record import PublishedBranch


class LocalQaContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.branch = PublishedBranch()
        self.branch.fixture.state_dir = (
            self.branch.repo / ".harness/orchestration/state"
        )
        self.branch.publish()
        self.record = self.branch.prepare()["integration_record_id"]

    def tearDown(self) -> None:
        self.branch.close()

    def args(self, **changes: object) -> argparse.Namespace:
        values = dict(
            record=self.record,
            ci_condition="absent",
            reason="No combined CI",
            request=None,
            retry=False,
            lease_seconds=None,
        )
        values.update(changes)
        return self.branch.args(**values)

    def assert_history_unchanged(self, before: JsonObject) -> None:
        after = self.branch.snapshot()
        self.assertEqual(after["git"], before["git"])
        for path, checksum in before["ledger_files"].items():
            if not path.startswith("qa-lane/"):
                self.assertEqual(after["ledger_files"][path], checksum)

    def test_public_request_records_all_fallback_conditions_without_rewriting_history(
        self,
    ) -> None:
        before = self.branch.snapshot()
        for condition in ("absent", "unavailable", "unusable"):
            result = coordinator.integration_local_qa(self.args(ci_condition=condition))
            self.assertEqual(result["state"], "completed")
            self.assertEqual(result["ci_condition"], condition)
            self.assertEqual(result["verification_commands"], ["true"])
            self.assertEqual(
                coordinator.integration_local_qa(self.args(ci_condition=condition))[
                    "request_id"
                ],
                result["request_id"],
            )
        self.assert_history_unchanged(before)

    def test_gate_uses_exact_detached_candidate_and_leaves_live_work_unchanged(
        self,
    ) -> None:
        config = self.branch.repo / ".harness/project.json"
        value = json.loads(config.read_text(encoding="utf-8"))
        value["qa_gate_commands"] = [
            "git rev-parse HEAD",
            "git symbolic-ref -q HEAD && exit 1 || exit 0",
        ]
        config.write_text(json.dumps(value), encoding="utf-8")
        before = self.branch.snapshot()
        result = coordinator.integration_local_qa(self.args())
        self.assertEqual(result["state"], "completed")
        self.assertEqual(
            [check["result"] for check in result["checks_run"]], ["pass", "pass"]
        )
        self.assertIn(result["candidate_sha"], result["checks_run"][0]["evidence"])
        self.assert_history_unchanged(before)

    def test_missing_reason_or_invalid_ci_assertion_is_rejected(self) -> None:
        for changes in ({"reason": " "}, {"ci_condition": "available"}):
            with self.subTest(changes=changes), self.assertRaises(CoordinatorError):
                coordinator.integration_local_qa(self.args(**changes))

    def test_blocked_gate_releases_ledger_lock_and_excludes_other_qa_owners(
        self,
    ) -> None:
        entered, finish = threading.Event(), threading.Event()
        results: list[JsonObject] = []
        errors: list[BaseException] = []

        def gate(*args: object, **kwargs: object) -> GateResult:
            entered.set()
            if not finish.wait(10):
                raise RuntimeError("test gate was not released")
            return GateResult(
                [{"command": "true", "result": "pass", "evidence": "exit 0"}],
                "$ true\nexit_code=0\n",
                0.0,
            )

        def run() -> None:
            try:
                results.append(coordinator.integration_local_qa(self.args()))
            except BaseException as exc:
                errors.append(exc)

        with mock.patch(
            "harness.orchestration.workflow.local_qa.run_gate", side_effect=gate
        ) as runner:
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(entered.wait(10))
                # Ordinary ledger operations can proceed while the heavy gate is blocked.
                status = coordinator.integration_status(
                    self.branch.args(record=self.record)
                )
                self.assertEqual(status["state"], "current")
                queued = coordinator.integration_local_qa(
                    self.args(reason="CI unavailable for another request")
                )
                self.assertEqual(queued["state"], "queued")
                ledger = LifecycleLedger(self.branch.state_root())
                with ledger.lock():
                    ordinary = qa_lane.acquire(
                        ledger, "dispatch-abc123", coordinator, lease_seconds=1800
                    )
                self.assertEqual(ordinary["state"], "queued")
                self.assertEqual(ordinary["position"], 3)
                self.assertEqual(runner.call_count, 1)
            finally:
                finish.set()
                thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(results[0]["state"], "completed")

    def test_public_cli_requires_ci_assertion_and_has_no_partial_command_option(
        self,
    ) -> None:
        parsed = coordinator.parser().parse_args(
            [
                "integration",
                "local-qa",
                "--record",
                self.record,
                "--ci-condition",
                "absent",
                "--reason",
                "No combined CI",
            ]
        )
        self.assertIs(parsed.handler, coordinator.integration_local_qa)
        with self.assertRaises(SystemExit):
            coordinator.parser().parse_args(
                [
                    "integration",
                    "local-qa",
                    "--record",
                    self.record,
                    "--ci-condition",
                    "absent",
                    "--reason",
                    "No CI",
                    "--command",
                    "true",
                ]
            )
