"""Public local integration QA, with real Git and terminal ledger history."""

from __future__ import annotations

import argparse
import json
import unittest

from harness.orchestration import coordinator
from harness.orchestration.core.utils import CoordinatorError, JsonObject
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
