"""Public local integration QA, with real Git and terminal ledger history."""

from __future__ import annotations

import argparse
import unittest

from harness.orchestration import coordinator
from harness.orchestration.core.utils import CoordinatorError
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

    def test_public_request_records_all_fallback_conditions_without_rewriting_history(
        self,
    ) -> None:
        before = self.branch.snapshot()
        for condition in ("absent", "unavailable", "unusable"):
            result = coordinator.integration_local_qa(self.args(ci_condition=condition))
            self.assertEqual(result["state"], "requested")
            self.assertEqual(result["ci_condition"], condition)
            self.assertEqual(result["verification_commands"], ["true"])
            self.assertEqual(
                coordinator.integration_local_qa(self.args(ci_condition=condition))[
                    "request_id"
                ],
                result["request_id"],
            )
        self.assertEqual(self.branch.snapshot(), before)

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
