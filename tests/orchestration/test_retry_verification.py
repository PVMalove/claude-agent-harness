"""Developer-retry checks over the related tests (issue #757).

With `retry_verification` configured, a developer-retry brief runs the lint commands and one test
command over the Context Package's `related_tests`; the first developer dispatch, code-review and
QA keep their lists. Without related tests, or without the key, the retry keeps the batch's
developer commands. The report's `checks_run` still has to match the brief exactly.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from harness.configuration_schema import value_problems
from harness.context_builder import context_builder
from harness.orchestration import contract
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from tests.orchestration import test_coordinator as coordinator_tests

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "harness" / "orchestration.example.json"
SCHEMA = ROOT / "harness" / "orchestration" / "orchestration.schema.json"
ROLES = ROOT / "harness" / "orchestration" / "roles"

POLICY = {"lint": ["make lint"], "test_command": "pytest -q"}
INVALID_POLICIES: dict[str, object] = {
    "not an object": ["make lint"],
    "lint not a list": {"lint": "make lint", "test_command": "pytest -q"},
    "lint not strings": {"lint": [1], "test_command": "pytest -q"},
    "missing test command": {"lint": ["make lint"]},
    "empty test command": {"lint": ["make lint"], "test_command": ""},
    "missing lint": {"test_command": "pytest -q"},
    "unknown field": {**POLICY, "max_tests": 3},
}


def _example() -> JsonObject:
    data: JsonObject = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    return data


def _retry_brief(related_tests: list[str], next_action: str) -> JsonObject:
    return {
        "transition": {"next_action": next_action},
        "context_package_summary": {"related_tests": related_tests},
    }


class RetryVerificationContractTests(unittest.TestCase):
    def test_retry_commands_are_lint_plus_one_test_command_over_related_tests(
        self,
    ) -> None:
        commands = contract.retry_verification_commands(
            POLICY, ["tests/test_x.py", "tests/a b.py"]
        )

        self.assertEqual(
            commands, ["make lint", "pytest -q tests/test_x.py 'tests/a b.py'"]
        )

    def test_no_related_tests_or_no_policy_gives_no_retry_commands(self) -> None:
        self.assertIsNone(contract.retry_verification_commands(POLICY, []))
        self.assertIsNone(
            contract.retry_verification_commands(None, ["tests/test_x.py"])
        )

    def test_only_a_developer_retry_brief_accepts_the_retry_commands(self) -> None:
        config = {
            "verification_commands": ["make verify"],
            "developer_verification_commands": ["make full"],
            "retry_verification": POLICY,
        }
        retry = ["make lint", "pytest -q tests/test_x.py"]

        accepted_retry = contract.accepted_verification_commands(
            config,
            "developer",
            "work",
            brief=_retry_brief(["tests/test_x.py"], "developer-retry"),
        )
        accepted_first = contract.accepted_verification_commands(
            config,
            "developer",
            "work",
            brief=_retry_brief(["tests/test_x.py"], "initial"),
        )
        accepted_review = contract.accepted_verification_commands(
            config,
            "code-review",
            "work",
            brief=_retry_brief(["tests/test_x.py"], "developer-retry"),
        )

        self.assertIn(retry, accepted_retry)
        self.assertIn(["make full"], accepted_retry)
        self.assertNotIn(retry, accepted_first)
        self.assertNotIn(retry, accepted_review)

    def test_the_example_ships_a_valid_retry_policy(self) -> None:
        self.assertEqual(
            _example()["retry_verification"],
            {"lint": ["make lint"], "test_command": "pytest -q -n 8"},
        )
        self.assertEqual(contract.health_problems(EXAMPLE, ROLES), [])

    def test_the_validator_and_the_schema_reject_the_same_invalid_policies(
        self,
    ) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        node = schema["properties"]["retry_verification"]

        self.assertEqual(value_problems(POLICY, node, schema), [])
        for case, policy in INVALID_POLICIES.items():
            with self.subTest(case=case):
                problems = [
                    problem
                    for problem in contract.configuration_problems(
                        {**_example(), "retry_verification": policy}, ROLES
                    )
                    if "retry_verification" in problem
                ]
                self.assertNotEqual(problems, [])
                self.assertNotEqual(value_problems(policy, node, schema), [])


class RetryVerificationDispatchTests(unittest.TestCase):
    """The developer-retry brief and its report, through the coordinator and a real ledger."""

    GENERAL = ["make full-developer"]
    REVIEW = ["make full-review"]

    def setUp(self) -> None:
        self.fx = coordinator_tests.CoordinatorRetryRoutingTests()
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)

    def _configure(self, policy: JsonObject | None) -> None:
        # No assignment plans and no access policy: dispatches run as in a zero-config project.
        # The default `auto_retry_min_severity` marks the policy config as authored, so its
        # verification lists replace the zero-config defaults.
        config: JsonObject = {
            "provider_profiles": {},
            "assignment_plans": {},
            "concurrency_budget": 1,
            "verification_commands": ["make verify"],
            "developer_verification_commands": self.GENERAL,
            "review_verification_commands": self.REVIEW,
            "auto_retry_min_severity": "info",
        }
        if policy is not None:
            config["retry_verification"] = policy
        (self.fx.repo / ".harness" / "orchestration.json").write_text(
            json.dumps(config), encoding="utf-8"
        )

    def _add_related_test(self) -> None:
        """A test that imports the module every developer commit of the fixture changes.

        Without the offline parser bundle the Repo Map stays path-only and has no import edges,
        so only the Context Builder's related-test lookup is replaced: it returns the test the
        parser would find. The coordinator, the ledger and the brief stay real.
        """
        tests = self.fx.repo / "tests"
        tests.mkdir(exist_ok=True)
        (tests / "test_x.py").write_text(
            "import services.x\n\n\ndef test_value():\n    assert services.x.VALUE\n",
            encoding="utf-8",
        )
        for arguments in (
            ("add", "-A"),
            ("commit", "-m", "test: related test"),
            ("push", "origin", "master"),
        ):
            subprocess.run(
                ["git", "-C", str(self.fx.repo), *arguments],
                check=True,
                capture_output=True,
            )
        related = mock.patch.object(
            context_builder,
            "_related_tests",
            side_effect=lambda _graph, files, starting: (
                ["tests/test_x.py"]
                if "services/x.py" in starting and "tests/test_x.py" in files
                else []
            ),
        )
        related.start()
        self.addCleanup(related.stop)

    def _retry_brief(self) -> tuple[str, JsonObject, JsonObject]:
        """First developer and review briefs, a code finding, then the developer-retry brief."""
        batch_id = self.fx._create_batch()["batch_id"]
        self.fx._accepted_architect(batch_id)
        first = self.fx._dispatch(batch_id, "developer")["brief"]
        self.fx._start(first["dispatch_id"])
        candidate, changed = self.fx._developer_commit("x")
        self.fx._submit(
            first["dispatch_id"], self.fx._developer_report(first, candidate, changed)
        )
        self.fx._decide(batch_id, "accept")
        self.fx._assess(batch_id, candidate, changed)
        review = self.fx._reported_review(
            batch_id,
            candidate,
            standards=(
                "warning",
                [
                    {
                        "severity": "warning",
                        "summary": "off-by-one",
                        "evidence": "x.py:1",
                    }
                ],
            ),
        )
        self.fx._decide(batch_id, "retry", reason_category="code")
        retry = self.fx._dispatch(batch_id, "developer")["brief"]
        self.assertEqual(retry["transition"]["next_action"], "developer-retry")
        return batch_id, first, {"review": review, "retry": retry}

    def test_the_retry_brief_runs_lint_and_the_related_tests_only(self) -> None:
        self._configure(POLICY)
        self._add_related_test()

        _, first, briefs = self._retry_brief()

        retry = briefs["retry"]
        self.assertEqual(
            retry["context_package_summary"]["related_tests"], ["tests/test_x.py"]
        )
        self.assertEqual(
            retry["verification_commands"], ["make lint", "pytest -q tests/test_x.py"]
        )
        self.assertEqual(first["verification_commands"], self.GENERAL)

    def test_a_retry_report_with_the_brief_commands_is_accepted(self) -> None:
        self._configure(POLICY)
        self._add_related_test()
        batch_id, _, briefs = self._retry_brief()
        retry = briefs["retry"]
        self.fx._start(retry["dispatch_id"])
        candidate, changed = self.fx._developer_commit("y")

        self.fx._submit(
            retry["dispatch_id"],
            self.fx._developer_report(retry, candidate, changed),
        )
        decided = self.fx._decide(batch_id, "accept")

        self.assertEqual(decided["next_action"], "risk-assessment")

    def test_a_retry_report_with_the_general_commands_is_refused(self) -> None:
        self._configure(POLICY)
        self._add_related_test()
        _, _, briefs = self._retry_brief()
        retry = briefs["retry"]
        self.fx._start(retry["dispatch_id"])
        candidate, changed = self.fx._developer_commit("y")
        report = self.fx._developer_report(
            retry,
            candidate,
            changed,
            checks_run=[
                {"command": command, "result": "pass", "evidence": "passed"}
                for command in self.GENERAL
            ],
        )

        with self.assertRaises(CoordinatorError):
            self.fx._submit(retry["dispatch_id"], report)

    def test_without_related_tests_the_retry_keeps_the_developer_commands(
        self,
    ) -> None:
        self._configure(POLICY)

        _, _, briefs = self._retry_brief()

        self.assertEqual(briefs["retry"]["verification_commands"], self.GENERAL)

    def test_without_the_key_the_retry_and_the_batch_are_unchanged(self) -> None:
        self._configure(None)
        self._add_related_test()

        batch_id, _, briefs = self._retry_brief()

        self.assertEqual(briefs["retry"]["verification_commands"], self.GENERAL)
        self.assertNotIn("retry_verification", self.fx._batch_record(batch_id))


if __name__ == "__main__":
    unittest.main()
