#!/usr/bin/env python3
"""``approval_policy: auto``: the automatic path from batch approve to accepted publish (issue #643).

The end-to-end run on real Git and a real ledger lives in ``test_coordinator.py``; these tests pin
the parts on data: the configuration gate, the hashed ledger records and their validation, the
decision table, the closed stop list and the final report.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from harness.orchestration.core import config
from harness.orchestration.core.utils import CoordinatorError


class AutoConfigGateTests(unittest.TestCase):
    def _repo(self, policy: dict[str, object]) -> Path:
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(repo, ignore_errors=True))
        (repo / ".harness").mkdir()
        (repo / ".harness/project.json").write_text("{}", encoding="utf-8")
        (repo / ".harness/orchestration.json").write_text(
            json.dumps({"access_policy": {"defaults": {"mode": "inherit"}}, **policy}),
            encoding="utf-8",
        )
        return repo

    def test_approval_policy_refuses_auto_under_a_tty_gate(self) -> None:
        with self.assertRaises(CoordinatorError) as refused:
            config._approval_policy(
                {"approval_policy": "auto", "human_approval_gate": "tty"}
            )
        self.assertIn("human_approval_gate", refused.exception.message)
        self.assertIn("trusted", refused.exception.remedy)
        for policy in ("manual_all", "milestone", "low_risk"):
            with self.subTest(policy=policy):
                self.assertEqual(
                    config._approval_policy(
                        {"approval_policy": policy, "human_approval_gate": "tty"}
                    ),
                    policy,
                )

    def test_config_validation_refuses_an_incompatible_auto_config(self) -> None:
        for policy, problem in (
            ({"approval_policy": "auto"}, "worker_attestation_required"),
            (
                {"approval_policy": "auto", "worker_attestation_required": False},
                "worker_attestation_required",
            ),
            (
                {
                    "approval_policy": "auto",
                    "worker_attestation_required": True,
                    "human_approval_gate": "tty",
                },
                "human_approval_gate",
            ),
        ):
            with (
                self.subTest(policy=policy),
                self.assertRaises(CoordinatorError) as refused,
            ):
                config._config(self._repo(policy))
            self.assertIn(problem, refused.exception.message)

    def test_config_validation_accepts_a_compatible_auto_config(self) -> None:
        loaded = config._config(
            self._repo(
                {
                    "approval_policy": "auto",
                    "worker_attestation_required": True,
                    "human_approval_gate": "trusted",
                }
            )
        )
        self.assertEqual(config._approval_policy(loaded), "auto")


if __name__ == "__main__":
    unittest.main()
