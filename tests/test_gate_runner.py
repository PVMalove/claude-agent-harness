#!/usr/bin/env python3
"""Focused public-contract tests for the shared quality-gate runner."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_ROOT = Path(__file__).resolve().parents[1] / "harness" / "gate_runner"
sys.path.insert(0, str(MODULE_ROOT))
from gate_runner import CleanRoomPolicy, LocalPolicy, run_gate


class GateRunnerTests(unittest.TestCase):
    def test_local_and_clean_room_return_the_same_sanitised_evidence_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Gate Runner Test"], cwd=repo, check=True)
            tracked = repo / "candidate.txt"
            tracked.write_text("candidate\n", encoding="utf-8")
            subprocess.run(["git", "add", "candidate.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "test: pin candidate"], cwd=repo, check=True)
            candidate = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
            ).stdout.strip()
            tracked.write_text("dirty checkout\n", encoding="utf-8")

            command = f'{sys.executable} -c "from pathlib import Path; print(Path(\'candidate.txt\').read_text().strip()); print(\'token=visible\')"'
            local = run_gate([command], LocalPolicy(repo), stop_on_failure=True)
            clean_room = run_gate([command], CleanRoomPolicy(repo, candidate), stop_on_failure=False)

        self.assertEqual([set(check) for check in local.checks], [set(check) for check in clean_room.checks])
        self.assertEqual(set(local.checks[0]), {"command", "result", "evidence"})
        self.assertEqual(clean_room.checks[0]["result"], "pass")
        self.assertIn("candidate", clean_room.artifact)
        self.assertNotIn("dirty checkout", clean_room.artifact)
        for result in (local, clean_room):
            self.assertIn("token=<redacted>", result.artifact)
            self.assertNotIn("token=visible", result.artifact)
            self.assertNotIn("token=visible", result.checks[0]["evidence"])

    def test_stops_at_first_failure_when_the_policy_requests_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            failed = f'{sys.executable} -c "import sys; print(\'password=visible\'); sys.exit(7)"'
            skipped = f'{sys.executable} -c "print(\'must not run\')"'
            result = run_gate([failed, skipped], LocalPolicy(root), stop_on_failure=True)

        self.assertEqual(len(result.checks), 1)
        self.assertEqual(result.checks[0]["result"], "fail")
        self.assertIn("password=<redacted>", result.artifact)
        self.assertNotIn("password=visible", result.artifact)


if __name__ == "__main__":
    unittest.main()
