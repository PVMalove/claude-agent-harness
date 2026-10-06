"""'integration status' with collected CI evidence (issue #535): the QA replacement block."""

from __future__ import annotations

import unittest

from harness.orchestration import coordinator
from harness.orchestration.core.ci_source import CheckRun, CiObservation
from harness.orchestration.core.utils import JsonObject
from tests.orchestration.test_ci_collect import (
    GITHUB,
    MERGE,
    PR,
    REPO,
    FakeSource,
    write_project,
)
from tests.orchestration.test_integration_record import PublishedBranch


class CiStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.branch = PublishedBranch()
        self.addCleanup(self.branch.close)
        self.published = self.branch.publish()
        self.prepared = self.branch.prepare()
        self.record_id = self.prepared["integration_record_id"]
        write_project(self.branch.repo, GITHUB, ["lint"])

    def status(self) -> JsonObject:
        return coordinator.integration_status(
            self.branch.args(
                record=self.record_id, ticket=None, branch=None, batch=None
            )
        )

    def collect(self, candidate: str, target: str) -> JsonObject:
        observation = CiObservation(
            repository=REPO,
            pull_request=PR,
            base_ref="master",
            candidate_sha=candidate,
            checkout="combined",
            merge_commit_sha=MERGE,
            merge_parents=(candidate, target),
            checks=(
                CheckRun(
                    "lint",
                    MERGE,
                    "completed",
                    "success",
                    "10",
                    f"https://github.com/{REPO}/runs/10",
                ),
            ),
        )
        return coordinator.integration_collect_ci(
            self.branch.args(
                record=self.record_id,
                ticket=None,
                branch=None,
                batch=None,
                pull_request=PR,
                ci_source=FakeSource(observation),
            )
        )

    def link_manual_ci(self, candidate: str, target: str) -> None:
        coordinator.integration_link_evidence(
            self.branch.args(
                record=self.record_id,
                kind="ci",
                result="passed",
                reference="manual-run",
                artifact_sha256=None,
                candidate_commit=candidate,
                target_commit=target,
            )
        )

    def test_without_ci_the_replacement_does_not_apply(self) -> None:
        block = self.status()["qa_replacement"]
        self.assertFalse(block["applies"])
        self.assertEqual(block["reason"], "no_collected_ci")
        self.assertTrue(self.status()["verification"]["accepted_kinds"])

    def test_collected_ci_replaces_qa_for_the_verified_pair_and_shows_identity(
        self,
    ) -> None:
        candidate, target = self.prepared["candidate_sha"], self.prepared["target_sha"]
        self.collect(candidate, target)

        status = self.status()

        block = status["qa_replacement"]
        self.assertTrue(block["applies"])
        self.assertFalse(block["re_refresh_required"])
        self.assertEqual(block["candidate_sha"], candidate)
        self.assertEqual(block["target_sha"], target)
        self.assertEqual(block["merge_commit_sha"], MERGE)
        self.assertEqual(block["source"], "github")
        self.assertEqual(block["repository"], REPO)
        self.assertEqual(block["pull_request"], PR)
        self.assertEqual(block["checks"][0]["run_id"], "10")
        self.assertEqual(
            status["source_evidence"]["qa"]["outcome"],
            self.prepared["source_evidence"]["qa"]["outcome"],
        )

    def test_manually_linked_ci_never_satisfies_or_replaces(self) -> None:
        candidate, target = self.prepared["candidate_sha"], self.prepared["target_sha"]
        self.link_manual_ci(candidate, target)
        self.assertFalse(self.status()["qa_replacement"]["applies"])

    def test_integration_movement_after_ci_makes_it_unverified(self) -> None:
        candidate, target = self.prepared["candidate_sha"], self.prepared["target_sha"]
        self.collect(candidate, target)
        moved = self.branch.advance_integration_ref()

        status = self.status()

        block = status["qa_replacement"]
        self.assertFalse(block["applies"])
        self.assertTrue(block["re_refresh_required"])
        self.assertEqual(block["reason"], "integration_moved")
        self.assertEqual(block["target_sha"], target)
        self.assertEqual(status["integration_tip"], moved)
        self.assertTrue(status["refresh_required"])

    def test_after_a_refresh_only_collected_ci_of_the_new_pair_satisfies_verification(
        self,
    ) -> None:
        moved = self.branch.advance_integration_ref()
        refreshed = coordinator.integration_refresh(
            self.branch.args(
                record=self.record_id, ticket=None, branch=None, batch=None
            )
        )
        new = refreshed["new_candidate_sha"]
        self.link_manual_ci(new, moved)
        self.assertFalse(self.status()["verification"]["satisfied"])
        self.collect(self.prepared["candidate_sha"], self.prepared["target_sha"])
        self.assertFalse(self.status()["verification"]["satisfied"])

        self.collect(new, moved)

        status = self.status()
        self.assertTrue(status["verification"]["satisfied"])
        self.assertTrue(status["qa_replacement"]["applies"])
        self.assertEqual(status["qa_replacement"]["candidate_sha"], new)


if __name__ == "__main__":
    unittest.main()
