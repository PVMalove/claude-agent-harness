"""The verification plan in dispatch briefs and approvals (ADR 0023, sections 2 and 6).

With `verification_plan_policy` frozen in the batch, `dispatch propose` builds a plan for a
developer-retry, a code-review and a QA brief, puts it into the brief and binds its digest to the
approval as `verification_plan_sha256`. A batch without the policy keeps the brief it always had.
Everything runs through the real coordinator, ledger and git repository; only the offline parser
bundle, which the suite does not install, is replaced by a regex parser.
"""

from __future__ import annotations

import copy
import json
import subprocess
import unittest
from unittest import mock

from harness.orchestration import operational_guards, verification_plan
from harness.orchestration.core.utils import JsonObject
from harness.repo_map import change_impact
from tests.orchestration import test_coordinator as coordinator_tests
from tests.repo_map.test_change_impact import RegexBackend

POLICY: JsonObject = {
    "max_related_tests": 5,
    "stages": [
        {
            "stage_id": "lint",
            "kind": "lint",
            "argv": ["make", "lint"],
            "timeout_seconds": 600,
        },
        {
            "stage_id": "tests",
            "kind": "pytest",
            "argv": ["python", "-m", "pytest", "-q"],
            "timeout_seconds": 1800,
        },
    ],
}
OTHER_POLICY: JsonObject = {
    "max_related_tests": 9,
    "stages": [
        {
            "stage_id": "everything",
            "kind": "custom",
            "argv": ["make", "verify"],
            "timeout_seconds": 60,
        }
    ],
}


def _stage(plan: JsonObject, stage_id: str) -> JsonObject:
    return next(item for item in plan["stages"] if item["stage_id"] == stage_id)


class VerificationPlanDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = coordinator_tests.CoordinatorRetryRoutingTests()
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self._write_test()
        # The suite installs no parser bundle: a regex parser stands in for it.
        parser = mock.patch.object(
            change_impact,
            "BundleParserBackend",
            side_effect=lambda repo, _policy: RegexBackend(repo),
        )
        parser.start()
        self.addCleanup(parser.stop)

    def _write_test(self) -> None:
        tests = self.fx.repo / "tests"
        tests.mkdir(exist_ok=True)
        (tests / "test_x.py").write_text(
            "import services.x\n\n\ndef test_value():\n    assert services.x.VALUE\n",
            encoding="utf-8",
        )
        for arguments in (
            ("add", "-A"),
            ("commit", "-m", "test: related test"),
            ("push", "origin", "master"),
        ):
            subprocess.run(
                ["git", "-C", str(self.fx.repo), *arguments],
                check=True,
                capture_output=True,
            )

    def _configure(self, policy: JsonObject | None) -> None:
        config: JsonObject = {
            "provider_profiles": {},
            "assignment_plans": {},
            "concurrency_budget": 1,
            "verification_commands": ["make verify"],
            "developer_verification_commands": ["make dev"],
            "review_verification_commands": ["make review"],
            "auto_retry_min_severity": "info",
        }
        if policy is not None:
            config["verification_plan_policy"] = policy
        (self.fx.repo / ".harness" / "orchestration.json").write_text(
            json.dumps(config), encoding="utf-8"
        )

    def _briefs(self) -> JsonObject:
        """First developer, review (with a finding), developer-retry, then QA briefs."""
        fx = self.fx
        batch_id = fx._create_batch()["batch_id"]
        fx._accepted_architect(batch_id)
        first = fx._dispatch(batch_id, "developer")["brief"]
        fx._start(first["dispatch_id"])
        candidate, changed = fx._developer_commit("x")
        fx._submit(
            first["dispatch_id"], fx._developer_report(first, candidate, changed)
        )
        fx._decide(batch_id, "accept")
        fx._assess(batch_id, candidate, changed)
        finding = [
            {"severity": "warning", "summary": "off-by-one", "evidence": "x.py:1"}
        ]
        review = fx._reported_review(
            batch_id, candidate, standards=("warning", finding)
        )
        fx._decide(batch_id, "retry", reason_category="code")
        retry = fx._dispatch(batch_id, "developer")["brief"]
        fx._start(retry["dispatch_id"])
        fixed, fixed_files = fx._developer_commit("y")
        fx._submit(
            retry["dispatch_id"], fx._developer_report(retry, fixed, fixed_files)
        )
        fx._decide(batch_id, "accept")
        fx._assess(batch_id, fixed, fixed_files)
        clean = fx._reported_review(batch_id, fixed)
        fx._decide(batch_id, "accept")
        qa = fx._dispatch(batch_id, "qa", candidate=fixed)["brief"]
        return {
            "batch_id": batch_id,
            "first": first,
            "review": review,
            "retry": retry,
            "clean_review": clean,
            "qa": qa,
        }

    def test_the_developer_retry_review_and_qa_briefs_carry_a_plan(self) -> None:
        self._configure(POLICY)

        briefs = self._briefs()

        retry, review, qa = briefs["retry"], briefs["clean_review"], briefs["qa"]
        self.assertEqual(retry["verification_plan"]["role"], "developer")
        self.assertEqual(
            retry["verification_plan"]["candidate_sha"], retry["snapshot_commit"]
        )
        self.assertEqual(
            _stage(retry["verification_plan"], "tests")["selection"], "focused"
        )
        self.assertEqual(
            _stage(retry["verification_plan"], "tests")["nodeids"], ["tests/test_x.py"]
        )
        self.assertEqual(review["verification_plan"]["role"], "code-review")
        self.assertEqual(
            review["verification_plan"]["candidate_sha"], review["candidate_commit"]
        )
        self.assertEqual(
            _stage(review["verification_plan"], "tests")["selection"], "focused"
        )
        self.assertEqual(qa["verification_plan"]["role"], "qa")
        self.assertEqual(_stage(qa["verification_plan"], "tests")["selection"], "full")

    def test_the_plan_digest_is_bound_to_the_approval(self) -> None:
        self._configure(POLICY)

        briefs = self._briefs()

        for name in ("retry", "clean_review", "qa"):
            with self.subTest(brief=name):
                brief = briefs[name]
                self.assertEqual(
                    brief["transition"]["verification_plan_sha256"],
                    verification_plan.plan_sha256(brief["verification_plan"]),
                )
                self.assertEqual(
                    brief["coordinator_approval"]["transition_digest"],
                    brief["transition_digest"],
                )

    def test_the_commands_the_role_reports_stay_the_brief_commands(self) -> None:
        self._configure(POLICY)

        briefs = self._briefs()

        self.assertEqual(
            briefs["clean_review"]["verification_commands"], ["make review"]
        )
        self.assertEqual(briefs["qa"]["verification_commands"], ["make verify"])

    def test_the_first_developer_brief_has_no_diff_and_no_plan(self) -> None:
        self._configure(POLICY)

        briefs = self._briefs()

        self.assertNotIn("verification_plan", briefs["first"])
        self.assertNotIn("verification_plan_sha256", briefs["first"]["transition"])

    def test_a_batch_without_the_policy_keeps_its_briefs_unchanged(self) -> None:
        self._configure(None)

        briefs = self._briefs()

        for name in ("first", "retry", "clean_review", "qa"):
            with self.subTest(brief=name):
                self.assertNotIn("verification_plan", briefs[name])
                self.assertNotIn("verification_plan_sha256", briefs[name]["transition"])

    def _assessed_candidate(self) -> tuple[str, str]:
        fx = self.fx
        batch_id = fx._create_batch()["batch_id"]
        fx._accepted_architect(batch_id)
        first = fx._dispatch(batch_id, "developer")["brief"]
        fx._start(first["dispatch_id"])
        candidate, changed = fx._developer_commit("x")
        fx._submit(
            first["dispatch_id"], fx._developer_report(first, candidate, changed)
        )
        fx._decide(batch_id, "accept")
        fx._assess(batch_id, candidate, changed)
        return batch_id, candidate

    def test_a_proposal_shows_the_plan_its_digest_binds(self) -> None:
        self._configure(POLICY)
        batch_id, candidate = self._assessed_candidate()

        proposal = self.fx._propose(batch_id, "code-review", candidate=candidate)

        plan = proposal["verification_plan"]
        self.assertEqual(plan["candidate_sha"], candidate)
        self.assertEqual(
            proposal["transition"]["verification_plan_sha256"],
            verification_plan.plan_sha256(plan),
        )

    def test_a_proposal_without_the_policy_has_no_plan(self) -> None:
        self._configure(None)
        batch_id, candidate = self._assessed_candidate()

        proposal = self.fx._propose(batch_id, "code-review", candidate=candidate)

        self.assertNotIn("verification_plan", proposal)
        self.assertNotIn("verification_plan_sha256", proposal["transition"])

    def test_a_config_edit_after_batch_create_does_not_change_the_plan(self) -> None:
        self._configure(POLICY)
        fx = self.fx
        batch_id, candidate = self._assessed_candidate()
        self._configure(OTHER_POLICY)

        review = fx._reported_review(batch_id, candidate)

        self.assertEqual(
            [stage["stage_id"] for stage in review["verification_plan"]["stages"]],
            ["lint", "tests"],
        )
        self.assertEqual(review["verification_plan"]["max_related_tests"], 5)

    def test_a_tampered_plan_no_longer_matches_its_approval(self) -> None:
        self._configure(POLICY)
        briefs = self._briefs()
        brief = briefs["clean_review"]
        batch_id = briefs["batch_id"]

        operational_guards.validate_binding(brief, batch_id)
        tampered = copy.deepcopy(brief)
        _stage(tampered["verification_plan"], "tests")["selection"] = "failed-only"

        with self.assertRaises(operational_guards.GuardError) as refused:
            operational_guards.validate_binding(tampered, batch_id)
        self.assertIn("verification plan", refused.exception.message)

    def test_a_brief_whose_plan_is_missing_from_the_transition_is_refused(self) -> None:
        self._configure(POLICY)
        briefs = self._briefs()
        brief = copy.deepcopy(briefs["qa"])
        del brief["transition"]["verification_plan_sha256"]
        brief["transition_digest"] = operational_guards.transition_digest(
            brief["transition"]
        )
        brief["coordinator_approval"]["transition_digest"] = brief["transition_digest"]

        with self.assertRaises(operational_guards.GuardError):
            operational_guards.validate_binding(brief, briefs["batch_id"])


