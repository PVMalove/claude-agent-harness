#!/usr/bin/env python3
"""Public seam tests for runtime checkout attestation."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.orchestration import runtime_attestation
from harness.orchestration.runtime_attestation import AttestationError, attest


def _git(path: Path, *arguments: str) -> str:
    result = subprocess.run(["git", *arguments], cwd=path, check=True, capture_output=True, text=True)
    return result.stdout.strip()


class RuntimeAttestationTests(unittest.TestCase):
    def test_write_role_accepts_windows_git_output_without_forcing_utf8(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            _git(repo, "init", "-q")
            _git(repo, "config", "user.email", "test@example.invalid")
            _git(repo, "config", "user.name", "Attestation Test")
            (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
            _git(repo, "add", "tracked.txt")
            _git(repo, "commit", "-qm", "test: base")
            snapshot = _git(repo, "rev-parse", "HEAD")
            branch = "feature/issue-240-attested"
            _git(repo, "branch", branch)
            worktree = Path(temporary) / "issue-240"
            _git(repo, "worktree", "add", "-q", str(worktree), branch)
            dispatch = {"role": "developer", "branch": branch, "candidate_commit": None, "snapshot_commit": snapshot}
            real_run = runtime_attestation.subprocess.run

            def windows_git_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
                command = args[0]
                if (
                    isinstance(command, list)
                    and command[-3:] == ["worktree", "list", "--porcelain"]
                    and kwargs.get("encoding") == "utf-8"
                ):
                    raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
                return real_run(*args, **kwargs)  # type: ignore[arg-type, no-any-return]

            with patch.object(runtime_attestation.subprocess, "run", side_effect=windows_git_run):
                proof = attest(repo, dispatch, str(worktree))

            self.assertEqual(proof["worktree"], str(worktree.resolve()))

    def test_write_role_requires_the_registered_issue_worktree(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            _git(repo, "init", "-q")
            _git(repo, "config", "user.email", "test@example.invalid")
            _git(repo, "config", "user.name", "Attestation Test")
            (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
            _git(repo, "add", "tracked.txt")
            _git(repo, "commit", "-qm", "test: base")
            snapshot = _git(repo, "rev-parse", "HEAD")
            branch = "feature/issue-1-attested"
            _git(repo, "branch", branch)
            worktree = Path(temporary) / "issue-1"
            _git(repo, "worktree", "add", "-q", str(worktree), branch)
            dispatch = {"role": "developer", "branch": branch, "candidate_commit": None, "snapshot_commit": snapshot}

            proof = attest(repo, dispatch, str(worktree))

            self.assertEqual(proof["worktree"], str(worktree.resolve()))
            self.assertEqual(proof["branch"], branch)
            (worktree / "nested").mkdir()
            with self.assertRaisesRegex(AttestationError, "not registered"):
                attest(repo, dispatch, str(worktree / "nested"))

    def test_write_role_rejects_a_missing_or_non_string_issue_branch(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            _git(repo, "init", "-q")
            _git(repo, "config", "user.email", "test@example.invalid")
            _git(repo, "config", "user.name", "Attestation Test")
            (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
            _git(repo, "add", "tracked.txt")
            _git(repo, "commit", "-qm", "test: base")
            snapshot = _git(repo, "rev-parse", "HEAD")
            branch = "feature/issue-227-attested"
            _git(repo, "branch", branch)
            worktree = Path(temporary) / "issue-227"
            _git(repo, "worktree", "add", "-q", str(worktree), branch)

            for pinned in ({}, {"branch": None}, {"branch": 7}, {"branch": "feature/issue-other"}):
                with self.subTest(pinned=pinned):
                    dispatch = {"role": "developer", "snapshot_commit": snapshot, **pinned}
                    with self.assertRaises(AttestationError) as raised:
                        attest(repo, dispatch, str(worktree))
                    self.assertIn("does not match the immutable issue branch", str(raised.exception))
                    if isinstance(dispatch.get("branch", None), str):
                        self.assertIn("checkout branch", raised.exception.remedy)
                    else:
                        self.assertIn("pin a string issue branch", raised.exception.remedy)

    def test_review_role_requires_the_pinned_candidate(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            _git(repo, "init", "-q")
            _git(repo, "config", "user.email", "test@example.invalid")
            _git(repo, "config", "user.name", "Attestation Test")
            (repo / "tracked.txt").write_text("candidate\n", encoding="utf-8")
            _git(repo, "add", "tracked.txt")
            _git(repo, "commit", "-qm", "test: candidate")
            candidate = _git(repo, "rev-parse", "HEAD")

            proof = attest(repo, {"role": "code-review", "branch": "feature/issue-2-review", "candidate_commit": candidate, "snapshot_commit": candidate}, str(repo))

            self.assertEqual(proof["head_commit"], candidate)


if __name__ == "__main__":
    unittest.main()
