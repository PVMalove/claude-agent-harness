#!/usr/bin/env python3
"""The conflict-resolver route (issue #534), through the public coordinator workflow.

Real ledger and real Git (a local bare remote); no mocks.  A published ticket branch meets a moved
integration tip with a textual conflict, and the route hands it to a conflict-resolver writer.
"""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

from harness.orchestration import coordinator
from harness.orchestration.core import git_utils
from harness.orchestration.core.ci_source import CheckRun, CiObservation
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.core.workspace import _prepare_agent_inbox as workspace_inbox
from harness.orchestration.workflow import resolver_state
from tests.orchestration.test_ci_collect import GITHUB, MERGE, PR, REPO, FakeSource
from tests.orchestration.test_ci_collect import write_project
from tests.orchestration.test_coordinator import ORCHESTRATION_ROOT
from tests.orchestration.test_integration_record import PublishedBranch, _git

CONFLICT_FILE = "services/x.py"
TARGET_TEXT = "VALUE = 'upstream'\n"
MERGED_TEXT = "VALUE = 'merged'\n"


class ResolverFixture(unittest.TestCase):
    """A published ticket branch with an integration record, ready for a conflicting target."""

    def setUp(self) -> None:
        self.branch = PublishedBranch()
        self.addCleanup(self.branch.close)
        self.fx = self.branch.fixture
        roles = self.branch.repo / ".harness" / "orchestration" / "roles"
        shutil.copy(ORCHESTRATION_ROOT / "roles" / "conflict-resolver.md", roles)
        self.published = self.branch.publish()
        self.record_id = self.branch.prepare()["integration_record_id"]
        self.worktree = self.fx.worktree

    # -- git ------------------------------------------------------------------------------------

    def land(self, name: str = CONFLICT_FILE, content: str = TARGET_TEXT) -> str:
        """Another change lands on the integration ref in the remote."""
        repo = self.branch.repo
        _git(repo, "checkout", "-q", "master")
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content, encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", f"feat: land {name} (#901)")
        _git(repo, "push", "origin", "master")
        return _git(repo, "rev-parse", "HEAD")

    def resolve_in_worktree(self, tip: str, content: str = MERGED_TEXT) -> str:
        """What a resolver does: rebase onto the exact target, resolve, continue, commit."""
        _git(self.worktree, "fetch", "-q", "origin", "master")
        try:
            _git(self.worktree, "rebase", tip)
        except RuntimeError:
            pass
        (self.worktree / CONFLICT_FILE).write_text(content, encoding="utf-8")
        _git(self.worktree, "add", "-A")
        _git(
            self.worktree,
            "-c",
            "core.editor=true",
            "rebase",
            "--continue",
        )
        return _git(self.worktree, "rev-parse", "HEAD")

    # -- coordinator ----------------------------------------------------------------------------

    def resolve(self, **overrides: object) -> JsonObject:
        values: JsonObject = {
            "record": self.record_id,
            "ticket": None,
            "branch": None,
            "batch": None,
        }
        values.update(overrides)
        return coordinator.integration_resolve(self.branch.args(**values))

    def events(self) -> list[JsonObject]:
        return resolver_state.events(self.branch.state_root(), self.record_id)

    def batch_record(self, batch_id: str) -> JsonObject:
        return self.fx._batch_record(batch_id)

    def approved_resolver_dispatch(self, created: JsonObject) -> JsonObject:
        batch_id = created["batch_id"]
        coordinator.approve_batch(
            self.branch.args(batch=batch_id, **self.fx._approval())
        )
        return self.fx._dispatch(batch_id, "conflict-resolver")

    def start(self, brief: JsonObject) -> None:
        self.fx._start(brief["dispatch_id"])

    def report(
        self,
        brief: JsonObject,
        resolved: str,
        *,
        outcome: str = "completed",
        cause: str = "resolved",
        decisions: list[str] | None = None,
        **block_overrides: object,
    ) -> JsonObject:
        """A conflict-resolver report: the resolver block mirrors what the brief asked for."""
        section = brief["resolver"]
        target = section["target_sha"]
        changed = git_utils._changed_files_between(self.branch.repo, target, resolved)
        commits = git_utils._commits_between(self.branch.repo, target, resolved)
        requirements = [
            {"side": "candidate", "requirement": text, "preserved_by": "kept"}
            for text in section["sides"]["candidate"]["requirements"]
        ] + [
            {"side": "target", "requirement": text, "preserved_by": "kept"}
            for text in section["sides"]["target"]["requirements"]
        ]
        block: JsonObject = {
            "preserved_requirements": requirements,
            "human_decisions": decisions or [],
            "target_sha": target,
            "resolved_candidate_sha": resolved,
            "cause": cause,
            "changed_files": changed,
            "commits": [
                {"commit_sha": sha, "plan_entry_id": "resolution"} for sha in commits
            ],
        }
        block.update(block_overrides)
        return self.fx._base_report(
            brief,
            "conflict-resolver",
            outcome=outcome,
            commit_sha=resolved,
            changed_files=changed,
            resolver=block,
        )

    def submit(self, brief: JsonObject, report: JsonObject) -> JsonObject:
        return self.fx._submit(brief["dispatch_id"], report)

    def run_cycle(self, content: str = MERGED_TEXT, *, abandon: bool = True) -> str:
        """One whole automatic cycle on a freshly landed conflicting target."""
        self.landed = getattr(self, "landed", 0) + 1
        tip = self.land(content=f"VALUE = 'landed-{self.landed}'\n")
        created = self.resolve()
        dispatch = self.approved_resolver_dispatch(created)
        self.start(dispatch["brief"])
        resolved = self.resolve_in_worktree(tip, content)
        self.submit(dispatch["brief"], self.report(dispatch["brief"], resolved))
        self.fx._decide(created["batch_id"], "accept")
        if abandon:
            coordinator.abandon_batch(
                self.branch.args(
                    batch=created["batch_id"],
                    reason="the next target needs a fresh resolver batch",
                    **self.fx._approval(),
                )
            )
        return tip


class ResolveRouteTests(ResolverFixture):
    def test_a_textual_conflict_creates_a_resolver_batch_and_a_complete_brief(
        self,
    ) -> None:
        tip = self.land()
        before = {
            "head": _git(self.worktree, "rev-parse", "HEAD"),
            "remote": _git(
                self.branch.repo,
                "ls-remote",
                "origin",
                f"refs/heads/{self.branch.branch}",
            ),
        }

        created = self.resolve()

        self.assertEqual(created["state"], "created")
        self.assertEqual(created["conflicting_files"], [CONFLICT_FILE])
        self.assertEqual(created["candidate_sha"], self.published["candidate"])
        self.assertEqual(created["target_sha"], tip)
        self.assertEqual(created["next_action"], "resolve-conflict")
        self.assertEqual(created["budget"]["remaining"], 2)
        # Nothing was written to Git.
        self.assertEqual(_git(self.worktree, "rev-parse", "HEAD"), before["head"])
        self.assertEqual(_git(self.worktree, "status", "--porcelain"), "")
        self.assertEqual(
            _git(
                self.branch.repo,
                "ls-remote",
                "origin",
                f"refs/heads/{self.branch.branch}",
            ),
            before["remote"],
        )
        batch = self.batch_record(created["batch_id"])
        self.assertEqual(batch["kind"], "resolver")
        self.assertEqual(batch["base_commit"], tip)
        self.assertIn(CONFLICT_FILE, batch["allowed_paths"])
        self.assertNotEqual(created["batch_id"], self.published["batch_id"])
        self.assertEqual(
            self.batch_record(self.published["batch_id"])["state"], "completed"
        )

        dispatch = self.approved_resolver_dispatch(created)
        brief = dispatch["brief"]

        self.assertEqual(brief["role"], "conflict-resolver")
        self.assertEqual(brief["access"], "write")
        self.assertEqual(brief["ticket"], "#244")
        self.assertEqual(brief["snapshot_commit"], self.published["candidate"])
        section = brief["resolver"]
        self.assertEqual(section["ticket"], "#244")
        self.assertEqual(section["candidate_sha"], self.published["candidate"])
        self.assertEqual(section["target_sha"], tip)
        self.assertEqual(
            section["sides"]["candidate"],
            {"ticket": "#244", "requirements": ["route retries by cause"]},
        )
        self.assertEqual(section["sides"]["target"]["tickets"], ["#901"])
        self.assertEqual(
            section["sides"]["target"]["requirements"],
            [f"feat: land {CONFLICT_FILE} (#901)"],
        )
        self.assertEqual(section["scope"], batch["allowed_paths"])
        self.assertTrue(section["prohibitions"])
        self.assertEqual(
            [item["id"] for item in section["commit_plan"]], ["resolution"]
        )
        self.assertEqual(section["checks"], brief["verification_commands"])
        self.assertEqual(section["budget"]["remaining"], 2)
        self.assertEqual(section["report_staging_path"], brief["report_staging_path"])
        stored = json.loads(
            (
                self.branch.records() / "dispatches" / f"{brief['dispatch_id']}.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(stored["resolver"], section)

    def test_the_role_points_at_the_resolving_merge_conflicts_skill(self) -> None:
        text = (ORCHESTRATION_ROOT / "roles" / "conflict-resolver.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("resolving-merge-conflicts", text)
        self.assertIn(".agents/skills", text)

    def test_a_clean_rebase_is_refused_and_leaves_the_budget_alone(self) -> None:
        self.land("landed.txt", "landed\n")

        with self.assertRaisesRegex(CoordinatorError, "clean"):
            self.resolve()

        self.assertEqual(self.events(), [])
        self.assertEqual(
            _git(self.worktree, "rev-parse", "HEAD"), self.published["candidate"]
        )

    def test_without_a_moved_target_there_is_nothing_to_resolve(self) -> None:
        with self.assertRaisesRegex(CoordinatorError, "no conflict"):
            self.resolve()

    def test_a_failed_check_of_the_original_pair_is_not_the_resolvers(self) -> None:
        """The branch was never refreshed: its failure is the task's own defect, which goes to a
        regular developer, so the resolver route refuses it."""
        write_project(self.branch.repo, GITHUB, ["lint", "tests"])
        candidate = self.published["candidate"]
        target = _git(self.branch.repo, "rev-parse", "origin/master")
        runs = tuple(
            CheckRun(name, MERGE, "completed", "failure", str(index), None)
            for index, name in enumerate(("lint", "tests"), start=10)
        )
        observation = CiObservation(
            repository=REPO,
            pull_request=PR,
            base_ref="master",
            candidate_sha=candidate,
            checkout="combined",
            merge_commit_sha=MERGE,
            merge_parents=(candidate, target),
            checks=runs,
        )
        collected = coordinator.integration_collect_ci(
            self.branch.args(
                record=self.record_id,
                ticket=None,
                branch=None,
                batch=None,
                pull_request=PR,
                ci_source=FakeSource(observation),
            )
        )
        self.assertEqual(collected["outcome"], "failed")

        with self.assertRaisesRegex(CoordinatorError, "no conflict"):
            self.resolve()

        self.assertEqual(self.events(), [])

    def test_repeating_the_route_returns_the_open_resolver_batch(self) -> None:
        self.land()
        first = self.resolve()
        again = self.resolve()
        self.assertEqual(again["state"], "existing")
        self.assertEqual(again["batch_id"], first["batch_id"])

    def test_the_resolver_is_not_dispatchable_without_the_route(self) -> None:
        self.land()
        created = self.resolve()
        coordinator.approve_batch(
            self.branch.args(batch=created["batch_id"], **self.fx._approval())
        )
        with self.assertRaisesRegex(CoordinatorError, "next action"):
            self.fx._dispatch(created["batch_id"], "developer")

    def test_the_public_cli_reaches_the_route(self) -> None:
        parsed = coordinator.parser().parse_args(
            [
                "--repo",
                str(self.branch.repo),
                "integration",
                "resolve",
                "--record",
                self.record_id,
            ]
        )
        self.assertIs(parsed.handler, coordinator.integration_resolve)
        event = coordinator.parser().parse_args(
            [
                "integration",
                "resolver-event",
                "--record",
                self.record_id,
                "--kind",
                "human-decision",
                "--dispatch",
                "dispatch-1",
                "--decided-by",
                "Malove",
                "--note",
                "n",
                "--extends-budget",
            ]
        )
        self.assertIs(event.handler, coordinator.resolver_event)
        self.assertTrue(event.extends_budget)


class ResolutionFlowTests(ResolverFixture):
    def link(self, candidate: str, target: str, kind: str) -> None:
        coordinator.integration_link_evidence(
            self.branch.args(
                record=self.record_id,
                kind=kind,
                candidate_commit=candidate,
                target_commit=target,
                result="passed",
                reference=f"https://ci.example.invalid/{kind}",
                artifact_sha256=None,
            )
        )

    def status(self) -> JsonObject:
        return coordinator.integration_status(
            self.branch.args(
                record=self.record_id, ticket=None, branch=None, batch=None
            )
        )

    def test_the_resolver_resolves_reports_and_the_report_is_recorded_with_its_block(
        self,
    ) -> None:
        tip = self.land()
        created = self.resolve()
        brief = self.approved_resolver_dispatch(created)["brief"]
        self.start(brief)
        resolved = self.resolve_in_worktree(tip)

        result = self.submit(brief, self.report(brief, resolved))

        self.assertEqual(result["state"], "reported")
        stored = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
        block = stored["resolver"]
        self.assertEqual(block["resolved_candidate_sha"], resolved)
        self.assertEqual(block["human_decisions"], [])
        self.assertEqual(block["changed_files"], [CONFLICT_FILE])
        self.assertEqual([item["commit_sha"] for item in block["commits"]], [resolved])
        sides = {item["side"] for item in block["preserved_requirements"]}
        self.assertEqual(sides, {"candidate", "target"})
        [spent] = self.events()
        self.assertEqual(spent["kind"], "cycle-spent")
        self.assertEqual(spent["target_sha"], tip)
        self.assertEqual(spent["dispatch_id"], brief["dispatch_id"])

    def test_an_accepted_resolution_takes_the_narrow_route_and_still_needs_new_qa(
        self,
    ) -> None:
        tip = self.land()
        created = self.resolve()
        brief = self.approved_resolver_dispatch(created)["brief"]
        self.start(brief)
        resolved = self.resolve_in_worktree(tip)
        self.submit(brief, self.report(brief, resolved))

        decided = self.fx._decide(created["batch_id"], "accept")

        self.assertEqual(decided["next_action"], "risk-assessment")
        changed = git_utils._changed_files_between(self.branch.repo, tip, resolved)
        risk = coordinator.assess_risk(
            self.branch.args(
                batch=created["batch_id"],
                candidate_commit=resolved,
                base_commit=None,
                changed_file=changed,
                developer_trigger=[],
            )
        )
        # The repeat review is skipped; QA of the new pair is not.
        self.assertFalse(risk["review_required"])
        self.assertEqual(self.batch_record(created["batch_id"])["next_action"], "qa")
        qa = self.fx._dispatch(created["batch_id"], "qa", candidate=resolved)
        self.assertEqual(qa["brief"]["candidate_commit"], resolved)
        # The record's pair moved to the resolved candidate; only CI or local QA confirms it.
        status = self.status()
        self.assertEqual(status["candidate_sha"], resolved)
        self.assertEqual(status["target_sha"], tip)
        self.assertTrue(status["verification"]["required"])
        self.assertFalse(status["verification"]["satisfied"])
        self.assertFalse(status["verification"]["re_review_required"])
        self.link(resolved, tip, "resolver")
        self.assertFalse(self.status()["verification"]["satisfied"])
        self.link(resolved, tip, "ci")
        # A CI result linked by hand is unverified evidence (issue #535): only 'collect-ci'
        # acceptance or generated local QA of the new pair (issue #536) closes the verification.
        self.assertFalse(self.status()["verification"]["satisfied"])
        self.link(resolved, tip, "local-qa")
        self.assertFalse(self.status()["verification"]["satisfied"])
        refresh = json.loads(
            next(
                (self.branch.records() / "reports/integration-refresh").glob("*.json")
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(refresh["resolver"], {"invoked": True, "cycles_spent": 1})

    def test_a_resolver_report_is_never_auto_accepted_by_policy(self) -> None:
        tip = self.land()
        created = self.resolve()
        brief = self.approved_resolver_dispatch(created)["brief"]
        self.start(brief)
        resolved = self.resolve_in_worktree(tip)

        result = self.submit(brief, self.report(brief, resolved))

        self.assertNotIn("auto_accepted", result)
        self.assertEqual(
            self.batch_record(created["batch_id"]).get("next_action"),
            "resolve-conflict",
        )


class ReportContractTests(ResolverFixture):
    def reported(self) -> tuple[JsonObject, str]:
        tip = self.land()
        created = self.resolve()
        brief = self.approved_resolver_dispatch(created)["brief"]
        self.start(brief)
        return brief, self.resolve_in_worktree(tip)

    def test_a_completed_report_without_the_block_is_rejected(self) -> None:
        brief, resolved = self.reported()
        report = self.report(brief, resolved)
        del report["resolver"]
        with self.assertRaisesRegex(CoordinatorError, "resolver block"):
            self.submit(brief, report)
        self.assertEqual(self.events(), [])

    def test_a_requirement_of_either_side_left_out_is_rejected(self) -> None:
        brief, resolved = self.reported()
        report = self.report(brief, resolved)
        report["resolver"]["preserved_requirements"].pop()
        with self.assertRaisesRegex(CoordinatorError, "requirements of both sides"):
            self.submit(brief, report)
        report = self.report(brief, resolved)
        report["resolver"]["preserved_requirements"] = [
            item
            for item in report["resolver"]["preserved_requirements"]
            if item["side"] == "candidate"
        ]
        with self.assertRaisesRegex(CoordinatorError, "requirements of both sides"):
            self.submit(brief, report)

    def test_human_decisions_must_name_exactly_the_recorded_events(self) -> None:
        brief, resolved = self.reported()
        with self.assertRaisesRegex(CoordinatorError, "human_decisions"):
            self.submit(brief, self.report(brief, resolved, decisions=["invented"]))

    def test_commits_and_changed_files_must_match_git(self) -> None:
        brief, resolved = self.reported()
        with self.assertRaisesRegex(CoordinatorError, "commits"):
            self.submit(brief, self.report(brief, resolved, commits=[]))
        with self.assertRaisesRegex(CoordinatorError, "changed_files"):
            self.submit(brief, self.report(brief, resolved, changed_files=["x"]))

    def test_the_resolution_must_contain_the_exact_target(self) -> None:
        brief, _ = self.reported()
        # The worktree is still at the unresolved candidate, which lacks the target.
        with self.assertRaises(CoordinatorError):
            self.submit(brief, self.report(brief, self.published["candidate"]))

    def test_another_role_may_not_carry_the_block(self) -> None:
        brief, resolved = self.reported()
        report = self.report(brief, resolved)
        other = {**brief, "role": "developer"}
        with self.assertRaisesRegex(CoordinatorError, "only a conflict-resolver"):
            resolver_state.validate_report(
                self.branch.repo, self.branch.state_root(), {}, other, report
            )


class ContinuationTests(ResolverFixture):
    def checkpoint(self, brief: JsonObject) -> JsonObject:
        """The resolver found incompatible requirements: it checkpoints with the options."""
        candidate = self.published["candidate"]
        tip = brief["resolver"]["target_sha"]
        name = f"checkpoint-{brief['dispatch_id']}.json"
        path = workspace_inbox(self.branch.repo) / name
        path.write_text(
            json.dumps(
                {
                    "dispatch_id": brief["dispatch_id"],
                    "commit_sha": candidate,
                    "changed_files": git_utils._changed_files_between(
                        self.branch.repo, tip, candidate
                    ),
                    "remaining_definition_of_done": brief["definition_of_done"],
                    "passing_checks": [],
                    "risks": "the sides contradict each other",
                    "blockers": "option keep-target drops the ticket value; "
                    "option keep-ticket drops the upstream value",
                    "context_package_id": "not applicable — no context package registered",
                }
            ),
            encoding="utf-8",
        )
        return coordinator.checkpoint_dispatch(self.branch.args(file=str(path)))

    def facts(self, brief: JsonObject, **override: object) -> str:
        document = {
            "dispatch_id": brief["dispatch_id"],
            "remaining_definition_of_done": brief["definition_of_done"],
            "risks": "the sides contradict each other",
            "dependencies": brief["dependencies"],
            **override,
        }
        path = workspace_inbox(self.branch.repo) / f"facts-{brief['dispatch_id']}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return str(path)

    def decide(self, brief: JsonObject, kind: str = "human-decision") -> JsonObject:
        return coordinator.resolver_event(
            self.branch.args(
                record=self.record_id,
                ticket=None,
                branch=None,
                batch=None,
                kind=kind,
                dispatch=brief["dispatch_id"],
                decided_by="Malove",
                note="keep the ticket value",
                option="keep-ticket" if kind == "human-decision" else None,
                extends_budget=False,
            )
        )

    def resume(self, brief: JsonObject, **override: object) -> JsonObject:
        values: JsonObject = {
            "dispatch": brief["dispatch_id"],
            "termination_reason": None,
            "trigger": "human-decision",
            "measured_value": None,
            "note": None,
            **self.fx._approval(),
        }
        values["file"] = self.facts(brief) if "file" not in override else None
        values.update(override)
        return coordinator.resume_dispatch(self.branch.args(**values))

    def budget(self) -> JsonObject:
        return resolver_state.budget(
            self.branch.state_root(),
            coordinator._config(self.branch.repo),
            self.record_id,
        )

    def test_a_human_decision_is_its_own_event_and_the_same_session_continues(
        self,
    ) -> None:
        tip = self.land()
        created = self.resolve()
        brief = self.approved_resolver_dispatch(created)["brief"]
        self.start(brief)
        dispatches_before = len(self.batch_record(created["batch_id"])["dispatches"])
        neighbour = self.batch_record(self.published["batch_id"])

        self.checkpoint(brief)
        with self.assertRaisesRegex(CoordinatorError, "human-decision"):
            self.resume(brief)
        event = self.decide(brief)
        resumed = self.resume(brief)

        self.assertEqual(event["kind"], "human-decision")
        self.assertEqual(event["option"], "keep-ticket")
        self.assertEqual(event["decided_by"], "Malove")
        self.assertEqual(resumed["dispatch_id"], brief["dispatch_id"])
        self.assertEqual(resumed["state"], "dispatched")
        batch = self.batch_record(created["batch_id"])
        self.assertEqual(len(batch["dispatches"]), dispatches_before)
        self.assertEqual(self.batch_record(self.published["batch_id"]), neighbour)
        self.assertEqual(self.budget()["cycles_spent"], 0)
        # The resumed session finishes with the decision named in its report.
        coordinator.self_report_dispatch(
            self.branch.args(
                dispatch=brief["dispatch_id"], model="sonnet", worktree=None
            )
        )
        resolved = self.resolve_in_worktree(tip)
        self.submit(brief, self.report(brief, resolved, decisions=[event["event_id"]]))
        self.assertEqual(
            [item["kind"] for item in self.events()], ["human-decision", "cycle-spent"]
        )

    def test_resume_with_changed_scope_facts_is_refused_and_the_brief_stays(
        self,
    ) -> None:
        self.land()
        created = self.resolve()
        brief = self.approved_resolver_dispatch(created)["brief"]
        self.start(brief)
        path = self.branch.records() / "dispatches" / f"{brief['dispatch_id']}.json"
        stored = path.read_bytes()
        self.checkpoint(brief)
        self.decide(brief)

        with self.assertRaisesRegex(CoordinatorError, "ordinary approval"):
            self.resume(
                brief,
                file=self.facts(brief, remaining_definition_of_done=["widen scope"]),
            )

        scope = self.decide(brief, "scope-change")
        self.assertEqual(scope["kind"], "scope-change")
        self.assertEqual(path.read_bytes(), stored)

    def test_a_decision_without_a_checkpoint_is_refused(self) -> None:
        self.land()
        created = self.resolve()
        brief = self.approved_resolver_dispatch(created)["brief"]
        self.start(brief)
        with self.assertRaisesRegex(CoordinatorError, "checkpointed"):
            self.decide(brief)

    def test_a_lost_session_resumes_the_same_dispatch_without_resetting_the_budget(
        self,
    ) -> None:
        tip = self.land()
        created = self.resolve()
        brief = self.approved_resolver_dispatch(created)["brief"]
        self.start(brief)
        self.checkpoint(brief)
        before = self.budget()

        # A new worker session of the same dispatch, authorized by a rate-limit termination.
        self.resume(brief, termination_reason="rate_limit", trigger=None, file=None)
        coordinator.self_report_dispatch(
            self.branch.args(
                dispatch=brief["dispatch_id"], model="sonnet", worktree=None
            )
        )
        resolved = self.resolve_in_worktree(tip)
        self.submit(brief, self.report(brief, resolved))

        self.assertEqual((before["cycles_spent"], before["remaining"]), (0, 2))
        after = self.budget()
        self.assertEqual((after["cycles_spent"], after["remaining"]), (1, 1))


class BudgetTests(ResolverFixture):
    def budget(self) -> JsonObject:
        return resolver_state.budget(
            self.branch.state_root(),
            coordinator._config(self.branch.repo),
            self.record_id,
        )

    def test_two_automatic_targets_pass_and_a_third_needs_a_human_decision(
        self,
    ) -> None:
        first = self.run_cycle()
        second = self.run_cycle()
        self.land(content="VALUE = 'third'\n")
        neighbour = self.batch_record(self.published["batch_id"])
        head = _git(self.worktree, "rev-parse", "HEAD")

        with self.assertRaisesRegex(CoordinatorError, "automatic cycles are spent"):
            self.resolve()

        budget = self.budget()
        self.assertEqual(budget["spent_targets"], sorted([first, second]))
        self.assertEqual(budget["remaining"], 0)
        [exhausted] = [item for item in self.events() if item["kind"] == "exhausted"]
        self.assertEqual(exhausted["route"], "same-resolver")
        # The branch and the evidence are preserved and no other task was touched.
        self.assertEqual(_git(self.worktree, "rev-parse", "HEAD"), head)
        self.assertEqual(self.batch_record(self.published["batch_id"]), neighbour)
        self.assertTrue(
            list((self.branch.records() / "reports").glob("dispatch-*.json"))
        )

    def test_a_human_decision_extending_the_budget_allows_the_third_target(
        self,
    ) -> None:
        self.run_cycle()
        self.run_cycle()
        self.land(content="VALUE = 'third'\n")
        with self.assertRaises(CoordinatorError):
            self.resolve()
        spent = [item for item in self.events() if item["kind"] == "cycle-spent"]
        coordinator.resolver_event(
            self.branch.args(
                record=self.record_id,
                ticket=None,
                branch=None,
                batch=None,
                kind="human-decision",
                dispatch=spent[-1]["dispatch_id"],
                decided_by="Malove",
                note="one more cycle is worth it",
                option=None,
                extends_budget=True,
            )
        )

        created = self.resolve()

        self.assertEqual(created["state"], "created")
        self.assertEqual(created["budget"]["cycles_total"], 3)
        self.assertEqual(created["budget"]["remaining"], 1)

    def test_a_clean_rebase_spends_no_cycle(self) -> None:
        self.run_cycle()
        spent = self.budget()["cycles_spent"]
        self.land("another.txt", "clean\n")
        with self.assertRaisesRegex(CoordinatorError, "clean"):
            self.resolve()
        self.assertEqual(self.budget()["cycles_spent"], spent)

    def test_fixes_on_the_same_target_spend_no_cycle_and_are_bounded_by_the_retry_budget(
        self,
    ) -> None:
        tip = self.land()
        created = self.resolve()
        first = self.approved_resolver_dispatch(created)["brief"]
        self.start(first)
        resolved = self.resolve_in_worktree(tip)
        self.submit(first, self.report(first, resolved))
        self.fx._decide(created["batch_id"], "retry", reason_category="code")
        self.assertEqual(
            self.batch_record(created["batch_id"])["next_action"], "resolve-conflict"
        )

        second = self.fx._dispatch(created["batch_id"], "conflict-resolver")["brief"]
        self.start(second)
        (self.worktree / CONFLICT_FILE).write_text(
            "VALUE = 'fixed'\n", encoding="utf-8"
        )
        _git(self.worktree, "commit", "-am", "fix the resolution")
        fixed = _git(self.worktree, "rev-parse", "HEAD")
        self.submit(second, self.report(second, fixed))

        self.assertEqual(
            [item["kind"] for item in self.events()],
            ["cycle-spent", "same-target-fix"],
        )
        budget = self.budget()
        self.assertEqual((budget["cycles_spent"], budget["remaining"]), (1, 1))
        # The project retry budget (one by default) is spent: another fix is refused.
        with self.assertRaisesRegex(CoordinatorError, "max_developer_retries"):
            self.fx._decide(created["batch_id"], "retry", reason_category="code")

    def test_a_human_decision_extending_the_budget_allows_another_fix_on_the_same_target(
        self,
    ) -> None:
        tip = self.land()
        created = self.resolve()
        first = self.approved_resolver_dispatch(created)["brief"]
        self.start(first)
        resolved = self.resolve_in_worktree(tip)
        self.submit(first, self.report(first, resolved))
        self.fx._decide(created["batch_id"], "retry", reason_category="code")
        second = self.fx._dispatch(created["batch_id"], "conflict-resolver")["brief"]
        self.start(second)
        (self.worktree / CONFLICT_FILE).write_text(
            "VALUE = 'fixed'\n", encoding="utf-8"
        )
        _git(self.worktree, "commit", "-am", "fix the resolution")
        self.submit(
            second, self.report(second, _git(self.worktree, "rev-parse", "HEAD"))
        )
        with self.assertRaisesRegex(CoordinatorError, "max_developer_retries"):
            self.fx._decide(created["batch_id"], "retry", reason_category="code")

        coordinator.resolver_event(
            self.branch.args(
                record=self.record_id,
                ticket=None,
                branch=None,
                batch=None,
                kind="human-decision",
                dispatch=second["dispatch_id"],
                decided_by="Malove",
                note="one more fix is worth it",
                option=None,
                extends_budget=True,
            )
        )

        self.assertEqual(self.budget()["internal_fix_budget"], 2)
        self.fx._decide(created["batch_id"], "retry", reason_category="code")
        self.assertEqual(
            self.batch_record(created["batch_id"])["next_action"], "resolve-conflict"
        )


class VerificationFailureTests(ResolverFixture):
    """A failed CI check of a refreshed pair is handed to the same resolver (issue #537)."""

    def setUp(self) -> None:
        super().setUp()
        write_project(self.branch.repo, GITHUB, ["lint", "tests"])
        self.tip = self.land("landed.txt", "landed\n")
        self.refreshed = coordinator.integration_refresh(
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

    def observation(self, pair: JsonObject, *checks: CheckRun) -> CiObservation:
        return CiObservation(
            repository=REPO,
            pull_request=PR,
            base_ref="master",
            candidate_sha=pair["candidate_sha"],
            checkout="combined",
            merge_commit_sha=MERGE,
            merge_parents=(pair["candidate_sha"], pair["target_sha"]),
            checks=checks,
        )

    def collect(self, outcome: str) -> JsonObject:
        runs = tuple(
            CheckRun(name, MERGE, "completed", outcome, str(index), None)
            for index, name in enumerate(("lint", "tests"), start=10)
        )
        return coordinator.integration_collect_ci(
            self.branch.args(
                record=self.record_id,
                ticket=None,
                branch=None,
                batch=None,
                pull_request=PR,
                ci_source=FakeSource(self.observation(self.pair(), *runs)),
            )
        )

    def fix(self, name: str) -> str:
        """What a resolver does for a failed verification: one more change on the same target."""
        (self.worktree / CONFLICT_FILE).write_text(
            f"VALUE = {name!r}\n", encoding="utf-8"
        )
        _git(self.worktree, "add", "-A")
        _git(self.worktree, "commit", "-m", f"fix {name}")
        return _git(self.worktree, "rev-parse", "HEAD")

    def fix_cycle(self, name: str) -> str:
        created = self.resolve()
        brief = self.approved_resolver_dispatch(created)["brief"]
        self.start(brief)
        resolved = self.fix(name)
        self.submit(brief, self.report(brief, resolved))
        self.fx._decide(created["batch_id"], "accept")
        coordinator.abandon_batch(
            self.branch.args(
                batch=created["batch_id"],
                reason="the next failure needs a fresh resolver batch",
                **self.fx._approval(),
            )
        )
        return resolved

    def budget(self) -> JsonObject:
        return resolver_state.budget(
            self.branch.state_root(),
            coordinator._config(self.branch.repo),
            self.record_id,
        )

    def test_a_failed_check_of_the_refreshed_pair_creates_a_resolver_batch(
        self,
    ) -> None:
        self.assertEqual(self.collect("failure")["outcome"], "failed")
        head = _git(self.worktree, "rev-parse", "HEAD")

        created = self.resolve()

        self.assertEqual(created["state"], "created")
        self.assertEqual(created["trigger"], "verification-failure")
        self.assertEqual(created["conflicting_files"], [])
        self.assertEqual(created["candidate_sha"], self.refreshed["new_candidate_sha"])
        self.assertEqual(created["target_sha"], self.tip)
        self.assertEqual(created["next_action"], "resolve-conflict")
        self.assertEqual(created["budget"]["remaining"], 2)
        self.assertEqual(_git(self.worktree, "rev-parse", "HEAD"), head)
        self.assertEqual(_git(self.worktree, "status", "--porcelain"), "")
        batch = self.batch_record(created["batch_id"])
        self.assertEqual(batch["kind"], "resolver")
        self.assertEqual(batch["resolver"]["trigger"], "verification-failure")
        self.assertTrue(batch["resolver"]["failed_evidence_ids"])
        self.assertIn(CONFLICT_FILE, batch["allowed_paths"])
        brief = self.approved_resolver_dispatch(created)["brief"]
        self.assertEqual(brief["resolver"]["trigger"], "verification-failure")
        self.assertEqual(
            brief["resolver"]["failed_evidence_ids"],
            batch["resolver"]["failed_evidence_ids"],
        )
        # Asking for a resolver spends no cycle: only a recorded resolver report does.
        self.assertEqual(self.events(), [])

    def test_a_verification_failure_batch_describes_no_conflict_or_rebase(self) -> None:
        self.collect("failure")

        batch = self.batch_record(self.resolve()["batch_id"])

        summary = batch["resolver"]["commit_plan"][0]["summary"]
        for text in [batch["goal"], *batch["definition_of_done"], summary]:
            self.assertNotRegex(
                text.lower(), r"textual conflict|rebase the|every conflict"
            )
        self.assertIn("failed verification", batch["goal"])
        self.assertIn("failed_evidence_ids", " ".join(batch["definition_of_done"]))
        self.assertIn("failed verification", summary)

    def test_a_verification_failure_batch_carries_both_sides_without_widening_scope(
        self,
    ) -> None:
        self.collect("failure")

        batch = self.batch_record(self.resolve()["batch_id"])

        sides = batch["resolver"]["sides"]
        self.assertEqual(sides["target"]["tickets"], ["#901"])
        self.assertEqual(
            sides["target"]["requirements"], ["feat: land landed.txt (#901)"]
        )
        self.assertTrue(sides["candidate"]["requirements"])
        # The scope is not widened by the target's files: that is a scope-change dispatch.
        self.assertEqual(batch["resolver"]["scope"], [CONFLICT_FILE])
        self.assertEqual(batch["allowed_paths"], [CONFLICT_FILE])

    def test_a_conflict_batch_names_its_trigger_too(self) -> None:
        self.land(CONFLICT_FILE, TARGET_TEXT)
        created = self.resolve()
        self.assertEqual(created["trigger"], "conflict")
        self.assertEqual(created["conflicting_files"], [CONFLICT_FILE])
        batch = self.batch_record(created["batch_id"])
        self.assertEqual(batch["resolver"]["failed_evidence_ids"], [])
        self.assertTrue(batch["goal"].startswith("Resolve the textual conflict of"))
        self.assertIn("resolve every conflict", batch["definition_of_done"][0])
        self.assertEqual(
            batch["resolver"]["commit_plan"][0]["summary"],
            "Resolve the conflict preserving both sides and commit the result",
        )

    def test_repeating_the_route_returns_the_open_batch(self) -> None:
        self.collect("failure")
        first = self.resolve()
        self.assertEqual(self.resolve()["batch_id"], first["batch_id"])

    def test_without_a_failed_check_there_is_nothing_to_resolve(self) -> None:
        with self.assertRaisesRegex(CoordinatorError, "no conflict"):
            self.resolve()
        self.assertEqual(self.events(), [])

    def test_a_pending_ci_is_never_a_failure(self) -> None:
        pending = self.observation(
            self.pair(),
            CheckRun("lint", MERGE, "completed", "success", "1", None),
            CheckRun("tests", MERGE, "in_progress", None, "2", None),
        )
        result = coordinator.integration_collect_ci(
            self.branch.args(
                record=self.record_id,
                ticket=None,
                branch=None,
                batch=None,
                pull_request=PR,
                ci_source=FakeSource(pending),
            )
        )
        self.assertEqual(result["outcome"], "fallback")
        with self.assertRaisesRegex(CoordinatorError, "no conflict"):
            self.resolve()

    def test_a_later_passed_check_of_the_pair_supersedes_the_failure(self) -> None:
        self.collect("failure")
        self.collect("success")
        with self.assertRaisesRegex(CoordinatorError, "no conflict"):
            self.resolve()

    def test_a_second_failure_on_the_same_target_is_a_bounded_fix_not_a_new_cycle(
        self,
    ) -> None:
        self.collect("failure")
        first = self.fix_cycle("one")
        self.assertEqual(self.pair()["candidate_sha"], first)
        self.collect("failure")
        self.fix_cycle("two")

        self.assertEqual(
            [item["kind"] for item in self.events()],
            ["cycle-spent", "same-target-fix"],
        )
        budget = self.budget()
        self.assertEqual((budget["cycles_spent"], budget["remaining"]), (1, 1))
        # The project retry budget (one by default) is spent: a third failure stops this task.
        self.collect("failure")
        with self.assertRaisesRegex(CoordinatorError, "internal fix budget is spent"):
            self.resolve()
        [exhausted] = [item for item in self.events() if item["kind"] == "exhausted"]
        self.assertEqual(exhausted["target_sha"], self.tip)

    def test_a_clean_refresh_to_a_new_target_spends_no_cycle(self) -> None:
        self.collect("failure")

        self.land("another.txt", "clean\n")
        coordinator.integration_refresh(
            self.branch.args(
                record=self.record_id, ticket=None, branch=None, batch=None
            )
        )

        self.assertEqual(self.budget()["cycles_spent"], 0)


class CauseRoutingTests(ResolverFixture):
    def retried(self, cause: str) -> JsonObject:
        tip = self.land()
        created = self.resolve()
        brief = self.approved_resolver_dispatch(created)["brief"]
        self.start(brief)
        resolved = self.resolve_in_worktree(tip)
        self.submit(brief, self.report(brief, resolved, outcome="blocked", cause=cause))
        self.fx._decide(created["batch_id"], "retry", reason_category="code")
        batch = self.batch_record(created["batch_id"])
        routing: JsonObject = batch["coordinator_decisions"][-1]["routing"]
        return routing

    def test_an_integration_incompatibility_continues_with_the_same_resolver(
        self,
    ) -> None:
        routing = self.retried("integration-incompatibility")
        self.assertEqual(routing["next_action"], "resolve-conflict")
        self.assertEqual(routing["next_role"], "conflict-resolver")

    def test_the_tickets_own_defect_returns_to_a_regular_developer(self) -> None:
        routing = self.retried("task-defect")
        self.assertEqual(routing["route"], "developer-retry")
        self.assertEqual(routing["next_role"], "developer")

    def test_the_exhaustion_route_follows_the_last_cause(self) -> None:
        self.run_cycle()
        self.land(content="VALUE = 'two'\n")
        created = self.resolve()
        brief = self.approved_resolver_dispatch(created)["brief"]
        self.start(brief)
        # The resolver found the ticket's own defect and left the branch as it was.
        unresolved = _git(self.worktree, "rev-parse", "HEAD")
        self.submit(
            brief,
            self.report(brief, unresolved, outcome="blocked", cause="task-defect"),
        )
        self.fx._decide(created["batch_id"], "fail")
        self.land(content="VALUE = 'three'\n")
        with self.assertRaises(CoordinatorError) as raised:
            self.resolve()
        [exhausted] = [item for item in self.events() if item["kind"] == "exhausted"]
        self.assertEqual(exhausted["cause"], "task-defect")
        self.assertEqual(exhausted["route"], "developer-retry")
        self.assertIn("regular developer", raised.exception.remedy)


if __name__ == "__main__":
    unittest.main()
