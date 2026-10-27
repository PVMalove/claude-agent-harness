"""Strict fail-closed boundaries of the hook profile (ADR 0024, section 2a; issue #802).

Under the strict profile, `dispatch propose` and `dispatch create` for code-review and QA, `qa run`
and `dispatch publish` refuse a candidate that changes a sensitive path (ADR 0024, section 2) unless
a VerificationPlan proves the ImpactSet (ADR 0023) for it. The profile comes only from hook_policy
in `.harness/project.json`; a running dispatch keeps the policy pinned by its hook_policy_sha256.
Everything runs through the coordinator CLI, the real ledger and the real git repository.
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

from harness.orchestration import coordinator, hook_policy, qa_lane
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.ledger.lifecycle import LifecycleLedger
from tests.orchestration.test_verification_plan_dispatch import POLICY, PlanFixture

SENSITIVE = "services/auth.py"


class StrictBoundaryFixture(PlanFixture):
    """A coordinator fixture whose candidate may change a sensitive path."""

    def _hook_policy(self, policy: JsonObject | None) -> None:
        path = self.fx.repo / ".harness" / "project.json"
        project = json.loads(path.read_text(encoding="utf-8"))
        if policy is None:
            project.pop("hook_policy", None)
        else:
            project["hook_policy"] = policy
        path.write_text(json.dumps(project), encoding="utf-8")

    def _cli(self, *argv: str) -> tuple[int, str, str]:
        fx = self.fx
        out, err = io.StringIO(), io.StringIO()
        # The QA lane is repository-scoped: it refuses --state-dir and uses the repository state.
        repository_scoped = fx.state_dir == fx.repo / coordinator.STATE_REL
        state = [] if repository_scoped else ["--state-dir", str(fx.state_dir)]
        command = ["coordinator.py", "--repo", str(fx.repo), *state, *argv]
        with (
            mock.patch.object(sys, "argv", command),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = coordinator.main()
        return code, out.getvalue(), err.getvalue()

    def _propose_cli(
        self, batch_id: str, role: str, candidate: str
    ) -> tuple[int, str, str]:
        return self._cli(
            "dispatch", "propose", "--batch", batch_id, "--role", role,
            "--candidate-commit", candidate, "--runtime", "claude",
            "--model", "sonnet", "--effort", "high",
        )  # fmt: skip

    def _create_cli(
        self, batch_id: str, role: str, candidate: str
    ) -> tuple[int, str, str]:
        return self._cli(
            "dispatch", "create", "--batch", batch_id, "--role", role,
            "--candidate-commit", candidate, "--runtime", "claude",
            "--model", "sonnet", "--effort", "high",
            "--approved-by", "Malove", "--approved-at", self.fx.APPROVED_AT,
            "--transition-digest", "0" * 64,
        )  # fmt: skip

    def _candidate(self, name: str = "auth") -> tuple[str, str]:
        """An accepted, assessed developer candidate that adds ``services/<name>.py``."""
        fx = self.fx
        batch_id = fx._create_batch()["batch_id"]
        fx._accepted_architect(batch_id)
        first = fx._dispatch(batch_id, "developer")["brief"]
        fx._start(first["dispatch_id"])
        candidate, changed = fx._developer_commit(name)
        fx._submit(
            first["dispatch_id"], fx._developer_report(first, candidate, changed)
        )
        fx._decide(batch_id, "accept")
        fx._assess(batch_id, candidate, changed)
        return batch_id, candidate

    def _accepted_review(self, batch_id: str, candidate: str) -> None:
        self.fx._reported_review(batch_id, candidate)
        self.fx._decide(batch_id, "accept")

    @contextlib.contextmanager
    def _boundary_of_an_older_coordinator(self) -> Iterator[None]:
        """Approve as a coordinator without this check did: the brief pins the policy unchecked."""
        with mock.patch.object(hook_policy, "check_strict_boundary"):
            yield

    def _lease_path(self) -> Path:
        root = LifecycleLedger(self.fx.state_dir).records_root()
        return root / "qa-lane" / "lease.json"


class ProposeBoundaryTests(StrictBoundaryFixture):
    def test_strict_refuses_a_review_of_a_sensitive_change_without_a_plan(
        self,
    ) -> None:
        self._configure(None)
        batch_id, candidate = self._candidate()
        self._hook_policy({"profile": "strict"})
        before = self.fx._batch_record(batch_id)

        code, out, err = self._propose_cli(batch_id, "code-review", candidate)

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("strict_impact_unknown", err)
        self.assertIn(SENSITIVE, err)
        self.assertIn("verification_plan_policy", err)
        self.assertEqual(self.fx._batch_record(batch_id), before)

    def test_strict_refuses_a_qa_proposal_and_a_created_dispatch_writes_no_brief(
        self,
    ) -> None:
        self._configure(None)
        batch_id, candidate = self._candidate()
        self._accepted_review(batch_id, candidate)
        self._hook_policy({"profile": "strict"})
        before = self.fx._batch_record(batch_id)["dispatches"]

        proposed = self._propose_cli(batch_id, "qa", candidate)
        created = self._create_cli(batch_id, "qa", candidate)

        for code, _, err in (proposed, created):
            self.assertEqual(code, 2)
            self.assertIn("strict_impact_unknown", err)
        self.assertEqual(self.fx._batch_record(batch_id)["dispatches"], before)

    def test_strict_passes_a_sensitive_change_with_a_verification_plan(self) -> None:
        self._configure(POLICY)
        batch_id, candidate = self._candidate()
        self._hook_policy({"profile": "strict"})

        review = self._propose_cli(batch_id, "code-review", candidate)
        self._accepted_review(batch_id, candidate)
        qa = self._propose_cli(batch_id, "qa", candidate)

        self.assertEqual(review[0], 0, review[2])
        self.assertEqual(qa[0], 0, qa[2])

    def test_strict_passes_a_change_without_sensitive_paths(self) -> None:
        self._configure(None)
        batch_id, candidate = self._candidate("x")
        self._hook_policy({"profile": "strict"})

        code, _, err = self._propose_cli(batch_id, "code-review", candidate)

        self.assertEqual(code, 0, err)

    def test_minimal_and_standard_keep_todays_behaviour(self) -> None:
        for policy in (None, {"profile": "minimal"}, {"profile": "standard"}):
            with self.subTest(policy=policy):
                self.fx._reset()
                self._write_test()
                self._configure(None)
                batch_id, candidate = self._candidate()
                self._hook_policy(policy)

                code, _, err = self._propose_cli(batch_id, "code-review", candidate)

                self.assertEqual(code, 0, err)

    def test_an_escalation_applies_to_the_policy_role_of_the_dispatch(self) -> None:
        cases = (("review", 2), ("qa", 0), ("developer", 0))
        for role, expected in cases:
            with self.subTest(role=role):
                self.fx._reset()
                self._write_test()
                self._configure(None)
                batch_id, candidate = self._candidate()
                escalation = {
                    "roles": [role],
                    "paths": ["services/*"],
                    "profile": "strict",
                }
                self._hook_policy({"profile": "standard", "escalations": [escalation]})

                code, _, err = self._propose_cli(batch_id, "code-review", candidate)

                self.assertEqual(code, expected, err)


class QaRunBoundaryTests(StrictBoundaryFixture):
    def _approved_qa(self, policy: JsonObject) -> str:
        # The QA lane is repository-scoped: its ledger is the repository's own state directory.
        self.fx.state_dir = self.fx.repo / coordinator.STATE_REL
        self._configure(None)
        batch_id, candidate = self._candidate()
        self._accepted_review(batch_id, candidate)
        self._hook_policy(policy)
        with self._boundary_of_an_older_coordinator():
            brief = self.fx._dispatch(batch_id, "qa", candidate=candidate)["brief"]
        return str(brief["dispatch_id"])

    def test_qa_run_refuses_a_pinned_strict_dispatch_without_a_plan_before_the_lease(
        self,
    ) -> None:
        dispatch_id = self._approved_qa({"profile": "strict"})

        with mock.patch.object(qa_lane, "acquire") as acquire:
            code, _, err = self._cli("qa", "run", "--dispatch", dispatch_id)

        self.assertEqual(code, 2)
        self.assertIn("strict_impact_unknown", err)
        self.assertIn("qa run", err)
        acquire.assert_not_called()
        self.assertFalse(self._lease_path().exists())
        status = coordinator._load_dispatch_status(self.fx.state_dir, dispatch_id)
        self.assertEqual(status["state"], "approved")

    def test_an_edit_of_project_json_after_approval_does_not_change_the_decision(
        self,
    ) -> None:
        reached = CoordinatorError(
            "sentinel: the lease was requested", remedy="none: a test sentinel"
        )
        for pinned, current, refused in (
            ({"profile": "standard"}, {"profile": "strict"}, False),
            ({"profile": "strict"}, {"profile": "minimal"}, True),
        ):
            with self.subTest(pinned=pinned):
                self.fx._reset()
                self._write_test()
                dispatch_id = self._approved_qa(pinned)
                self._hook_policy(current)

                with mock.patch.object(qa_lane, "acquire", side_effect=reached):
                    code, _, err = self._cli("qa", "run", "--dispatch", dispatch_id)

                self.assertEqual(code, 2)
                self.assertEqual("strict_impact_unknown" in err, refused, err)
                self.assertEqual("sentinel" in err, not refused, err)


class PublishBoundaryTests(StrictBoundaryFixture):
    def _publish_brief(self, *, plan: bool) -> tuple[str, str]:
        """QA accepted under standard, then the project moves to strict before publish."""
        self._configure(POLICY if plan else None)
        batch_id, candidate = self._candidate()
        self._accepted_review(batch_id, candidate)
        self.fx._reported_qa(batch_id, candidate)
        self.fx._decide(batch_id, "accept")
        self._hook_policy({"profile": "strict"})
        brief = self.fx._dispatch(
            batch_id, "developer", purpose="publish", candidate=candidate
        )["brief"]
        return str(brief["dispatch_id"]), candidate

    def _pushed(self, candidate: str) -> bool:
        heads = subprocess.run(
            ["git", "-C", str(self.fx.repo), "ls-remote", "--heads", "origin"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return candidate in heads

    def test_publish_refuses_a_sensitive_candidate_whose_qa_has_no_plan(self) -> None:
        dispatch_id, candidate = self._publish_brief(plan=False)

        code, _, err = self._cli(
            "dispatch", "publish", "--dispatch", dispatch_id, "--remote", "origin"
        )

        self.assertEqual(code, 2)
        self.assertIn("strict_impact_unknown", err)
        self.assertIn("publish", err)
        self.assertFalse(self._pushed(candidate))

    def test_publish_passes_when_the_accepted_qa_carried_a_full_plan(self) -> None:
        dispatch_id, candidate = self._publish_brief(plan=True)

        code, _, err = self._cli(
            "dispatch", "publish", "--dispatch", dispatch_id, "--remote", "origin"
        )

        self.assertEqual(code, 0, err)
        self.assertTrue(self._pushed(candidate))


class DispatchPolicyTests(StrictBoundaryFixture):
    def test_a_brief_before_the_pinned_policy_gets_the_default_profile(self) -> None:
        self.assertEqual(
            hook_policy.dispatch_policy({"role": "qa"}),
            hook_policy.normalize_policy(None),
        )

    def test_a_pinned_digest_that_does_not_match_the_snapshot_is_refused(
        self,
    ) -> None:
        self._configure(None)
        batch_id, candidate = self._candidate()
        self._accepted_review(batch_id, candidate)
        brief = self.fx._dispatch(batch_id, "qa", candidate=candidate)["brief"]
        tampered = {**brief, "hook_policy_sha256": "0" * 64}

        with self.assertRaisesRegex(CoordinatorError, "hook_policy_sha256"):
            hook_policy.dispatch_policy(tampered)
        self.assertEqual(
            hook_policy.dispatch_policy(brief), hook_policy.normalize_policy(None)
        )


def _plan(unknown: list[JsonObject], selection: str = "focused") -> JsonObject:
    return {
        "impact": {"unknown": unknown, "related_tests": []},
        "stages": [{"kind": "pytest", "selection": selection, "nodeids": []}],
    }


class RequireStrictBoundaryTests(StrictBoundaryFixture):
    """The pure decision behind the three boundaries."""

    STRICT = hook_policy.normalize_policy({"profile": "strict"})

    def _decide(self, changed: list[str], plan: JsonObject | None) -> str | None:
        try:
            hook_policy.require_strict_boundary(
                self.STRICT, role="review", boundary="test", changed=changed, plan=plan
            )
        except CoordinatorError as exc:
            return exc.message
        return None

    def test_a_plan_passes_unless_an_unknown_reason_names_a_sensitive_path(
        self,
    ) -> None:
        changed = [SENSITIVE, "docs/a.md"]
        other = {"code": "non-python-file", "path": "docs/a.md", "detail": ""}
        own = {"code": "parse-failed", "path": SENSITIVE, "detail": ""}
        blind = {"code": "graph-degraded", "path": "", "detail": ""}

        self.assertIsNone(self._decide(changed, _plan([])))
        self.assertIsNone(self._decide(changed, _plan([other])))
        self.assertIsNone(self._decide(changed, _plan([own], selection="full")))
        for unknown in ([own], [blind]):
            refused = self._decide(changed, _plan(unknown))
            self.assertIsNotNone(refused)
            self.assertIn("strict_impact_unknown", str(refused))
        self.assertIsNotNone(self._decide(changed, None))

    def test_the_refusal_names_at_most_twenty_sensitive_paths(self) -> None:
        changed = [f"migrations/{index:04}.py" for index in range(25)]

        refused = str(self._decide(changed, None))

        self.assertIn("25 sensitive path(s)", refused)
        self.assertIn("migrations/0019.py", refused)
        self.assertNotIn("migrations/0020.py", refused)
