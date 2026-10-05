#!/usr/bin/env python3
"""PR-preparation refresh (issue #533): clean rebase of the own issue branch onto a moved target.

Real ledger and real Git (a local bare remote); no mocks.
"""

from __future__ import annotations

import json
import unittest

from harness.orchestration import coordinator
from harness.orchestration.core.utils import JsonObject
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
        (repo / name).write_text(content, encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", f"land {name}")
        _git(repo, "push", "origin", "master")
        return _git(repo, "rev-parse", "HEAD")

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


if __name__ == "__main__":
    unittest.main()