class VerificationPlanShapeTests(unittest.TestCase):
    PLAN: JsonObject = {
        "role": "qa",
        "stages": [
            {
                "stage_id": "tests",
                "kind": "pytest",
                "argv": ["pytest"],
                "timeout_seconds": 60,
                "selection": "full",
                "nodeids": [],
                "environment_fingerprint": "0" * 64,
            }
        ],
    }

    def test_a_well_formed_plan_has_no_problems(self) -> None:
        self.assertEqual(verification_plan.plan_problems(self.PLAN), [])

    def test_a_malformed_plan_is_named(self) -> None:
        def broken(**changes: object) -> JsonObject:
            plan = copy.deepcopy(self.PLAN)
            plan["stages"][0].update(changes)
            return plan

        cases: dict[str, object] = {
            "not an object": ["tests"],
            "unknown role": {**self.PLAN, "role": "architect"},
            "no stages": {**self.PLAN, "stages": []},
            "unregistered selection": broken(selection="partial"),
            "unregistered kind": broken(kind="docs"),
            "nodeids not strings": broken(nodeids=[1]),
            "missing field": {
                **self.PLAN,
                "stages": [{"stage_id": "tests", "kind": "pytest"}],
            },
        }
        for case, plan in cases.items():
            with self.subTest(case=case):
                self.assertNotEqual(verification_plan.plan_problems(plan), [])


if __name__ == "__main__":
    unittest.main()
