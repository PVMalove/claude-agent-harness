"""Focused verification command routing for orchestration roles (issue #306)."""

from __future__ import annotations

import unittest

from harness.orchestration.core import config
from harness.orchestration.workflow import dispatch


class VerificationCommandRoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.full = ["python scripts/verify.py"]
        self.developer = ["python -m pytest tests/test_context_builder.py"]
        self.review = ["python -m pytest tests/test_context_builder.py"]

    def test_review_commands_are_opt_in_with_a_safe_full_gate_fallback(self) -> None:
        legacy = {"verification_commands": self.full}
        configured = {**legacy, "review_verification_commands": self.review}

        self.assertEqual(config._review_verification_commands(legacy), self.full)
        self.assertEqual(config._review_verification_commands(configured), self.review)

    def test_each_work_role_receives_its_frozen_command_list(self) -> None:
        batch = {
            "verification_commands": self.full,
            "developer_verification_commands": self.developer,
            "review_verification_commands": self.review,
        }

        self.assertEqual(
            dispatch._dispatch_verification_commands(batch, "developer", "work"),
            self.developer,
        )
        self.assertEqual(
            dispatch._dispatch_verification_commands(batch, "code-review", "work"),
            self.review,
        )
        self.assertEqual(
            dispatch._dispatch_verification_commands(batch, "qa", "work"), self.full
        )
        self.assertEqual(
            dispatch._dispatch_verification_commands(batch, "developer", "publish"),
            self.full,
        )


if __name__ == "__main__":
    unittest.main()
