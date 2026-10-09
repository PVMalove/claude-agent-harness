"""Public scenarios of issue #535: positive and negative CI evidence, immutable audit, guidance."""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

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

ROOT = Path(__file__).resolve().parents[2]


class CiScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.branch = PublishedBranch()
        self.addCleanup(self.branch.close)
        self.branch.publish()
        self.prepared = self.branch.prepare()
        self.record_id = self.prepared["integration_record_id"]
        self.candidate = self.prepared["candidate_sha"]
        self.target = self.prepared["target_sha"]
        write_project(self.branch.repo, GITHUB, ["lint"])

    def observe(self, **changes: object) -> CiObservation:
        values: JsonObject = dict(
            repository=REPO,
            pull_request=PR,
            base_ref="master",
            candidate_sha=self.candidate,
            checkout="combined",
            merge_commit_sha=MERGE,
            merge_parents=(self.candidate, self.target),
            checks=(CheckRun("lint", MERGE, "completed", "success", "10", None),),
        )
        values.update(changes)
        return CiObservation(**values)

    def collect(self, observation: CiObservation) -> JsonObject:
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

    def status(self) -> JsonObject:
        return coordinator.integration_status(
            self.branch.args(
                record=self.record_id, ticket=None, branch=None, batch=None
            )
        )

    def test_positive_scenario_collect_then_status_shows_the_replacement(self) -> None:
        self.assertEqual(self.collect(self.observe())["outcome"], "accepted")
        block = self.status()["qa_replacement"]
        self.assertTrue(block["applies"])
        self.assertEqual(block["merge_commit_sha"], MERGE)

    def test_negative_scenarios_never_yield_readiness(self) -> None:
        negatives = [
            self.observe(repository="other/repo"),
            self.observe(pull_request=99),
            self.observe(merge_parents=(self.candidate, "d" * 40)),
            self.observe(candidate_sha="d" * 40),
            self.observe(checkout="unknown"),
            self.observe(
                checks=(
                    CheckRun("lint", self.candidate, "completed", "success", "1", None),
                )
            ),
            self.observe(checks=(CheckRun("lint", MERGE, "queued", None, "1", None),)),
            self.observe(
                checks=(CheckRun("lint", MERGE, "completed", "cancelled", "1", None),)
            ),
            self.observe(error="the CI service did not answer"),
        ]
        for observation in negatives:
            result = self.collect(observation)
            self.assertNotEqual(result["outcome"], "accepted")
            self.assertFalse(result["recorded"])
        self.assertFalse(self.status()["qa_replacement"]["applies"])

    def test_evidence_is_immutable_and_audited(self) -> None:
        first = self.collect(self.observe())
        path = (
            self.branch.records()
            / "reports"
            / "integration-evidence"
            / f"{first['evidence_id']}.json"
        )
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.collect(self.observe())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        written = [
            item for item in self.branch.audit_paths() if first["evidence_id"] in item
        ]
        self.assertEqual(len(written), 1)

    def test_guidance_is_delivered(self) -> None:
        for relative in (
            "docs/backend-orchestration.md",
            "docs/harness-guide.md",
            "docs/adr/0016-ci-evidence.md",
        ):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn(
                "collect-ci" if "adr" not in relative else "merge_commit_sha", text
            )
        guide = (ROOT / "docs/harness-guide.md").read_text(encoding="utf-8")
        self.assertIn("ci_required_checks", guide)
        self.assertIn("local_qa_required", guide)


if __name__ == "__main__":
    unittest.main()
