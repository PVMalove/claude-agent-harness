#!/usr/bin/env python3
"""PR-preparation refresh (issue #533): clean rebase of the own issue branch onto a moved target.

Real ledger and real Git (a local bare remote); no mocks.
"""

from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from harness.orchestration import coordinator
from harness.orchestration.core.utils import CoordinatorError, JsonObject
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


class ConflictTests(RefreshFixture):
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


if __name__ == "__main__":
    unittest.main()
