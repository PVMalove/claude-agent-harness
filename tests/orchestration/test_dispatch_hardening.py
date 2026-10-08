"""Hardening of dispatch creation: explicit invariants and immutable policy-retry evidence.

``assert`` statements vanish when Python runs with ``-O``, so a production invariant must raise a
``HarnessError`` with ``INTERNAL_INVARIANT_REMEDY`` instead (ADR 0007).  A policy-approved
infrastructure retry rests on the immutable report of the failed QA, so that report is verified
against its recorded digest before it authorizes a new brief.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest import mock

import pytest

from harness.orchestration import coordinator
from harness.orchestration.core.utils import CoordinatorError
from tests.orchestration.test_qa_preparation_e2e import PreparationFixture

WORKFLOW = (
    Path(__file__).resolve().parents[2] / "harness" / "orchestration" / "workflow"
)


@pytest.mark.parametrize(
    "module",
    ["dispatch", "integration", "batch", "commit_plan", "delivery", "resolver"],
)
def test_the_workflow_module_guards_invariants_without_assert(module: str) -> None:
    tree = ast.parse((WORKFLOW / f"{module}.py").read_text(encoding="utf-8"))

    asserts = [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Assert)]

    assert asserts == []


class PolicyRetryEvidenceTests(PreparationFixture):
    def test_a_policy_retry_refuses_a_report_changed_after_its_decision(self) -> None:
        self.configure(preparation=[self.prepare])
        path = self.repo / ".harness/orchestration.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["infrastructure_retry_policy"] = {"enabled": True}
        path.write_text(json.dumps(config), encoding="utf-8")
        first = self.qa_brief()
        self.registry_down.touch()
        blocked = self.run_qa(first)
        self.registry_down.unlink()
        # The policy decision is recorded, and the next brief is not created yet.
        with mock.patch(
            "harness.orchestration.workflow.completion.create_dispatch",
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                coordinator.complete_report(
                    self.fx._args(dispatch=first["dispatch_id"])
                )
        report_path = Path(blocked["report"])
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["risks"] = "rewritten after the policy decision"
        report_path.write_text(json.dumps(report), encoding="utf-8")

        with self.assertRaises(CoordinatorError) as raised:
            coordinator.create_dispatch(
                self.fx._args(
                    batch=self.batch_id,
                    role=first["role"],
                    runtime=first["resolved_runtime"],
                    purpose=first["purpose"],
                    candidate_commit=first["candidate_commit"],
                    delta_review_of=None,
                    model=first["resolved_model"],
                    effort=first["resolved_effort"],
                    propose=False,
                    transition_digest=None,
                    approved_by=None,
                    approved_at=None,
                    _policy_infrastructure_retry=True,
                )
            )

        self.assertIn("immutable integrity check", raised.exception.message)
