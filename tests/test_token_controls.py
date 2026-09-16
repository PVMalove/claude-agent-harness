#!/usr/bin/env python3
"""Focused regressions for bounded orchestration cost controls."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path


ORCHESTRATION_ROOT = Path(__file__).resolve().parents[1] / "harness" / "orchestration"
sys.path.insert(0, str(ORCHESTRATION_ROOT))

import contract  # noqa: E402
import coordinator  # noqa: E402


class TokenControlTests(unittest.TestCase):
    def _scope_args(self, **overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "expected_file": ["services/orders/checkout.py"],
            "expected_service": ["orders"],
            "expected_changed_lines": 120,
            "expected_context_tokens": None,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_preflight_accepts_a_bounded_ticket_and_records_conservative_context_floor(self) -> None:
        result = coordinator._scope_preflight(
            {}, "#1", "orders", ["protect checkout lock"], ["none"], self._scope_args(),
        )

        self.assertEqual(result["status"], "pass")
        self.assertGreaterEqual(result["expected_context_tokens"], 4_400)

    def test_preflight_rejects_a_ticket_that_exceeds_file_budget_before_dispatch(self) -> None:
        with self.assertRaisesRegex(coordinator.CoordinatorError, "expected_files=13 exceeds 12"):
            coordinator._scope_preflight(
                {}, "#372", "orders", ["one"], ["none"],
                self._scope_args(expected_file=[f"services/orders/file-{index}.py" for index in range(13)]),
            )

    def test_preflight_requires_declared_scope_estimates_by_default(self) -> None:
        with self.assertRaisesRegex(coordinator.CoordinatorError, "--expected-file"):
            coordinator._scope_preflight(
                {}, "#1", "orders", ["one"], ["none"],
                self._scope_args(expected_file=[], expected_service=[], expected_changed_lines=None),
            )

    def test_continuation_count_is_scoped_to_one_dispatch(self) -> None:
        batch = {
            "coordinator_decisions": [
                {"dispatch_id": "dispatch-a", "decision": "continue"},
                {"dispatch_id": "dispatch-a", "decision": "continue-automatic"},
                {"dispatch_id": "dispatch-b", "decision": "continue-automatic"},
            ]
        }
        self.assertEqual(coordinator._continuation_counts(batch, "dispatch-a"), (2, 1))

    def test_tokens_fields_are_not_secrets_but_session_token_is(self) -> None:
        self.assertIsNone(coordinator.SENSITIVE_KEY.search("input_tokens"))
        self.assertIsNotNone(coordinator.SENSITIVE_KEY.search("session_token"))

    def test_invalid_reasoning_effort_is_rejected_before_dispatch(self) -> None:
        with self.assertRaisesRegex(contract.ContractError, "assignment effort"):
            contract._valid_effort("highда")

    def test_communication_policy_defaults_to_english_protocol_and_russian_reports(self) -> None:
        self.assertEqual(
            coordinator._communication_policy({}),
            {"agent_to_agent_language": "en", "coordinator_report_language": "ru"},
        )
        with self.assertRaisesRegex(coordinator.CoordinatorError, "English.*Russian"):
            coordinator._communication_policy(
                {"communication_policy": {"agent_to_agent_language": "ru", "coordinator_report_language": "en"}},
            )

    def test_pre_approval_batch_shape_remains_usable_without_state_edit(self) -> None:
        batch_id = "batch-372-abc"
        legacy_fields = {
            "batch_id": batch_id,
            "created_at": "2026-01-01T00:00:00+00:00",
            "base_commit": "a" * 40,
            "integration_ref": "main",
            "branch_start_commit": "a" * 40,
            "ticket": "#372",
            "branch": "feature/issue-372-legacy",
            "worktree": "C:/worktree",
            "zone": "repository",
            "definition_of_done": ["preserve legacy batch"],
            "prohibited_changes": ["secrets"],
            "developer_verification_commands": ["test"],
            "verification_commands": ["test"],
            "required_gates": ["none"],
            "dependencies": ["none"],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            coordinator.LifecycleLedger(root).ensure()
            records = coordinator.LifecycleLedger(root).records_root()
            (records / "batches" / f"{batch_id}.json").write_text(
                json.dumps(legacy_fields), encoding="utf-8",
            )
            (records / "plans" / f"{batch_id}.json").write_text(
                json.dumps(legacy_fields), encoding="utf-8",
            )

            coordinator._validate_batch_integrity(root, legacy_fields)

    def test_pre_approval_shape_rejects_one_sided_approval_policy(self) -> None:
        batch_id = "batch-372-abc"
        legacy_fields = {
            "batch_id": batch_id,
            "created_at": "2026-01-01T00:00:00+00:00",
            "base_commit": "a" * 40,
            "integration_ref": "main",
            "branch_start_commit": "a" * 40,
            "ticket": "#372",
            "branch": "feature/issue-372-legacy",
            "worktree": "C:/worktree",
            "zone": "repository",
            "definition_of_done": ["preserve legacy batch"],
            "prohibited_changes": ["secrets"],
            "developer_verification_commands": ["test"],
            "verification_commands": ["test"],
            "required_gates": ["none"],
            "dependencies": ["none"],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            coordinator.LifecycleLedger(root).ensure()
            records = coordinator.LifecycleLedger(root).records_root()
            (records / "batches" / f"{batch_id}.json").write_text(
                json.dumps({**legacy_fields, "approval_policy": "manual_all"}), encoding="utf-8",
            )
            (records / "plans" / f"{batch_id}.json").write_text(
                json.dumps(legacy_fields), encoding="utf-8",
            )

            with self.assertRaisesRegex(coordinator.CoordinatorError, "batch record is incomplete"):
                coordinator._validate_batch_integrity(root, {**legacy_fields, "approval_policy": "manual_all"})


if __name__ == "__main__":
    unittest.main()
