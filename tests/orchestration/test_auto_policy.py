#!/usr/bin/env python3
"""``approval_policy: auto``: the automatic path from batch approve to accepted publish (issue #643).

The end-to-end run on real Git and a real ledger lives in ``test_coordinator.py``; these tests pin
the parts on data: the configuration gate, the hashed ledger records and their validation, the
decision table, the closed stop list and the final report.
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from harness.orchestration.core import config
from harness.orchestration.core.constants import AUTO_REPORT_FIELDS
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.workflow import approval, dispatch, history

MOMENT = "2026-10-08T00:00:00+00:00"


def _approved_batch() -> JsonObject:
    """A batch whose plan, one dispatch and one decision were all approved by ``policy:auto``."""
    batch: JsonObject = {
        "batch_id": "batch-1",
        "approval_policy": "auto",
        "coordinator_approval": {"approved_by": "policy:auto", "approved_at": MOMENT},
        "dispatches": [
            {
                "dispatch_id": "dispatch-1",
                "role": "architect",
                "state": "reported",
                "brief_sha256": "b" * 64,
                "report": "reports/dispatch-1.json",
                "report_sha256": "r" * 64,
                "decision": {"decision": "accept", "approved_by": "policy:auto"},
            }
        ],
        "coordinator_decisions": [
            {
                "dispatch_id": "dispatch-1",
                "decision": "carry-over",
                "approved_by": "policy:auto",
            }
        ],
    }
    for kind, dispatch_id, evidence in (
        (
            "batch-approve",
            None,
            {
                "plan_sha256": "p" * 64,
                "scope_preflight_status": "ok",
                "definition_of_done_items": 1,
            },
        ),
        (
            "dispatch",
            "dispatch-1",
            {
                "transition_digest": "t" * 64,
                "brief_sha256": "b" * 64,
                "lifted_milestones": [],
                "route_preview": None,
                "reason_category": None,
                "report_sha256": None,
            },
        ),
        (
            "decision",
            "dispatch-1",
            {
                "decision": "accept",
                "report_sha256": "r" * 64,
                "route_preview": None,
                "reason_category": None,
                "basis": "clean report",
                "accepted_risks": [],
                "commit_plan_sha256": None,
                "bug_ticket": None,
                "carried_item_ids": [],
            },
        ),
        (
            "carry-over",
            "dispatch-1",
            {"report_sha256": "r" * 64, "carried_item_ids": ["coordinator-finding-1"]},
        ),
    ):
        approval.record_auto(
            batch,
            kind=kind,
            dispatch_id=dispatch_id,
            rationale=f"{kind} by policy",
            evidence=evidence,
            moment=MOMENT,
        )
    return batch


def _stop(**overrides: object) -> JsonObject:
    return approval.sealed(
        {
            "category": "no-automatic-route",
            "reason": "unknown-reason",
            "detected_at": MOMENT,
            "detected_by": "batch auto-decide",
            "evidence": {"dispatch_id": "dispatch-1"},
            **overrides,
        }
    )


def _report(stop: JsonObject | None) -> JsonObject:
    body: JsonObject = {
        field: None for field in AUTO_REPORT_FIELDS if field != "record_sha256"
    }
    body.update({"batch_id": "batch-1", "stop": stop, "outcome": "stopped"})
    return approval.sealed(body)


class AutoConfigGateTests(unittest.TestCase):
    def _repo(self, policy: dict[str, object]) -> Path:
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(repo, ignore_errors=True))
        (repo / ".harness").mkdir()
        (repo / ".harness/project.json").write_text("{}", encoding="utf-8")
        (repo / ".harness/orchestration.json").write_text(
            json.dumps({"access_policy": {"defaults": {"mode": "inherit"}}, **policy}),
            encoding="utf-8",
        )
        return repo

    def test_approval_policy_refuses_auto_under_a_tty_gate(self) -> None:
        with self.assertRaises(CoordinatorError) as refused:
            config._approval_policy(
                {"approval_policy": "auto", "human_approval_gate": "tty"}
            )
        self.assertIn("human_approval_gate", refused.exception.message)
        self.assertIn("trusted", refused.exception.remedy)
        for policy in ("manual_all", "milestone", "low_risk"):
            with self.subTest(policy=policy):
                self.assertEqual(
                    config._approval_policy(
                        {"approval_policy": policy, "human_approval_gate": "tty"}
                    ),
                    policy,
                )

    def test_config_validation_refuses_an_incompatible_auto_config(self) -> None:
        for policy, problem in (
            ({"approval_policy": "auto"}, "worker_attestation_required"),
            (
                {"approval_policy": "auto", "worker_attestation_required": False},
                "worker_attestation_required",
            ),
            (
                {
                    "approval_policy": "auto",
                    "worker_attestation_required": True,
                    "human_approval_gate": "tty",
                },
                "human_approval_gate",
            ),
        ):
            with (
                self.subTest(policy=policy),
                self.assertRaises(CoordinatorError) as refused,
            ):
                config._config(self._repo(policy))
            self.assertIn(problem, refused.exception.message)

    def test_config_validation_accepts_a_compatible_auto_config(self) -> None:
        loaded = config._config(
            self._repo(
                {
                    "approval_policy": "auto",
                    "worker_attestation_required": True,
                    "human_approval_gate": "trusted",
                }
            )
        )
        self.assertEqual(config._approval_policy(loaded), "auto")


class AutoLedgerRecordTests(unittest.TestCase):
    def test_record_auto_appends_a_sequenced_hashed_record(self) -> None:
        batch = _approved_batch()
        records = batch["auto_decisions"]
        self.assertEqual([item["sequence"] for item in records], [1, 2, 3, 4])
        self.assertEqual(
            {item["approved_by"] for item in records}, {approval.AUTO_APPROVER}
        )
        history._validate_operational_batch_fields(batch)

    def test_a_batch_without_auto_records_stays_valid(self) -> None:
        history._validate_operational_batch_fields(
            {"batch_id": "batch-1", "dispatches": []}
        )

    def test_record_auto_refuses_an_unknown_kind_or_evidence_shape(self) -> None:
        for kind, evidence in (
            ("merge", {}),
            ("carry-over", {"report_sha256": "r" * 64}),
        ):
            with (
                self.subTest(kind=kind),
                self.assertRaises(CoordinatorError),
            ):
                approval.record_auto(
                    {},
                    kind=kind,
                    dispatch_id=None,
                    rationale="x",
                    evidence=evidence,
                    moment=MOMENT,
                )

    def test_validation_refuses_a_modified_or_misplaced_record(self) -> None:
        cases = {
            "rationale": lambda batch: batch["auto_decisions"][0].update(
                rationale="edited"
            ),
            "sequence": lambda batch: batch["auto_decisions"].pop(0),
            "brief": lambda batch: batch["dispatches"][0].update(brief_sha256="c" * 64),
            "decision approver": lambda batch: batch["dispatches"][0][
                "decision"
            ].update(approved_by="Malove"),
            "batch approver": lambda batch: batch["coordinator_approval"].update(
                approved_by="Malove"
            ),
            "carry-over": lambda batch: batch["coordinator_decisions"].clear(),
        }
        for name, change in cases.items():
            with self.subTest(change=name):
                batch = _approved_batch()
                change(batch)
                with self.assertRaises(CoordinatorError) as refused:
                    history._validate_operational_batch_fields(batch)
                self.assertIn("auto_decisions", refused.exception.message)

    def test_validation_accepts_only_a_listed_sealed_stop(self) -> None:
        batch = {"batch_id": "batch-1", "auto_stop": _stop()}
        history._validate_operational_batch_fields(batch)
        for stop in (
            _stop(reason="human-preference"),
            _stop(category="tired"),
            {**_stop(), "reason": "abandon-dead-end"},
        ):
            with (
                self.subTest(stop=stop),
                self.assertRaises(CoordinatorError) as refused,
            ):
                history._validate_operational_batch_fields(
                    {"batch_id": "batch-1", "auto_stop": stop}
                )
            self.assertIn("auto_stop", refused.exception.message)

    def test_the_recorded_report_must_carry_the_recorded_stop(self) -> None:
        stop = _stop()
        history._validate_operational_batch_fields(
            {"batch_id": "batch-1", "auto_stop": stop, "auto_report": _report(stop)}
        )
        history._validate_operational_batch_fields(
            {"batch_id": "batch-1", "auto_report": _report(None)}
        )
        for batch in (
            {"batch_id": "batch-1", "auto_stop": stop, "auto_report": _report(None)},
            {"batch_id": "batch-1", "auto_report": _report(stop)},
            {"batch_id": "batch-2", "auto_report": _report(None)},
            {
                "batch_id": "batch-1",
                "auto_report": {**_report(None), "outcome": "completed"},
            },
        ):
            with (
                self.subTest(batch=batch),
                self.assertRaises(CoordinatorError) as refused,
            ):
                history._validate_operational_batch_fields(batch)
            self.assertIn("auto_report", refused.exception.message)


class AutoDispatchApprovalTests(unittest.TestCase):
    """``_dispatch_approval_mode`` under ``auto``: every milestone is lifted while both the
    project config and the batch choose ``auto`` and no stop is recorded."""

    NO_APPROVAL = argparse.Namespace(approved_by=None, approved_at=None)

    def _batch(self, policy: str, route: str | None = None) -> JsonObject:
        entries: list[JsonObject] = []
        if route is not None:
            entries.append(
                {
                    "dispatch_id": "dispatch-1",
                    "role": "code-review",
                    "decision": {"decision": "retry", "routing": {"route": route}},
                }
            )
        return {
            "approval_policy": policy,
            "allowed_paths": ["**"],
            "dispatches": entries,
        }

    def _cases(
        self,
    ) -> list[tuple[str, JsonObject, str, str, JsonObject | None, str | None]]:
        """(milestone, batch fields, role, purpose, risk, rebase target) per milestone."""
        return [
            ("publish", {}, "developer", "publish", None, None),
            ("risk-trigger", {}, "qa", "work", {"matched_triggers": ["auth"]}, None),
            (
                "risk-reassessment-required",
                {"risk_reassessment_required": True},
                "code-review",
                "work",
                None,
                None,
            ),
            (
                "bypass-rerun",
                {"route": "bypass-rerun"},
                "code-review",
                "work",
                None,
                None,
            ),
            (
                "rebase-fix-forward",
                {"route": "rebase-fix-forward"},
                "developer",
                "work",
                None,
                None,
            ),
            ("rebase-target", {}, "developer", "work", None, "a" * 40),
        ]

    def _mode(
        self,
        config_policy: str | None,
        fields: JsonObject,
        role: str,
        purpose: str,
        risk: JsonObject | None,
        target: str | None,
        **batch_fields: object,
    ) -> str:
        batch = {
            **self._batch("auto", fields.get("route")),
            **{key: value for key, value in fields.items() if key != "route"},
            **batch_fields,
        }
        config_ = {"approval_policy": config_policy} if config_policy else {}
        return dispatch._dispatch_approval_mode(
            self.NO_APPROVAL, batch, config_, role, purpose, risk, target
        )

    def test_milestones_are_listed_in_a_fixed_order(self) -> None:
        for name, fields, role, purpose, risk, target in self._cases():
            with self.subTest(milestone=name):
                batch = {
                    **self._batch("manual_all", fields.get("route")),
                    **{key: value for key, value in fields.items() if key != "route"},
                }
                self.assertEqual(
                    dispatch._milestones(batch, "auto", role, purpose, risk, target),
                    [name],
                )
        self.assertEqual(
            dispatch._milestones(
                self._batch("milestone"), "milestone", "qa", "work", None, None
            ),
            ["qa"],
        )

    def test_auto_lifts_every_milestone(self) -> None:
        for name, fields, role, purpose, risk, target in self._cases():
            with self.subTest(milestone=name):
                self.assertEqual(
                    self._mode("auto", fields, role, purpose, risk, target),
                    approval.AUTO_APPROVER,
                )

    def test_a_batch_auto_policy_alone_keeps_the_milestones(self) -> None:
        for config_policy in (None, "milestone"):
            for name, fields, role, purpose, risk, target in self._cases():
                with (
                    self.subTest(config=config_policy, milestone=name),
                    self.assertRaises(CoordinatorError) as refused,
                ):
                    self._mode(config_policy, fields, role, purpose, risk, target)
                self.assertIn("--approved-by", refused.exception.remedy)

    def test_a_recorded_stop_ends_every_policy_approval(self) -> None:
        with self.assertRaises(CoordinatorError) as refused:
            self._mode(
                "auto",
                {},
                "architect",
                "work",
                None,
                None,
                auto_stop={"category": "budget-exhausted", "reason": "stale"},
            )
        self.assertIn("stopped", refused.exception.message)
        self.assertIn("batch auto-report", refused.exception.remedy)

    def test_an_explicit_approval_stays_explicit(self) -> None:
        self.assertEqual(
            dispatch._dispatch_approval_mode(
                argparse.Namespace(approved_by="Malove", approved_at=MOMENT),
                self._batch("auto"),
                {"approval_policy": "auto"},
                "architect",
                "work",
                None,
            ),
            "explicit",
        )


if __name__ == "__main__":
    unittest.main()
