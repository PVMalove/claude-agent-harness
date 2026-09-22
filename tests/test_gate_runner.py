#!/usr/bin/env python3
"""Focused public-contract tests for the shared quality-gate runner."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import os
from pathlib import Path
from typing import Protocol, cast
from unittest import mock

from harness.errors import HarnessError
from harness.gate_runner.gate_runner import (
    CleanRoomPolicy,
    GateRunnerError,
    LocalPolicy,
    run_gate,
)


class _RunFn(Protocol):
    def __call__(
        self, command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]: ...


class GateRunnerTests(unittest.TestCase):
    def test_clean_room_python_command_ignores_a_broken_path_launcher(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Gate Runner Test"],
                cwd=repo,
                check=True,
            )
            (repo / "check.py").write_text("print('valid interpreter')\n", encoding="utf-8")
            subprocess.run(["git", "add", "check.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "test: pin candidate"], cwd=repo, check=True)
            candidate = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            broken_bin = root / "broken-bin"
            broken_bin.mkdir()
            broken_python = broken_bin / "python"
            broken_python.write_text("#!/bin/sh\nexit 73\n", encoding="utf-8")
            broken_python.chmod(0o755)
            original_path = os.environ["PATH"]
            self.addCleanup(os.environ.__setitem__, "PATH", original_path)
            os.environ["PATH"] = f"{broken_bin}{os.pathsep}{original_path}"

            result = run_gate(
                [["python", "check.py"]],
                CleanRoomPolicy(repo, candidate),
                stop_on_failure=True,
            )
            string_result = run_gate(
                ["python check.py"],
                CleanRoomPolicy(repo, candidate),
                stop_on_failure=True,
            )

        self.assertEqual(result.checks[0]["result"], "pass")
        self.assertIn("valid interpreter", result.artifact)
        self.assertNotIn("$ python check.py", result.artifact)
        self.assertEqual(string_result.checks[0]["result"], "pass")
        self.assertNotIn("$ python check.py", string_result.artifact)

    def test_local_and_clean_room_return_the_same_sanitised_evidence_shape(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Gate Runner Test"], cwd=repo, check=True
            )
            tracked = repo / "candidate.txt"
            tracked.write_text("candidate\n", encoding="utf-8")
            subprocess.run(["git", "add", "candidate.txt"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "test: pin candidate"], cwd=repo, check=True
            )
            candidate = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            tracked.write_text("dirty checkout\n", encoding="utf-8")

            command = f"{sys.executable} -c \"from pathlib import Path; print(Path('candidate.txt').read_text().strip()); print('token=visible')\""
            local = run_gate([command], LocalPolicy(repo), stop_on_failure=True)
            clean_room = run_gate(
                [command], CleanRoomPolicy(repo, candidate), stop_on_failure=False
            )

        self.assertEqual(
            [set(check) for check in local.checks],
            [set(check) for check in clean_room.checks],
        )
        self.assertEqual(set(local.checks[0]), {"command", "result", "evidence"})
        self.assertEqual(clean_room.checks[0]["result"], "pass")
        self.assertIn("candidate", clean_room.artifact)
        self.assertNotIn("dirty checkout", clean_room.artifact)
        for result in (local, clean_room):
            self.assertIn("token=<redacted>", result.artifact)
            self.assertNotIn("token=visible", result.artifact)
            self.assertNotIn("token=visible", result.checks[0]["evidence"])

    def test_stops_at_first_failure_when_the_policy_requests_it(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            failed = f"{sys.executable} -c \"import sys; print('password=visible'); sys.exit(7)\""
            skipped = f"{sys.executable} -c \"print('must not run')\""
            result = run_gate(
                [failed, skipped], LocalPolicy(root), stop_on_failure=True
            )

        self.assertEqual(len(result.checks), 1)
        self.assertEqual(result.checks[0]["result"], "fail")
        self.assertIn("password=<redacted>", result.artifact)
        self.assertNotIn("password=visible", result.artifact)

    def test_clean_room_failures_raise_a_harness_error_with_message_and_remedy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Gate Runner Test"], cwd=repo, check=True
            )
            (repo / "candidate.txt").write_text("candidate\n", encoding="utf-8")
            subprocess.run(["git", "add", "candidate.txt"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "test: pin candidate"], cwd=repo, check=True
            )
            candidate_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            unknown_commit = "0" * 40
            abbreviated_commit = candidate_commit[:12]

            for candidate, expected_message in (
                (unknown_commit, "could not create clean QA worktree"),
                (abbreviated_commit, "does not match the pinned candidate commit"),
            ):
                with self.subTest(candidate=candidate):
                    with self.assertRaises(GateRunnerError) as raised:
                        run_gate(
                            ["true"],
                            CleanRoomPolicy(repo, candidate),
                            stop_on_failure=True,
                        )
                    self.assertIsInstance(raised.exception, HarnessError)
                    self.assertIn(expected_message, raised.exception.message)
                    self.assertIn(candidate, raised.exception.remedy)

    def test_dirty_clean_room_worktree_raises_with_a_status_specific_remedy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Gate Runner Test"], cwd=repo, check=True
            )
            (repo / "candidate.txt").write_text("candidate\n", encoding="utf-8")
            subprocess.run(["git", "add", "candidate.txt"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "test: pin candidate"], cwd=repo, check=True
            )
            candidate_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            real_run = cast(_RunFn, subprocess.run)

            def fake_status(status_result: subprocess.CompletedProcess[str]) -> _RunFn:
                def run(
                    command: list[str], **kwargs: object
                ) -> subprocess.CompletedProcess[str]:
                    if "status" in command:
                        return status_result
                    return real_run(command, **kwargs)

                return run

            for status_result, expected_remedy in (
                (
                    subprocess.CompletedProcess([], 128, stdout="", stderr="fatal"),
                    "inspect the 'git status' error",
                ),
                (
                    subprocess.CompletedProcess(
                        [], 0, stdout="?? stray.txt\n", stderr=""
                    ),
                    "internal invariant violated",
                ),
            ):
                with self.subTest(returncode=status_result.returncode):
                    with (
                        mock.patch(
                            "subprocess.run", side_effect=fake_status(status_result)
                        ),
                        self.assertRaises(GateRunnerError) as raised,
                    ):
                        run_gate(
                            ["true"],
                            CleanRoomPolicy(repo, candidate_commit),
                            stop_on_failure=True,
                        )
                    self.assertIn("contains mutable files", raised.exception.message)
                    self.assertIn(expected_remedy, raised.exception.remedy)


if __name__ == "__main__":
    unittest.main()
