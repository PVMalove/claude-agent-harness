#!/usr/bin/env python3
"""Public seam tests for runtime checkout attestation."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from harness.orchestration.runtime_attestation import AttestationError, attest


def _git(path: Path, *arguments: str) -> str:
    result = subprocess.run(["git", *arguments], cwd=path, check=True, capture_output=True, text=True)
    return result.stdout.strip()


class RuntimeAttestationTests(unittest.TestCase):
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
