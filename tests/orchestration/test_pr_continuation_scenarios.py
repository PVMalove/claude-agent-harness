"""Public end-to-end scenarios of issue #537: a PR session from actualization to a manual-merge handoff.

Every step goes through the public coordinator CLI parser and its handlers, the way
``/to-pull-requests`` drives it; real ledger and Git (shared published-branch fixture) and a
controlled in-memory CI source.  The pull request itself is opened outside the coordinator (the
skill uses the tracker CLI), so a scenario only passes its number to the commands that need it.
"""

from __future__ import annotations

import dataclasses
import shutil
from unittest import mock

from harness.gate_runner.gate_runner import GateRunnerError
from harness.orchestration import coordinator
from harness.orchestration.core.ci_source import CheckRun
from harness.orchestration.core.utils import JsonObject
from harness.orchestration.workflow import local_qa, resolver_state
from tests.orchestration.test_ci_collect import MERGE, PR, FakeSource
from tests.orchestration.test_coordinator import ORCHESTRATION_ROOT
from tests.orchestration.test_integration_record import _git
from tests.orchestration.test_pr_continuation import CONFLICT_FILE, NextFixture


class PrSession(NextFixture):
    """The coordinator calls of one PR session, parsed exactly as the CLI parses them."""

    def cli(self, *argv: str, **injected: object) -> JsonObject:
        args = coordinator.parser().parse_args(
            [
                *argv,
                "--repo",
                str(self.branch.repo),
                "--state-dir",
                str(self.branch.fixture.state_dir),
            ]
        )
        for name, value in injected.items():
            setattr(args, name, value)
        result: JsonObject = args.handler(args)
        return result

    def ticket(self) -> list[str]:
        return ["--ticket", "#244", "--branch", self.branch.branch]

    def step(self, pull_request: int | None = None) -> JsonObject:
        extra = ["--pull-request", str(pull_request)] if pull_request else []
        return self.cli("integration", "next", *self.ticket(), *extra)

    def refresh_cli(self) -> JsonObject:
        return self.cli("integration", "refresh", *self.ticket())

    def collect_cli(self, observation: object) -> JsonObject:
        return self.cli(
            "integration",
            "collect-ci",
            *self.ticket(),
            "--pull-request",
            str(PR),
            ci_source=FakeSource(observation),  # type: ignore[arg-type]
        )

    def local_qa_cli(self, hint: JsonObject) -> JsonObject:
        return self.cli(
            "integration",
            "local-qa",
            "--record",
            self.record_id,
            "--ci-condition",
            hint["ci_condition"],
            "--reason",
            "combined-result CI cannot verify the pair",
        )

    def land_conflict(self) -> str:
        repo = self.branch.repo
        _git(repo, "checkout", "-q", "master")
        (repo / CONFLICT_FILE).parent.mkdir(parents=True, exist_ok=True)
        (repo / CONFLICT_FILE).write_text("VALUE = 'upstream'\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "feat: land services/x.py (#901)")
        _git(repo, "push", "origin", "master")
        return _git(repo, "rev-parse", "HEAD")

    def events(self) -> list[JsonObject]:
        return resolver_state.events(self.branch.state_root(), self.record_id)


class UnchangedAndCleanRebaseTests(PrSession):
    def test_an_unchanged_target_reuses_the_original_qa_through_the_whole_session(
        self,
    ) -> None:
        before = self.step()
        after_open = self.step(PR)

        self.assertEqual(before["step"], "confirm-pr")
        self.assertEqual(before["qa_source"], "original-qa")
        self.assertEqual(after_open["step"], "handoff")
        self.assertEqual(after_open["handoff"]["qa_source"], "original-qa")
        self.assertEqual(
            after_open["handoff"]["target_sha"], self.prepared["target_sha"]
        )
        self.assertEqual(self.events(), [])

    def test_a_clean_rebase_then_accepted_ci_replaces_a_repeat_of_full_local_qa(
        self,
    ) -> None:
        self.branch.advance_integration_ref()
        self.assertEqual(self.step()["step"], "refresh")

        refreshed = self.refresh_cli()
        entering = self.step()
        opened = self.step(PR)
        collected = self.collect_cli(self.observation(self.pair()))
        handoff = self.step(PR)

        self.assertEqual(refreshed["state"], "rebased")
        self.assertEqual(entering["step"], "confirm-pr")
        self.assertEqual(entering["qa_source"], "verification-pending-after-pr")
        self.assertEqual(opened["step"], "verify")
        self.assertEqual(collected["outcome"], "accepted")
        self.assertNotIn("next", collected)
        self.assertEqual(handoff["step"], "handoff")
        self.assertEqual(handoff["handoff"]["qa_source"], "ci")
        self.assertEqual(
            handoff["handoff"]["candidate_sha"], refreshed["new_candidate_sha"]
        )
        # Neither the clean rebase nor the wait for CI spent a resolver cycle.
        self.assertEqual(self.events(), [])

    def test_a_target_detected_after_verification_repeats_actualization(self) -> None:
        self.branch.advance_integration_ref()
        self.refresh_cli()
        self.collect_cli(self.observation(self.pair()))
        self.assertEqual(self.step(PR)["step"], "handoff")

        moved = _git(self.branch.repo, "rev-parse", "origin/master")
        self.branch.advance_integration_ref()

        again = self.step(PR)
        self.assertEqual(again["step"], "refresh")
        self.assertNotIn("handoff", again)
        self.assertNotEqual(again["integration_tip"], moved)


class CiWaitAndFallbackTests(PrSession):
    def setUp(self) -> None:
        super().setUp()
        self.branch.advance_integration_ref()
        self.refresh_cli()

    def pending(self) -> object:
        return dataclasses.replace(
            self.observation(self.pair()),
            checks=(
                CheckRun("lint", MERGE, "completed", "success", "1", None),
                CheckRun("tests", MERGE, "in_progress", None, "2", None),
            ),
        )

    def test_a_pending_check_waits_without_spending_a_cycle_then_is_accepted(
        self,
    ) -> None:
        waiting = self.collect_cli(self.pending())
        self.assertEqual(waiting["next"], {"action": "wait", "ci_condition": None})
        self.assertEqual(self.step(PR)["step"], "verify")
        self.assertEqual(
            self.collect_cli(self.observation(self.pair()))["outcome"], "accepted"
        )

        self.assertEqual(self.step(PR)["step"], "handoff")
        self.assertEqual(self.events(), [])

    def test_unusable_ci_falls_back_to_local_qa_and_hands_over_its_evidence(
        self,
    ) -> None:
        head_only = dataclasses.replace(
            self.observation(self.pair()),
            checks=(
                CheckRun(
                    "lint",
                    self.pair()["candidate_sha"],
                    "completed",
                    "success",
                    "1",
                    None,
                ),
                CheckRun(
                    "tests",
                    self.pair()["candidate_sha"],
                    "completed",
                    "success",
                    "2",
                    None,
                ),
            ),
        )
        collected = self.collect_cli(head_only)
        self.assertEqual(
            collected["next"], {"action": "local-qa", "ci_condition": "unusable"}
        )
        self.assertEqual(self.step(PR)["step"], "verify")

        ran = self.local_qa_cli(collected["next"])
        handoff = self.step(PR)

        self.assertEqual(ran["state"], "completed")
        self.assertEqual(ran["ci_condition"], "unusable")
        self.assertEqual(handoff["step"], "handoff")
        self.assertEqual(handoff["handoff"]["qa_source"], "local-qa")
        self.assertEqual(
            handoff["handoff"]["reference"]["evidence_id"], ran["evidence_id"]
        )

    def test_unavailable_local_qa_is_an_operational_stop_not_a_code_failure(
        self,
    ) -> None:
        down = GateRunnerError("checkout unavailable", remedy="restore the checkout")
        with mock.patch.object(local_qa, "run_gate", side_effect=down):
            ran = self.local_qa_cli({"ci_condition": "unavailable"})

        self.assertEqual(ran["state"], "unavailable")
        step = self.step(PR)
        self.assertEqual(step["step"], "verify")
        self.assertNotIn("route", step)


class ConflictAndRoutingTests(PrSession):
    def setUp(self) -> None:
        super().setUp()
        shutil.copy(
            ORCHESTRATION_ROOT / "roles" / "conflict-resolver.md",
            self.branch.repo / ".harness" / "orchestration" / "roles",
        )

    def test_a_textual_conflict_goes_to_the_resolver_and_the_session_waits(
        self,
    ) -> None:
        self.land_conflict()
        self.assertEqual(self.step()["step"], "refresh")

        refreshed = self.refresh_cli()
        resolved = self.cli("integration", "resolve", *self.ticket())
        waiting = self.step()

        self.assertEqual(refreshed["state"], "conflict")
        self.assertEqual(resolved["state"], "created")
        self.assertEqual(resolved["trigger"], "conflict")
        self.assertEqual(waiting["step"], "resolver-open")
        self.assertEqual(waiting["batch_id"], resolved["batch_id"])
        self.assertEqual(self.events(), [])

    def test_a_failed_check_of_a_refreshed_pair_routes_to_the_same_resolver(
        self,
    ) -> None:
        self.branch.advance_integration_ref()
        self.refresh_cli()
        failed = self.collect_cli(self.observation(self.pair(), "failure"))
        self.assertEqual(failed["outcome"], "failed")

        routed = self.step(PR)
        created = self.cli("integration", "resolve", *self.ticket())
        waiting = self.step(PR)

        self.assertEqual(
            (routed["step"], routed["route"]), ("route-failure", "resolver")
        )
        self.assertEqual(created["trigger"], "verification-failure")
        self.assertEqual(waiting["step"], "resolver-open")
        self.assertEqual(self.events(), [])

    def test_a_failed_check_of_the_original_pair_goes_to_a_regular_developer(
        self,
    ) -> None:
        failed = self.collect_cli(self.observation(self.pair(), "failure"))
        self.assertEqual(failed["outcome"], "failed")

        routed = self.step(PR)

        self.assertEqual(
            (routed["step"], routed["route"]), ("route-failure", "developer")
        )
        self.assertIn("batch decide", " ".join(routed["next"]))

    def test_spent_cycles_stop_at_a_human_decision_that_an_answer_never_resets(
        self,
    ) -> None:
        self.branch.advance_integration_ref()
        self.refresh_cli()
        self.collect_cli(self.observation(self.pair(), "failure"))
        self.spend_cycles(2)

        stopped = self.step(PR)
        # Asking again (a new continuation of the same PR) finds the same spent budget.
        again = self.step(PR)

        self.assertEqual(stopped["step"], "human-decision")
        self.assertEqual(again["budget"], stopped["budget"])
        self.assertEqual(again["budget"]["remaining"], 0)


class NextIsReadOnlyThroughTheCliTests(PrSession):
    def test_every_step_leaves_ledger_and_git_untouched(self) -> None:
        self.branch.advance_integration_ref()
        self.refresh_cli()
        self.collect_cli(self.observation(self.pair(), "failure"))
        before = self.branch.snapshot()
        events = self.events()

        for pull_request in (None, PR):
            self.step(pull_request)

        self.assertEqual(self.branch.snapshot(), before)
        self.assertEqual(self.events(), events)
