"""Public local integration QA, with real Git and terminal ledger history."""

from __future__ import annotations

import argparse
import json
import threading
import unittest
from pathlib import Path
from unittest import mock

from harness.gate_runner.gate_runner import (
    ExecutionPolicy,
    GateResult,
    GateRunnerError,
    LocalPolicy,
    run_gate,
)

from harness.orchestration import coordinator, qa_lane
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.ledger import LifecycleLedger
from harness.orchestration.workflow import integration, local_qa
from tests.orchestration.test_integration_record import PublishedBranch, _git


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

    def test_refreshed_pair_requires_verified_generated_evidence_and_rechecks_artifact(
        self,
    ) -> None:
        self.branch.advance_integration_ref()
        refreshed = coordinator.integration_refresh(
            self.branch.args(record=self.record)
        )
        pair = coordinator.integration_status(self.branch.args(record=self.record))
        self.assertFalse(pair["verification"]["satisfied"])
        coordinator.integration_link_evidence(
            self.branch.args(
                record=self.record,
                kind="local-qa",
                result="passed",
                reference="manual-local-log",
                artifact_sha256=None,
                candidate_commit=pair["candidate_sha"],
                target_commit=pair["target_sha"],
            )
        )
        self.assertFalse(
            coordinator.integration_status(self.branch.args(record=self.record))[
                "verification"
            ]["satisfied"]
        )
        result = coordinator.integration_local_qa(self.args())
        self.assertEqual(result["verification"], "verified")
        self.assertEqual(result["candidate_sha"], refreshed["new_candidate_sha"])
        self.assertTrue(
            coordinator.integration_status(self.branch.args(record=self.record))[
                "verification"
            ]["satisfied"]
        )
        with mock.patch.object(
            local_qa, "run_gate", side_effect=AssertionError("gate reran")
        ):
            self.assertEqual(
                coordinator.integration_local_qa(self.args())["request_id"],
                result["request_id"],
            )
        Path(result["artifact"]).write_text("altered", encoding="utf-8")
        self.assertFalse(
            coordinator.integration_status(self.branch.args(record=self.record))[
                "verification"
            ]["satisfied"]
        )

        self.assertEqual(
            coordinator.integration_local_qa(self.args())["verification"], "unverified"
        )

    def test_target_movement_during_execution_keeps_historical_evidence_unverified(
        self,
    ) -> None:
        original = run_gate

        def gate(
            commands: list[str | list[str]],
            policy: ExecutionPolicy,
            *,
            stop_on_failure: bool,
        ) -> GateResult:
            result = original(commands, policy, stop_on_failure=stop_on_failure)
            self.branch.advance_integration_ref()
            return result

        with mock.patch.object(local_qa, "run_gate", side_effect=gate):
            result = coordinator.integration_local_qa(self.args())
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["verification"], "unverified")
        self.assertTrue(Path(result["artifact"]).is_file())
        self.assertEqual(
            coordinator.integration_local_qa(self.args())["verification"], "unverified"
        )
        self.assertFalse(
            coordinator.integration_status(self.branch.args(record=self.record))[
                "pair_checks"
            ][0]["applies_to_current_pair"]
        )

    def test_incomplete_command_coverage_cannot_verify_or_reuse_different_inputs(
        self,
    ) -> None:
        gate = GateResult([], "", 0.0)
        with mock.patch.object(local_qa, "run_gate", return_value=gate):
            result = coordinator.integration_local_qa(self.args())
        self.assertEqual(result["verification"], "unverified")
        with self.assertRaises(CoordinatorError):
            coordinator.integration_local_qa(
                self.args(request=result["request_id"], reason="Different CI assertion")
            )

    def test_local_qa_applicability_requires_both_candidate_and_target(self) -> None:
        pair = coordinator.integration_status(self.branch.args(record=self.record))
        linked = coordinator.integration_link_evidence(
            self.branch.args(
                record=self.record,
                kind="local-qa",
                result="passed",
                reference="wrong-candidate",
                artifact_sha256=None,
                candidate_commit="3" * 40,
                target_commit=pair["target_sha"],
            )
        )
        checks = coordinator.integration_status(self.branch.args(record=self.record))[
            "pair_checks"
        ]
        self.assertFalse(
            next(
                check
                for check in checks
                if check["evidence_id"] == linked["evidence_id"]
            )["applies_to_current_pair"]
        )

    def test_failures_are_retained_and_operational_retries_are_explicit_and_bounded(
        self,
    ) -> None:
        failure = GateRunnerError(
            "checkout unavailable password=private", remedy="restore checkout"
        )
        with mock.patch.object(local_qa, "run_gate", side_effect=failure) as runner:
            first = coordinator.integration_local_qa(self.args())
            self.assertEqual(first["state"], "unavailable")
            self.assertEqual(first["findings"], [])
            repeated = coordinator.integration_local_qa(self.args())
            self.assertEqual(repeated["state"], "unavailable")
            self.assertEqual(runner.call_count, 1)
            for _ in range(2):
                self.assertEqual(
                    coordinator.integration_local_qa(self.args(retry=True))["state"],
                    "unavailable",
                )
            self.assertEqual(
                coordinator.integration_local_qa(self.args(retry=True))["state"],
                "exhausted",
            )
            self.assertEqual(runner.call_count, 3)
        config = self.branch.repo / ".harness/project.json"
        value = json.loads(config.read_text(encoding="utf-8"))
        value["qa_gate_commands"] = ["printf 'password=private'; exit 1"]
        config.write_text(json.dumps(value), encoding="utf-8")
        failed = coordinator.integration_local_qa(self.args())
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(len(failed["findings"]), 1)
        self.assertEqual(failed["findings"][0]["command"], value["qa_gate_commands"][0])
        self.assertNotIn(
            "private", Path(failed["artifact"]).read_text(encoding="utf-8")
        )
        with mock.patch.object(
            local_qa, "run_gate", side_effect=AssertionError("failed gate retried")
        ):
            self.assertEqual(
                coordinator.integration_local_qa(self.args(retry=True))["state"],
                "failed",
            )
        self.assertIsNone(
            coordinator.qa_status(
                argparse.Namespace(repo=str(self.branch.repo), state_dir=None)
            )["lease"]
        )

    def test_finalization_persistence_failure_resumes_without_rerunning_gate(
        self,
    ) -> None:
        with mock.patch.object(
            integration,
            "_store_evidence",
            side_effect=CoordinatorError(
                "link persistence unavailable", remedy="restore ledger"
            ),
        ):
            first = coordinator.integration_local_qa(self.args())
        self.assertEqual(first["state"], "unavailable")
        self.assertEqual(first["checks_run"][0]["result"], "pass")
        self.assertTrue(Path(first["artifact"]).exists())
        with mock.patch.object(
            local_qa,
            "run_gate",
            side_effect=AssertionError("gate reran after finalization"),
        ):
            finished = coordinator.integration_local_qa(self.args())
        self.assertEqual(finished["verification"], "verified")
        self.assertIsNone(
            coordinator.qa_status(
                argparse.Namespace(repo=str(self.branch.repo), state_dir=None)
            )["lease"]
        )

    def test_command_launch_unavailability_preserves_completed_checks(self) -> None:
        with self.assertRaises(GateRunnerError) as caught:
            run_gate(
                [["git", "--version"], ["harness-test-command-does-not-exist"]],
                LocalPolicy(self.branch.repo),
                stop_on_failure=True,
            )
        failure = caught.exception
        self.assertIsNotNone(failure.partial_result)
        with mock.patch.object(local_qa, "run_gate", side_effect=failure):
            result = coordinator.integration_local_qa(self.args())
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["checks_run"][0]["result"], "pass")
        self.assertEqual(result["findings"], [])
        self.assertTrue(Path(result["artifact"]).exists())

    def test_remote_unavailability_does_not_execute_or_report_a_failed_check(
        self,
    ) -> None:
        remote = _git(self.branch.repo, "remote", "get-url", "origin")
        _git(
            self.branch.repo,
            "remote",
            "set-url",
            "origin",
            "/nonexistent/local-qa-test-origin.git",
        )
        with mock.patch.object(
            local_qa,
            "run_gate",
            side_effect=AssertionError("gate ran without pair observation"),
        ):
            result = coordinator.integration_local_qa(self.args())
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["stage"], "pair-observation")
        self.assertEqual(result["checks_run"], [])
        self.assertEqual(result["findings"], [])
        _git(self.branch.repo, "remote", "set-url", "origin", remote)
        self.assertEqual(
            coordinator.integration_local_qa(self.args(retry=True))["verification"],
            "verified",
        )

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
                self.assertTrue(
                    coordinator.integration_local_qa(self.args())["running"]
                )
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
