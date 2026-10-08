#!/usr/bin/env python3
"""PR-preparation refresh (issue #533): clean rebase of the own issue branch onto a moved target.

Real ledger and real Git (a local bare remote); no mocks.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

from harness.orchestration import coordinator, operation_access
from harness.orchestration.core import git_utils
from harness.orchestration.workflow import pr_refresh
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.ledger import IntegrationRefreshRecord, LifecycleLedger
from tests.orchestration.test_integration_record import PublishedBranch, _git


class RefreshFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.branch = PublishedBranch()
        self.addCleanup(self.branch.close)
        self.published = self.branch.publish()
        self.prepared = self.branch.prepare()
        self.record_id = self.prepared["integration_record_id"]
        self.worktree = self.branch.fixture.worktree

    def refresh(self, **overrides: object) -> JsonObject:
        values: JsonObject = {
            "record": self.record_id,
            "ticket": None,
            "branch": None,
            "batch": None,
        }
        values.update(overrides)
        return coordinator.integration_refresh(self.branch.args(**values))

    def refresh_files(self) -> list[str]:
        directory = self.branch.records() / "reports" / "integration-refresh"
        if not directory.is_dir():
            return []
        return sorted(path.name for path in directory.glob("*.json"))

    def land(self, name: str, content: str = "landed\n") -> str:
        """Another change lands on the integration ref in the remote."""
        repo = self.branch.repo
        _git(repo, "checkout", "-q", "master")
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content, encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", f"land {name}")
        _git(repo, "push", "origin", "master")
        return _git(repo, "rev-parse", "HEAD")

    def foreign_commit(self, name: str) -> tuple[Path, str]:
        """A clone of the remote that adds a commit to the published issue branch, unpushed."""
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        clone = Path(tmp.name) / "foreign"
        origin = _git(self.branch.repo, "remote", "get-url", "origin")
        _git(
            Path(tmp.name),
            "clone",
            "-q",
            "--branch",
            self.branch.branch,
            origin,
            str(clone),
        )
        _git(clone, "config", "user.email", "other@example.invalid")
        _git(clone, "config", "user.name", "Other")
        (clone / name).write_text("foreign\n", encoding="utf-8")
        _git(clone, "add", "-A")
        _git(clone, "commit", "-m", f"foreign {name}")
        return clone, _git(clone, "rev-parse", "HEAD")

    def remote_tip(self, name: str) -> str:
        return _git(
            self.branch.repo, "ls-remote", "origin", f"refs/heads/{name}"
        ).split()[0]


class UnchangedBaseTests(RefreshFixture):
    def test_unchanged_target_runs_no_rebase_and_writes_nothing(self) -> None:
        before = self.branch.snapshot()
        result = self.refresh()
        self.assertEqual(result["state"], "unchanged")
        self.assertFalse(result["rebased"])
        self.assertEqual(self.branch.snapshot(), before)
        self.assertEqual(self.refresh_files(), [])


class CleanRebaseTests(RefreshFixture):
    def test_clean_rebase_moves_only_the_own_issue_branch_and_records_the_link(
        self,
    ) -> None:
        tip = self.land("landed.txt")
        main_head = _git(self.branch.repo, "rev-parse", "HEAD")

        result = self.refresh()

        self.assertEqual(result["state"], "rebased")
        new = _git(self.worktree, "rev-parse", "HEAD")
        self.assertEqual(result["new_candidate_sha"], new)
        self.assertNotEqual(new, self.published["candidate"])
        self.assertEqual(_git(self.worktree, "rev-parse", "HEAD~1"), tip)
        self.assertEqual(_git(self.branch.repo, "rev-parse", "HEAD"), main_head)
        stored = json.loads(
            (
                self.branch.records()
                / "reports/integration-refresh"
                / f"{result['refresh_id']}.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(stored["previous_candidate_sha"], self.published["candidate"])
        self.assertEqual(stored["new_candidate_sha"], new)
        self.assertEqual(stored["target_sha"], tip)
        self.assertEqual(stored["history"]["commits"], [new])
        self.assertEqual(
            stored["history"]["previous_commits"], [self.published["candidate"]]
        )
        self.assertEqual(stored["resolver"], {"invoked": False, "cycles_spent": 0})
        self.assertFalse(stored["evidence"]["confirms_new_candidate"])
        self.assertFalse(stored["re_review_required"])

    def test_refresh_is_idempotent_once_the_pair_is_current(self) -> None:
        self.land("landed.txt")
        first = self.refresh()
        again = self.refresh()
        self.assertEqual(again["state"], "unchanged")
        self.assertEqual(again["candidate_sha"], first["new_candidate_sha"])
        self.assertEqual(len(self.refresh_files()), 1)

    def test_a_moving_target_chains_refreshes_from_the_latest_candidate(self) -> None:
        first_tip = self.land("one.txt")
        first = self.refresh()
        second_tip = self.land("two.txt")
        second = self.refresh()
        self.assertEqual(second["state"], "rebased")
        self.assertEqual(second["candidate_sha"], first["new_candidate_sha"])
        self.assertEqual(second["target_sha"], first_tip)
        self.assertEqual(second["integration_tip"], second_tip)
        self.assertEqual(len(self.refresh_files()), 2)

    def test_cyclic_refresh_chain_finishes_under_ledger_lock(self) -> None:
        tip = self.land("landed.txt")
        first = self.refresh()
        # Model a malformed record that closes the chain back to its original pair.
        members: JsonObject = {
            "integration_record_id": self.record_id,
            "previous_candidate_sha": first["new_candidate_sha"],
            "previous_target_sha": tip,
            "new_candidate_sha": self.published["candidate"],
            "target_sha": self.prepared["target_sha"],
        }
        closing_id = IntegrationRefreshRecord.derive_id(members)
        LifecycleLedger(self.branch.state_root()).write_record(
            IntegrationRefreshRecord.from_dict({**members, "refresh_id": closing_id})
        )
        before = self.branch.snapshot()
        command = [
            sys.executable,
            str(Path(coordinator.__file__).resolve()),
            "--repo",
            str(self.branch.repo),
            "--state-dir",
            str(self.branch.fixture.state_dir),
            "integration",
            "status",
            "--record",
            self.record_id,
        ]

        # A second call also proves the first operation released the ledger lock.
        for _ in range(2):
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            status = json.loads(result.stdout)
            self.assertEqual(
                [item["refresh_id"] for item in status["refreshes"]],
                [first["refresh_id"], closing_id],
            )
            self.assertEqual(status["candidate_sha"], self.published["candidate"])
            self.assertEqual(status["target_sha"], self.prepared["target_sha"])
            self.assertEqual(status["state"], "stale")
        self.assertEqual(self.branch.snapshot(), before)

    def test_the_public_cli_group_reaches_refresh(self) -> None:
        parsed = coordinator.parser().parse_args(
            [
                "--repo",
                str(self.branch.repo),
                "--state-dir",
                str(self.branch.fixture.state_dir),
                "integration",
                "refresh",
                "--record",
                self.record_id,
            ]
        )
        self.assertIs(parsed.handler, coordinator.integration_refresh)


class RecoveryTests(RefreshFixture):
    def interrupted_refresh(self) -> str:
        """The rewrite reaches the remote, then recording the refresh fails."""
        self.land("landed.txt")
        with mock.patch.object(
            pr_refresh, "_write_record", side_effect=OSError("disk full")
        ):
            with self.assertRaises(OSError):
                self.refresh()
        self.assertEqual(self.refresh_files(), [])
        pushed = self.remote_tip(self.branch.branch)
        self.assertNotEqual(pushed, self.published["candidate"])
        return pushed

    def test_rerun_after_push_before_record_writes_only_the_missing_record(
        self,
    ) -> None:
        pushed = self.interrupted_refresh()

        result = self.refresh()

        self.assertEqual(result["state"], "recovered")
        self.assertEqual(result["new_candidate_sha"], pushed)
        self.assertEqual(self.remote_tip(self.branch.branch), pushed)
        self.assertEqual(_git(self.worktree, "rev-parse", "HEAD"), pushed)
        self.assertEqual(len(self.refresh_files()), 1)
        self.assertEqual(self.refresh()["state"], "unchanged")

    def test_recovery_also_finishes_a_detached_checkout(self) -> None:
        pushed = self.interrupted_refresh()
        _git(self.worktree, "checkout", "-q", "--detach", self.published["candidate"])

        result = self.refresh()

        self.assertEqual(result["state"], "recovered")
        self.assertEqual(
            _git(self.worktree, "rev-parse", "--abbrev-ref", "HEAD"),
            self.branch.branch,
        )
        self.assertEqual(_git(self.worktree, "rev-parse", "HEAD"), pushed)

    def test_a_foreign_commit_on_top_of_the_rewrite_is_still_refused(self) -> None:
        self.interrupted_refresh()
        clone, _ = self.foreign_commit("foreign.txt")
        _git(clone, "fetch", "-q", "origin", self.branch.branch)
        _git(clone, "reset", "-q", "--hard", "FETCH_HEAD")
        (clone / "foreign.txt").write_text("foreign\n", encoding="utf-8")
        _git(clone, "add", "-A")
        _git(clone, "commit", "-m", "foreign on top")
        _git(clone, "push", "origin", f"HEAD:refs/heads/{self.branch.branch}")
        foreign_tip = self.remote_tip(self.branch.branch)

        with self.assertRaisesRegex(CoordinatorError, "foreign commits"):
            self.refresh()

        self.assertEqual(self.remote_tip(self.branch.branch), foreign_tip)
        self.assertEqual(self.refresh_files(), [])


class ConflictTests(RefreshFixture):
    def test_rebase_failure_without_conflicts_restores_worktree_and_raises(
        self,
    ) -> None:
        self.land("landed.txt")
        # Git starts the rebase but cannot create its rewritten commit. No GPG
        # installation is needed: the configured signing executable does not exist.
        _git(self.worktree, "config", "gpg.format", "openpgp")
        _git(
            self.worktree,
            "config",
            "gpg.program",
            str(self.branch.fixture.state_dir / "missing-gpg"),
        )
        _git(self.worktree, "config", "user.signingkey", "test-key")
        _git(self.worktree, "config", "commit.gpgSign", "true")
        before = self.branch.snapshot()

        with self.assertRaisesRegex(
            CoordinatorError,
            "the rebase failed without conflicting files; the worktree was restored",
        ) as raised:
            self.refresh()

        self.assertIn("retry the refresh", raised.exception.remedy)
        self.assertEqual(
            _git(self.worktree, "rev-parse", "--abbrev-ref", "HEAD"),
            self.branch.branch,
        )
        self.assertEqual(self.branch.snapshot(), before)
        self.assertEqual(self.refresh_files(), [])
        for marker in ("rebase-merge", "rebase-apply", "MERGE_HEAD"):
            self.assertFalse(
                (
                    self.worktree
                    / _git(self.worktree, "rev-parse", "--git-path", marker)
                ).exists(),
                marker,
            )

        # The failed operation released its lock and left a usable checkout.
        _git(self.worktree, "config", "commit.gpgSign", "false")
        self.assertEqual(self.refresh()["state"], "rebased")

    def test_textual_conflict_returns_resolver_data_and_loses_nothing(self) -> None:
        self.land("services/x.py", "VALUE = 'upstream'\n")
        main_head = _git(self.branch.repo, "rev-parse", "HEAD")
        remote_before = self.remote_tip(self.branch.branch)
        before = self.branch.snapshot()

        result = self.refresh()

        self.assertEqual(result["state"], "conflict")
        self.assertFalse(result["rebased"])
        resolver = result["resolver"]
        self.assertTrue(resolver["required"])
        self.assertEqual(resolver["cycles_spent"], 0)
        self.assertEqual(resolver["conflicting_files"], ["services/x.py"])
        self.assertEqual(resolver["candidate_sha"], self.published["candidate"])
        self.assertEqual(resolver["worktree"], str(self.worktree))
        self.assertEqual(
            _git(self.worktree, "rev-parse", "HEAD"), self.published["candidate"]
        )
        self.assertEqual(
            _git(self.worktree, "rev-parse", "--abbrev-ref", "HEAD"),
            self.branch.branch,
        )
        self.assertEqual(_git(self.worktree, "status", "--porcelain"), "")
        self.assertEqual(self.remote_tip(self.branch.branch), remote_before)
        self.assertEqual(_git(self.branch.repo, "rev-parse", "HEAD"), main_head)
        self.assertEqual(self.refresh_files(), [])
        self.assertEqual(self.branch.snapshot()["ledger_files"], before["ledger_files"])


class GuardTests(RefreshFixture):
    def test_dirty_worktree_is_never_touched(self) -> None:
        self.land("landed.txt")
        (self.worktree / "services" / "x.py").write_text(
            "unfinished\n", encoding="utf-8"
        )
        (self.worktree / "scratch.txt").write_text("notes\n", encoding="utf-8")
        remote_before = self.remote_tip(self.branch.branch)

        with self.assertRaisesRegex(CoordinatorError, "uncommitted"):
            self.refresh()

        self.assertEqual(
            (self.worktree / "services" / "x.py").read_text(encoding="utf-8"),
            "unfinished\n",
        )
        self.assertTrue((self.worktree / "scratch.txt").is_file())
        self.assertEqual(
            _git(self.worktree, "rev-parse", "HEAD"), self.published["candidate"]
        )
        self.assertEqual(self.remote_tip(self.branch.branch), remote_before)
        self.assertEqual(self.refresh_files(), [])

    def test_a_changed_remote_branch_is_never_overwritten(self) -> None:
        self.land("landed.txt")
        clone, foreign = self.foreign_commit("foreign.txt")
        _git(clone, "push", "origin", f"HEAD:refs/heads/{self.branch.branch}")

        with self.assertRaisesRegex(CoordinatorError, "foreign commits"):
            self.refresh()

        self.assertEqual(self.remote_tip(self.branch.branch), foreign)
        self.assertEqual(
            _git(self.worktree, "rev-parse", "HEAD"), self.published["candidate"]
        )
        self.assertEqual(self.refresh_files(), [])

    def test_a_remote_change_during_the_push_fails_the_lease(self) -> None:
        self.land("landed.txt")
        clone, foreign = self.foreign_commit("foreign.txt")
        hook = self.branch.repo / ".git" / "hooks" / "pre-push"
        hook.write_text(
            "#!/bin/sh\nunset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE\n"
            f"git -C '{clone}' push -q origin HEAD:refs/heads/{self.branch.branch}\n",
            encoding="utf-8",
        )
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR)

        with self.assertRaisesRegex(CoordinatorError, "changed or refused"):
            self.refresh()
        hook.unlink()

        self.assertEqual(self.remote_tip(self.branch.branch), foreign)
        self.assertEqual(
            _git(self.worktree, "rev-parse", "HEAD"), self.published["candidate"]
        )
        self.assertEqual(
            _git(self.worktree, "rev-parse", "--abbrev-ref", "HEAD"),
            self.branch.branch,
        )
        self.assertEqual(self.refresh_files(), [])

    def test_only_the_own_issue_branch_is_written_on_the_remote(self) -> None:
        tip = self.land("landed.txt")
        before = _git(self.branch.repo, "ls-remote", "--heads", "origin").splitlines()

        result = self.refresh()

        after = _git(self.branch.repo, "ls-remote", "--heads", "origin").splitlines()
        changed = sorted(set(before) ^ set(after))
        self.assertEqual(
            [line.split("\t")[1] for line in changed],
            [f"refs/heads/{self.branch.branch}"] * 2,
        )
        self.assertEqual(
            self.remote_tip(self.branch.branch), result["new_candidate_sha"]
        )
        self.assertEqual(self.remote_tip("master"), tip)


@unittest.skipIf(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    "a privileged process is not denied by file permissions",
)
class AccessRefusalTests(RefreshFixture):
    """The environment refuses what refresh needs: nothing may change and the cause is named."""

    @contextlib.contextmanager
    def read_only(self, path: Path, *, tree: bool = False) -> Iterator[None]:
        paths = [path, *path.rglob("*")] if tree else [path]
        modes = {item: item.stat().st_mode & 0o7777 for item in paths}
        for item in paths:
            item.chmod(modes[item] & ~0o222)
        try:
            yield
        finally:
            for item in paths:
                item.chmod(modes[item])

    def authored_inherit_policy(self) -> None:
        (self.branch.repo / ".harness/orchestration.json").write_text(
            json.dumps({"access_policy": {"defaults": {"mode": "inherit"}}}),
            encoding="utf-8",
        )

    def test_a_refused_git_metadata_write_stops_refresh_before_any_change(
        self,
    ) -> None:
        self.land("landed.txt")
        self.authored_inherit_policy()
        before = self.branch.snapshot()

        with self.read_only(self.branch.repo / ".git"):
            with self.assertRaises(operation_access.OperationAccessError) as raised:
                self.refresh()

        evidence = raised.exception.evidence
        self.assertEqual((evidence["operation"], evidence["status"]), ("git", "denied"))
        self.assertIn("shared Git metadata", raised.exception.message)
        self.assertEqual(self.branch.snapshot(), before)
        self.assertEqual(self.refresh_files(), [])
        self.assertEqual(self.refresh()["state"], "rebased")

    def test_a_refused_worktree_metadata_write_is_a_classified_git_error(self) -> None:
        self.land("landed.txt")
        before = self.branch.snapshot()
        admin = Path(_git(self.worktree, "rev-parse", "--absolute-git-dir"))

        with self.read_only(admin):
            with self.assertRaises(git_utils.GitAccessError) as raised:
                self.refresh()

        self.assertEqual(raised.exception.category, git_utils.METADATA_DENIED)
        self.assertEqual(self.branch.snapshot(), before)
        self.assertEqual(self.refresh_files(), [])

    def test_a_rebase_the_environment_refused_is_not_reported_as_a_conflict(
        self,
    ) -> None:
        self.land("landed.txt")
        hook = self.branch.repo / ".git" / "hooks" / "pre-rebase"
        hook.write_text(
            "#!/bin/sh\necho \"fatal: Unable to create '/r/.git/index.lock': "
            'Permission denied" >&2\nexit 1\n',
            encoding="utf-8",
        )
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
        before = self.branch.snapshot()

        with self.assertRaises(git_utils.GitAccessError) as raised:
            self.refresh()
        hook.unlink()

        self.assertEqual(raised.exception.category, git_utils.METADATA_DENIED)
        self.assertEqual(
            _git(self.worktree, "rev-parse", "--abbrev-ref", "HEAD"),
            self.branch.branch,
        )
        self.assertEqual(self.branch.snapshot(), before)
        self.assertEqual(self.refresh()["state"], "rebased")

    def test_a_conflict_whose_commit_subject_mentions_a_denial_is_not_an_access_failure(
        self,
    ) -> None:
        rebase = subprocess.CompletedProcess(
            ["git", "rebase"],
            1,
            stdout=(
                "Auto-merging x.py\nCONFLICT (content): Merge conflict in x.py\n"
                "error: could not apply 1a2b3c4... fix: handle permission denied on metadata write\n"
            ),
            stderr="hint: Resolve all conflicts manually\n",
        )

        self.assertIsNone(
            pr_refresh._rebase_access_failure(self.worktree, self.branch.branch, rebase)
        )

    def test_a_remote_that_refuses_the_rewrite_is_classified_and_leaves_the_branch(
        self,
    ) -> None:
        self.land("landed.txt")
        remote = Path(_git(self.branch.repo, "remote", "get-url", "origin"))
        before = self.branch.snapshot()

        with self.read_only(remote, tree=True):
            with self.assertRaises(git_utils.GitAccessError) as raised:
                self.refresh()

        self.assertEqual(raised.exception.category, git_utils.REMOTE_DENIED)
        self.assertEqual(
            _git(self.worktree, "rev-parse", "HEAD"), self.published["candidate"]
        )
        self.assertEqual(
            _git(self.worktree, "rev-parse", "--abbrev-ref", "HEAD"),
            self.branch.branch,
        )
        self.assertEqual(self.branch.snapshot(), before)
        self.assertEqual(self.refresh_files(), [])
        self.assertEqual(self.refresh()["state"], "rebased")


if __name__ == "__main__":
    unittest.main()
