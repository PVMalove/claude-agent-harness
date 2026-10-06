"""'integration next' of issue #537: the read-only step of a PR continuation.

Real ledger and Git (shared published-branch fixture); CI is a controlled in-memory ``CiSource``.
The command only observes: every test that calls it also proves nothing was written.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import unittest

from harness.orchestration import coordinator
from harness.orchestration.core.ci_source import CheckRun, CiObservation
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.ledger import LifecycleLedger
from harness.orchestration.workflow import resolver_state
from tests.orchestration.test_ci_collect import GITHUB, MERGE, PR, REPO, FakeSource
from tests.orchestration.test_ci_collect import write_project
from tests.orchestration.test_coordinator import ORCHESTRATION_ROOT
from tests.orchestration.test_integration_record import PublishedBranch, _git

CONFLICT_FILE = "services/x.py"


class NextFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.branch = PublishedBranch()
        self.branch.fixture.state_dir = (
            self.branch.repo / ".harness/orchestration/state"
        )
        self.addCleanup(self.branch.close)
        self.published = self.branch.publish()
        self.prepared = self.branch.prepare()
        self.record_id = self.prepared["integration_record_id"]
        write_project(self.branch.repo, GITHUB, ["lint", "tests"])

    # -- coordinator ----------------------------------------------------------------------------

    def next(self, pull_request: int | None = None) -> JsonObject:
        return coordinator.integration_next(
            self.branch.args(
                record=self.record_id,
                ticket=None,
                branch=None,
                batch=None,
                pull_request=pull_request,
            )
        )

    def refresh(self) -> JsonObject:
        self.branch.advance_integration_ref()
        return coordinator.integration_refresh(
            self.branch.args(
                record=self.record_id, ticket=None, branch=None, batch=None
            )
        )

    def pair(self) -> JsonObject:
        return coordinator.integration_status(
            self.branch.args(
                record=self.record_id, ticket=None, branch=None, batch=None
            )
        )

    def observation(self, pair: JsonObject, outcome: str = "success") -> CiObservation:
        runs = tuple(
            CheckRun(name, MERGE, "completed", outcome, str(index), None)
            for index, name in enumerate(("lint", "tests"), start=10)
        )
        return CiObservation(
            repository=REPO,
            pull_request=PR,
            base_ref="master",
            candidate_sha=pair["candidate_sha"],
            checkout="combined",
            merge_commit_sha=MERGE,
            merge_parents=(pair["candidate_sha"], pair["target_sha"]),
            checks=runs,
        )

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

    def local_qa(self, *commands: str) -> JsonObject:
        if commands:
            config = self.branch.repo / ".harness/project.json"
            value = json.loads(config.read_text(encoding="utf-8"))
            value["qa_gate_commands"] = list(commands)
            config.write_text(json.dumps(value), encoding="utf-8")
        return coordinator.integration_local_qa(
            self.branch.args(
                record=self.record_id,
                ci_condition="absent",
                reason="No combined CI",
                request=None,
                retry=False,
                lease_seconds=None,
            )
        )

    def spend_cycles(self, count: int) -> None:
        root = self.branch.state_root()
        ledger = LifecycleLedger(root)
        for index in range(count):
            resolver_state.write_event(
                ledger,
                root,
                "cycle-spent",
                {
                    "integration_record_id": self.record_id,
                    "target_sha": f"{index + 1:040x}",
                },
                ticket="#244",
                outcome="completed",
                cause="resolved",
            )


class StepTests(NextFixture):
    def test_an_unchanged_target_reuses_the_original_qa_before_the_pr(self) -> None:
        result = self.next()

        self.assertEqual(result["step"], "confirm-pr")
        self.assertEqual(result["candidate_sha"], self.published["candidate"])
        self.assertEqual(result["target_sha"], self.prepared["target_sha"])
        self.assertEqual(result["qa_source"], "original-qa")
        self.assertFalse(result["refreshed"])

    def test_an_unchanged_target_with_a_pr_hands_over_with_the_original_qa(
        self,
    ) -> None:
        result = self.next(PR)

        self.assertEqual(result["step"], "handoff")
        handoff = result["handoff"]
        self.assertEqual(handoff["qa_source"], "original-qa")
        self.assertEqual(handoff["candidate_sha"], self.published["candidate"])
        self.assertEqual(handoff["target_sha"], self.prepared["target_sha"])
        self.assertFalse(handoff["refreshed"])
        self.assertEqual(
            handoff["reference"]["dispatch_id"],
            self.pair()["source_evidence"]["qa"]["dispatch_id"],
        )

    def test_a_moved_target_asks_for_a_refresh(self) -> None:
        self.branch.advance_integration_ref()
        before = self.branch.snapshot()

        result = self.next()

        self.assertEqual(result["step"], "refresh")
        self.assertIn("integration refresh", result["next"][0])
        self.assertEqual(self.branch.snapshot(), before)

    def test_a_refreshed_pair_enters_pr_preparation_but_is_not_verified(self) -> None:
        refreshed = self.refresh()

        before_pr = self.next()
        after_pr = self.next(PR)

        self.assertEqual(before_pr["step"], "confirm-pr")
        self.assertEqual(before_pr["qa_source"], "verification-pending-after-pr")
        self.assertTrue(before_pr["refreshed"])
        self.assertEqual(before_pr["candidate_sha"], refreshed["new_candidate_sha"])
        self.assertEqual(after_pr["step"], "verify")
        self.assertNotIn("handoff", after_pr)
        self.assertIn("collect-ci", " ".join(after_pr["next"]))

    def test_collected_ci_hands_the_refreshed_pair_over(self) -> None:
        self.refresh()
        pair = self.pair()
        self.collect(self.observation(pair))

        result = self.next(PR)

        self.assertEqual(result["step"], "handoff")
        handoff = result["handoff"]
        self.assertEqual(handoff["qa_source"], "ci")
        self.assertTrue(handoff["refreshed"])
        self.assertEqual(handoff["candidate_sha"], pair["candidate_sha"])
        self.assertEqual(handoff["target_sha"], pair["target_sha"])
        self.assertEqual(handoff["reference"]["merge_commit_sha"], MERGE)
        self.assertEqual(handoff["reference"]["pull_request"], PR)

    def test_passed_local_qa_hands_the_refreshed_pair_over(self) -> None:
        self.refresh()
        ran = self.local_qa("true")

        result = self.next(PR)

        self.assertEqual(result["step"], "handoff")
        self.assertEqual(result["handoff"]["qa_source"], "local-qa")
        self.assertEqual(
            result["handoff"]["reference"]["evidence_id"], ran["evidence_id"]
        )

    def test_a_target_that_moves_after_verification_asks_for_a_new_refresh(
        self,
    ) -> None:
        self.refresh()
        self.collect(self.observation(self.pair()))
        self.assertEqual(self.next(PR)["step"], "handoff")

        self.branch.advance_integration_ref()

        result = self.next(PR)
        self.assertEqual(result["step"], "refresh")
        self.assertNotIn("handoff", result)

    def test_an_unreadable_integration_ref_is_an_operational_stop(self) -> None:
        _git(self.branch.repo, "remote", "set-url", "origin", "/nonexistent/remote")

        result = self.next()

        self.assertEqual(result["step"], "unavailable")
        self.assertNotIn("route", result)

    def test_operational_ci_and_local_qa_outcomes_never_route_a_code_failure(
        self,
    ) -> None:
        self.refresh()
        pending = self.observation(self.pair())
        pending = dataclasses.replace(
            pending,
            checks=(
                CheckRun("lint", MERGE, "completed", "success", "1", None),
                CheckRun("tests", MERGE, "in_progress", None, "2", None),
            ),
        )
        self.assertEqual(self.collect(pending)["outcome"], "fallback")
        self.assertEqual(self.next(PR)["step"], "verify")

        down = dataclasses.replace(self.observation(self.pair()), error="CI is down")
        self.assertEqual(self.collect(down)["outcome"], "fallback")
        self.assertEqual(self.next(PR)["step"], "verify")

    def test_the_public_cli_reaches_the_command(self) -> None:
        parsed = coordinator.parser().parse_args(
            ["integration", "next", "--record", self.record_id, "--pull-request", "7"]
        )
        self.assertIs(parsed.handler, coordinator.integration_next)
        self.assertEqual(parsed.pull_request, 7)

    def test_an_invalid_pull_request_number_is_refused(self) -> None:
        with self.assertRaises(CoordinatorError):
            self.next(0)

    def test_next_is_strictly_read_only(self) -> None:
        self.refresh()
        self.collect(self.observation(self.pair()))
        before = self.branch.snapshot()
        events = resolver_state.events(self.branch.state_root(), self.record_id)

        for pull_request in (None, PR):
            self.next(pull_request)

        self.assertEqual(self.branch.snapshot(), before)
        self.assertEqual(
            resolver_state.events(self.branch.state_root(), self.record_id), events
        )


class RouteFailureTests(NextFixture):
    def failed_ci(self) -> None:
        result = self.collect(self.observation(self.pair(), outcome="failure"))
        self.assertEqual(result["outcome"], "failed")

    def test_a_failed_check_of_a_refreshed_pair_routes_to_the_resolver(self) -> None:
        self.refresh()
        self.failed_ci()

        result = self.next(PR)

        self.assertEqual(result["step"], "route-failure")
        self.assertEqual(result["route"], "resolver")
        self.assertEqual(result["budget"]["cycles_total"], 2)
        self.assertEqual(result["budget"]["remaining"], 2)
        self.assertEqual(result["fixes_on_target"], 0)
        self.assertTrue(result["failed_evidence"])
        self.assertIn("integration resolve", " ".join(result["next"]))

    def test_a_failed_local_qa_of_a_refreshed_pair_routes_to_the_resolver(self) -> None:
        self.refresh()
        self.assertEqual(self.local_qa("false")["state"], "failed")

        result = self.next(PR)

        self.assertEqual(result["step"], "route-failure")
        self.assertEqual(result["route"], "resolver")

    def test_a_failed_check_of_the_original_pair_routes_to_the_developer(self) -> None:
        self.failed_ci()

        result = self.next(PR)

        self.assertEqual(result["step"], "route-failure")
        self.assertEqual(result["route"], "developer")
        self.assertIn("batch decide", " ".join(result["next"]))
        self.assertNotIn("budget", result)

    def test_spent_cycles_stop_at_a_human_decision(self) -> None:
        self.refresh()
        self.failed_ci()
        self.spend_cycles(2)

        result = self.next(PR)

        self.assertEqual(result["step"], "human-decision")
        self.assertEqual(result["route"], "resolver")
        self.assertEqual(result["budget"]["remaining"], 0)
        self.assertIn("--extends-budget", " ".join(result["next"]))

    def test_a_human_extension_reopens_the_route(self) -> None:
        self.refresh()
        self.failed_ci()
        self.spend_cycles(2)
        root = self.branch.state_root()
        resolver_state.write_event(
            LifecycleLedger(root),
            root,
            "human-decision",
            {"integration_record_id": self.record_id, "discriminator": "extension:x"},
            ticket="#244",
            decided_by="Malove",
            note="one more",
            extends_budget=True,
        )

        result = self.next(PR)

        self.assertEqual(result["step"], "route-failure")
        self.assertEqual(result["budget"]["cycles_total"], 3)

    def test_a_passed_check_supersedes_an_earlier_failed_one(self) -> None:
        self.refresh()
        self.failed_ci()
        self.local_qa("true")

        self.assertEqual(self.next(PR)["step"], "handoff")


class OpenResolverTests(NextFixture):
    def test_an_open_resolver_batch_keeps_the_pr_session_waiting(self) -> None:
        shutil.copy(
            ORCHESTRATION_ROOT / "roles" / "conflict-resolver.md",
            self.branch.repo / ".harness" / "orchestration" / "roles",
        )
        repo = self.branch.repo
        _git(repo, "checkout", "-q", "master")
        (repo / CONFLICT_FILE).parent.mkdir(parents=True, exist_ok=True)
        (repo / CONFLICT_FILE).write_text("VALUE = 'upstream'\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "feat: land services/x.py (#901)")
        _git(repo, "push", "origin", "master")
        created = coordinator.integration_resolve(
            self.branch.args(
                record=self.record_id, ticket=None, branch=None, batch=None
            )
        )
        before = self.branch.snapshot()

        result = self.next()

        self.assertEqual(result["step"], "resolver-open")
        self.assertEqual(result["batch_id"], created["batch_id"])
        self.assertEqual(result["next_action"], "resolve-conflict")
        self.assertEqual(self.branch.snapshot(), before)
        self.assertEqual(
            resolver_state.budget(self.branch.state_root(), {}, self.record_id)[
                "cycles_spent"
            ],
            0,
        )


if __name__ == "__main__":
    unittest.main()
