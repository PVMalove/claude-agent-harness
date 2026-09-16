#!/usr/bin/env python3
"""Focused regressions for bounded orchestration cost controls."""

from __future__ import annotations

import argparse
import sys
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


if __name__ == "__main__":
    unittest.main()
