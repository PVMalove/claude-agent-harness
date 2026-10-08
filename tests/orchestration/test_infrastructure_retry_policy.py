"""Infrastructure policy through public contracts, coordinator, real Git and QA commands."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

import pytest

from harness.orchestration import contract, coordinator
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from tests.orchestration import test_qa_preparation_e2e as preparation
from tests.orchestration import test_coordinator as fixtures


def test_a_standalone_retry_opt_in_is_a_valid_project_config(tmp_path: Path) -> None:
    path = tmp_path / "orchestration.json"
    path.write_text(json.dumps({"infrastructure_retry_policy": {"enabled": True}}))
    roles = Path(__file__).resolve().parents[2] / "harness/orchestration/roles"
    assert contract.health_problems(path, roles) == []


@pytest.mark.parametrize(
    "value", [None, True, {}, {"enabled": 1}, {"enabled": True, "budget": 9}]
)
def test_malformed_retry_policy_is_rejected(tmp_path: Path, value: object) -> None:
    path = tmp_path / "orchestration.json"
    path.write_text(json.dumps({"infrastructure_retry_policy": value}))
    roles = Path(__file__).resolve().parents[2] / "harness/orchestration/roles"
    assert any(
        "infrastructure_retry_policy" in p
        for p in contract.health_problems(path, roles)
    )


class InfrastructureRetryTests(preparation.PreparationFixture):
    def configure_policy(self, *, enabled: bool = True, budget: int = 2) -> None:
        self.configure(preparation=[self.prepare])
        path = self.repo / ".harness/orchestration.json"
        config = json.loads(path.read_text())
        config.update(
            infrastructure_retry_policy={"enabled": enabled},
            attention_policy={"max_infrastructure_retries": budget},
        )
        path.write_text(json.dumps(config))

    def test_standalone_opt_in_keeps_session_assignment_and_project_gate(self) -> None:
        (self.repo / ".harness/orchestration.json").write_text(
            json.dumps({"infrastructure_retry_policy": {"enabled": True}})
        )
        brief = self.qa_brief()
        self.assertEqual(brief["verification_commands"], self.gate)
        self.assertEqual(brief["resolved_transport"], "in-process")
        self.assertTrue(
            brief["orchestration_policy"]["infrastructure_retry"]["enabled"]
        )
        self.assertEqual(brief["coordinator_approval"]["approved_by"], self.fx._approval()["approved_by"])

    def test_project_defect_remains_manual(self) -> None:
        self.configure_policy()
        brief = self.qa_brief()
        self.lock_broken.touch()
        result = self.run_qa(brief)
        self.assertEqual(self.report_of(result)["outcome"], "failed")
        self.assertIsNone(result.get("next_dispatch_id"))
        self.assertIsNone(self.complete(brief)["next_dispatch_id"])
        self.assertEqual(self.developer_retries(), 0)

    def test_failed_code_gate_remains_manual(self) -> None:
        self.configure_policy()
        brief = self.qa_brief()
        self.gate_failing.touch()
        result = self.run_qa(brief)
        self.assertEqual(self.report_of(result)["outcome"], "failed")
        self.assertIsNone(result.get("next_dispatch_id"))
        self.assertIsNone(self.complete(brief)["next_dispatch_id"])
        self.assertEqual(self.developer_retries(), 0)

    def test_unsupported_access_is_not_an_infrastructure_retry(self) -> None:
        self.configure_policy()
        path = self.repo / ".harness/orchestration.json"
        config = json.loads(path.read_text())
        config["access_policy"]["defaults"]["mode"] = "sandbox"
        path.write_text(json.dumps(config))
        brief = self.qa_brief()
        with self.assertRaises(CoordinatorError):
            self.run_qa(brief)
        with self.assertRaises(CoordinatorError):
            coordinator.retry_infrastructure_dispatch(
                self.fx._args(dispatch=brief["dispatch_id"])
            )
        self.assert_lane_free_and_dispatch(brief, "approved")
        self.assertTrue(
            coordinator.attention_check(self.fx._args(batch=self.batch_id))[
                "needs_attention"
            ]
        )

    def test_the_opt_in_and_qa_commands_are_bound_to_the_approved_contract(
        self,
    ) -> None:
        self.configure_policy()
        proposal = self.fx._propose(self.batch_id, "qa", candidate=self.candidate)
        self.configure_policy(enabled=False)
        with self.assertRaises(CoordinatorError):
            self.fx._dispatch(
                self.batch_id,
                "qa",
                candidate=self.candidate,
                digest=proposal["transition_digest"],
            )
        self.configure_policy()
        brief = self.qa_brief()
        snapshot = brief["orchestration_policy"]["infrastructure_retry"]
        self.assertTrue(snapshot["enabled"])
        self.assertEqual(snapshot["max_infrastructure_retries"], 2)
        self.assertEqual(snapshot["qa_preparation"], [self.prepare])
        self.configure(
            preparation=[self._script("wrong", "raise Exception('wrong commands')")]
        )
        result = self.run_qa(brief)
        self.assertEqual(self.report_of(result)["outcome"], "completed")

    def complete(self, brief: JsonObject) -> JsonObject:
        return coordinator.complete_report(self.fx._args(dispatch=brief["dispatch_id"]))

    def test_ready_environment_prepares_one_same_sha_retry_without_human_approval(
        self,
    ) -> None:
        self.configure_policy()
        first = self.qa_brief()
        self.registry_down.touch()
        original = self.run_qa(first)
        self.assertEqual(self.report_of(original)["outcome"], "blocked")
        with self.assertRaises(CoordinatorError):
            self.complete(first)
        self.registry_down.unlink()
        resumed = self.complete(first)
        self.assertIsNotNone(resumed["next_dispatch_id"])
        repeated = self.complete(first)
        self.assertEqual(resumed["next_dispatch_id"], repeated["next_dispatch_id"])
        second = coordinator._load_dispatch(
            self.fx.state_dir, resumed["next_dispatch_id"]
        )
        self.assertNotEqual(second["dispatch_id"], first["dispatch_id"])
        self.assertEqual(second["candidate_commit"], self.candidate)
        self.assertEqual(second["runtime_access"], first["runtime_access"])
        self.assertEqual(
            second["coordinator_approval"]["approved_by"], "policy:infrastructure-retry"
        )
        self.assertEqual(self.developer_retries(), 0)
        self.assertEqual(self.report_of(self.run_qa(second))["outcome"], "completed")
        self.assertTrue(Path(original["report"]).exists())

    def test_interruption_after_policy_decision_replays_without_resetting_budget(
        self,
    ) -> None:
        self.configure_policy(budget=1)
        first = self.qa_brief()
        self.registry_down.touch()
        self.run_qa(first)
        self.registry_down.unlink()
        with mock.patch(
            "harness.orchestration.workflow.completion.create_dispatch",
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.complete(first)
        self.assertFalse(
            coordinator.attention_check(self.fx._args(batch=self.batch_id))[
                "needs_attention"
            ]
        )
        retry = self.complete(first)
        self.assertEqual(
            retry["next_dispatch_id"], self.complete(first)["next_dispatch_id"]
        )
        self.assertEqual(self.developer_retries(), 0)

    def test_budget_stops_before_the_next_dispatch_and_raises_attention(self) -> None:
        self.configure_policy(budget=1)
        first = self.qa_brief()
        self.registry_down.touch()
        self.run_qa(first)
        self.registry_down.unlink()
        retried = self.complete(first)
        second = coordinator._load_dispatch(
            self.fx.state_dir, retried["next_dispatch_id"]
        )
        self.registry_down.touch()
        self.run_qa(second)
        self.registry_down.unlink()
        with self.assertRaises(CoordinatorError) as refused:
            self.complete(second)
        self.assertIn("budget exhausted", refused.exception.message)
        view = coordinator.attention_check(self.fx._args(batch=self.batch_id))
        self.assertTrue(view["needs_attention"])
        self.assertEqual(view["attention_reason"], "infrastructure-retry-repeated")
        self.assertEqual(self.developer_retries(), 0)

    def test_without_opt_in_ready_environment_still_requires_a_manual_decision(
        self,
    ) -> None:
        self.configure_policy(enabled=False)
        first = self.qa_brief()
        self.registry_down.touch()
        self.run_qa(first)
        self.registry_down.unlink()
        self.configure_policy(enabled=True)
        self.assertIsNone(self.complete(first)["next_dispatch_id"])

    def test_zero_budget_never_creates_a_retry(self) -> None:
        self.configure_policy(budget=0)
        first = self.qa_brief()
        self.registry_down.touch()
        self.run_qa(first)
        self.registry_down.unlink()
        with self.assertRaises(CoordinatorError):
            self.complete(first)
        self.assertTrue(
            coordinator.attention_check(self.fx._args(batch=self.batch_id))[
                "needs_attention"
            ]
        )

    def test_default_budget_allows_two_retries_then_stops(self) -> None:
        self.configure_policy()
        brief = self.qa_brief()
        ids = {brief["dispatch_id"]}
        for _ in range(2):
            self.registry_down.touch()
            self.run_qa(brief)
            self.registry_down.unlink()
            result = self.complete(brief)
            brief = coordinator._load_dispatch(
                self.fx.state_dir, result["next_dispatch_id"]
            )
            ids.add(brief["dispatch_id"])
        self.assertEqual(len(ids), 3)
        self.registry_down.touch()
        self.run_qa(brief)
        self.registry_down.unlink()
        with self.assertRaises(CoordinatorError):
            self.complete(brief)
        self.assertEqual(self.developer_retries(), 0)

    def test_unknown_preparation_needs_attention_and_never_creates_policy_dispatch(
        self,
    ) -> None:
        self.configure_policy()
        path = self.repo / ".harness/orchestration.json"
        config = json.loads(path.read_text())
        config["qa_environment_probes"] = []
        config["qa_project_file_checks"] = []
        path.write_text(json.dumps(config))
        brief = self.qa_brief()
        self.registry_down.touch()
        result = self.run_qa(brief)
        self.assertEqual(
            self.report_of(result)["qa_stages"]["diagnosis"]["category"], "unknown"
        )
        self.assertIsNone(result["next_dispatch_id"])
        self.assertTrue(
            coordinator.attention_check(self.fx._args(batch=self.batch_id))[
                "needs_attention"
            ]
        )

    def test_changed_commands_or_access_stop_policy_continuation(self) -> None:
        self.configure_policy()
        brief = self.qa_brief()
        self.registry_down.touch()
        self.run_qa(brief)
        self.registry_down.unlink()
        path = self.repo / ".harness/orchestration.json"
        config = json.loads(path.read_text())
        config["qa_preparation"] = ["different-command"]
        config["access_policy"]["defaults"]["network"] = {"hosts": ["new.example"]}
        path.write_text(json.dumps(config))
        with self.assertRaises(CoordinatorError):
            self.complete(brief)
        self.assertTrue(
            coordinator.attention_check(self.fx._args(batch=self.batch_id))[
                "needs_attention"
            ]
        )

    def test_live_tools_cannot_expand_a_policy_retry(self) -> None:
        self.configure_policy()
        path = self.repo / ".harness/orchestration.json"
        config = json.loads(path.read_text())
        config["tool_policy"] = {"roles": {"qa": ["Read"]}}
        path.write_text(json.dumps(config))
        first = self.qa_brief()
        self.registry_down.touch()
        self.run_qa(first)
        self.registry_down.unlink()
        config["tool_policy"]["roles"]["qa"] = ["Read", "Bash"]
        path.write_text(json.dumps(config))
        with self.assertRaises(CoordinatorError):
            self.complete(first)
        self.assertTrue(
            coordinator.attention_check(self.fx._args(batch=self.batch_id))[
                "needs_attention"
            ]
        )

    def test_same_denied_qa_directory_does_not_cancel_or_retry(self) -> None:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root is not refused by filesystem mode bits")
        from harness.storage import storage_path

        self.configure_policy()
        brief = self.qa_brief()
        checkout = storage_path(self.repo, "runs", "qa")
        checkout.mkdir(parents=True, exist_ok=True)
        mode = checkout.stat().st_mode & 0o7777
        checkout.chmod(mode & ~0o222)
        try:
            with self.assertRaises(CoordinatorError):
                self.run_qa(brief)
            with self.assertRaises(CoordinatorError):
                coordinator.retry_infrastructure_dispatch(
                    self.fx._args(dispatch=brief["dispatch_id"])
                )
            self.assert_lane_free_and_dispatch(brief, "approved")
        finally:
            checkout.chmod(mode)
        retry = coordinator.retry_infrastructure_dispatch(
            self.fx._args(dispatch=brief["dispatch_id"])
        )
        self.assertNotEqual(retry["dispatch_id"], brief["dispatch_id"])
        self.assertEqual(
            self.report_of(self.run_qa(retry["brief"]))["outcome"], "completed"
        )


class CoordinatorOperationRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = fixtures.CoordinatorRetryRoutingTests()
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)

    def test_publish_metadata_denial_recovers_with_a_new_policy_approved_dispatch(
        self,
    ) -> None:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root is not refused by filesystem mode bits")
        fx = self.fx
        batch = fx._create_batch()
        fx._accepted_architect(batch["batch_id"])
        candidate = fx._accepted_candidate(batch["batch_id"])
        fx._accepted_review_and_qa(batch["batch_id"], candidate)
        (fx.repo / ".harness/orchestration.json").write_text(
            json.dumps(
                {
                    "access_policy": {"defaults": {"mode": "inherit"}},
                    "infrastructure_retry_policy": {"enabled": True},
                }
            )
        )
        first = fx._dispatch(
            batch["batch_id"], "developer", purpose="publish", candidate=candidate
        )["brief"]
        metadata = fx.repo / ".git"
        mode = metadata.stat().st_mode & 0o7777
        metadata.chmod(mode & ~0o222)
        try:
            with self.assertRaises(CoordinatorError):
                coordinator.publish_dispatch(
                    fx._args(dispatch=first["dispatch_id"], remote="origin")
                )
            with self.assertRaises(CoordinatorError):
                coordinator.retry_infrastructure_dispatch(
                    fx._args(dispatch=first["dispatch_id"])
                )
        finally:
            metadata.chmod(mode)
        with mock.patch(
            "harness.orchestration.workflow.dispatch.create_dispatch",
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                coordinator.retry_infrastructure_dispatch(
                    fx._args(dispatch=first["dispatch_id"])
                )
        retry = coordinator.retry_infrastructure_dispatch(
            fx._args(dispatch=first["dispatch_id"])
        )
        self.assertNotEqual(retry["dispatch_id"], first["dispatch_id"])
        again = coordinator.retry_infrastructure_dispatch(
            fx._args(dispatch=first["dispatch_id"])
        )
        self.assertEqual(again["dispatch_id"], retry["dispatch_id"])
        other = fx.tmp / "other.git"
        preparation._git(fx.repo, "init", "--bare", str(other))
        preparation._git(fx.repo, "remote", "add", "other", str(other))
        with self.assertRaises(CoordinatorError):
            coordinator.publish_dispatch(
                fx._args(dispatch=retry["dispatch_id"], remote="other")
            )
        coordinator.attention_resolve(
            fx._args(
                batch=batch["batch_id"],
                note="restored original approved destination",
                **fx._approval(),
            )
        )
        published = coordinator.publish_dispatch(
            fx._args(dispatch=retry["dispatch_id"], remote="origin")
        )
        self.assertEqual(published["candidate_commit"], candidate)
        self.assertEqual(published["state"], "reported")
