#!/usr/bin/env python3
"""Public seam tests for runtime checkout attestation."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Protocol, cast
from unittest.mock import patch

import pytest

from harness.orchestration.runtime_attestation import AttestationError, attest


class _RunFn(Protocol):
    def __call__(
        self, command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]: ...


def _git(path: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=path, check=True, capture_output=True, text=True
    )
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
            dispatch = {
                "role": "developer",
                "branch": branch,
                "candidate_commit": None,
                "snapshot_commit": snapshot,
            }
            real_run = cast(_RunFn, subprocess.run)

            def windows_git_run(
                command: list[str], **kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                if (
                    command[-3:] == ["worktree", "list", "--porcelain"]
                    and kwargs.get("encoding") == "utf-8"
                ):
                    raise UnicodeDecodeError(
                        "utf-8", b"\xff", 0, 1, "invalid start byte"
                    )
                return real_run(command, **kwargs)

            with patch.object(subprocess, "run", side_effect=windows_git_run):
                proof = attest(repo, dispatch, str(worktree))

            self.assertEqual(proof["worktree"], str(worktree.resolve()))

    def test_attestation_trusts_only_its_registered_git_paths_in_a_sandbox(
        self,
    ) -> None:
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
            branch = "feature/issue-314-sandbox"
            _git(repo, "branch", branch)
            worktree = Path(temporary) / "issue-314"
            _git(repo, "worktree", "add", "-q", str(worktree), branch)
            real_run = cast(_RunFn, subprocess.run)

            def sandbox_git(
                command: list[str], **kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                git_path = Path(command[command.index("-C") + 1]).resolve()
                if f"safe.directory={git_path}" not in command:
                    return subprocess.CompletedProcess(
                        command, 128, "", "fatal: detected dubious ownership"
                    )
                return real_run(command, **kwargs)

            with patch.object(subprocess, "run", side_effect=sandbox_git):
                proof = attest(
                    repo,
                    {
                        "role": "developer",
                        "branch": branch,
                        "snapshot_commit": snapshot,
                    },
                    str(worktree),
                )

            self.assertEqual(proof["worktree"], str(worktree.resolve()))

    def test_attestation_surfaces_undecodable_git_error_as_failure(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()

            def undecodable_git(
                command: list[str], **kwargs: object
            ) -> subprocess.CompletedProcess[str]:
                if kwargs.get("errors") != "replace":
                    raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte")
                return subprocess.CompletedProcess(
                    command, 128, "", "fatal: detected dubious ownership �"
                )

            with patch.object(subprocess, "run", side_effect=undecodable_git):
                with self.assertRaisesRegex(AttestationError, "dubious ownership"):
                    attest(repo, {"role": "developer"}, str(repo))

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
            dispatch = {
                "role": "developer",
                "branch": branch,
                "candidate_commit": None,
                "snapshot_commit": snapshot,
            }

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

            for pinned in (
                {},
                {"branch": None},
                {"branch": 7},
                {"branch": "feature/issue-other"},
            ):
                with self.subTest(pinned=pinned):
                    dispatch = {
                        "role": "developer",
                        "snapshot_commit": snapshot,
                        **pinned,
                    }
                    with self.assertRaises(AttestationError) as raised:
                        attest(repo, dispatch, str(worktree))
                    self.assertIn(
                        "does not match the immutable issue branch",
                        str(raised.exception),
                    )
                    if isinstance(dispatch.get("branch", None), str):
                        self.assertIn("checkout branch", raised.exception.remedy)
                    else:
                        self.assertIn(
                            "pin a string issue branch", raised.exception.remedy
                        )

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

            proof = attest(
                repo,
                {
                    "role": "code-review",
                    "branch": "feature/issue-2-review",
                    "candidate_commit": candidate,
                    "snapshot_commit": candidate,
                },
                str(repo),
            )

            self.assertEqual(proof["head_commit"], candidate)


def _developer_worktree(root: Path) -> tuple[Path, Path, dict[str, object]]:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Attestation Test")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "test: base")
    branch = "feature/issue-1-bounded"
    _git(repo, "branch", branch)
    worktree = root / "issue-1"
    _git(repo, "worktree", "add", "-q", str(worktree), branch)
    snapshot = _git(repo, "rev-parse", "HEAD")
    return (
        repo,
        worktree,
        {"role": "developer", "branch": branch, "snapshot_commit": snapshot},
    )


def test_every_attestation_git_call_is_bounded(tmp_path: Path) -> None:
    repo, worktree, dispatch = _developer_worktree(tmp_path)
    real_run = cast(_RunFn, subprocess.run)
    timeouts: list[object] = []

    def bounded(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        timeouts.append(kwargs.get("timeout"))
        return real_run(command, **kwargs)

    with patch.object(subprocess, "run", side_effect=bounded):
        attest(repo, dispatch, str(worktree))

    assert timeouts
    assert all(isinstance(value, int) and value > 0 for value in timeouts)


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired(["git"], 60),
        FileNotFoundError(2, "No such file or directory", "git"),
    ],
)
def test_a_git_probe_that_hangs_or_cannot_start_is_an_attestation_error(
    tmp_path: Path, failure: Exception
) -> None:
    with (
        patch.object(subprocess, "run", side_effect=failure),
        pytest.raises(AttestationError) as caught,
    ):
        attest(tmp_path, {"role": "developer"}, str(tmp_path))

    assert caught.value.__cause__ is failure
    assert caught.value.remedy.strip()


def test_an_ancestry_check_that_hangs_is_an_attestation_error(tmp_path: Path) -> None:
    repo, worktree, dispatch = _developer_worktree(tmp_path)
    real_run = cast(_RunFn, subprocess.run)

    def hanging(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if "merge-base" in command:
            raise subprocess.TimeoutExpired(command, 60)
        return real_run(command, **kwargs)

    with (
        patch.object(subprocess, "run", side_effect=hanging),
        pytest.raises(AttestationError) as caught,
    ):
        attest(repo, dispatch, str(worktree))

    assert isinstance(caught.value.__cause__, subprocess.TimeoutExpired)
    assert caught.value.remedy.strip()


if __name__ == "__main__":
    unittest.main()
