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
from collections.abc import Callable
from pathlib import Path

from harness.orchestration.core import config
from harness.orchestration.core.constants import AUTO_REPORT_FIELDS
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.workflow import (
    approval,
    auto_policy,
    auto_report,
    decisions,
    dispatch,
    history,
)

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
        cases: tuple[tuple[dict[str, object], str], ...] = (
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
        )
        for policy, problem in cases:
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
        cases: dict[str, Callable[[JsonObject], object]] = {
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

    def test_a_batch_auto_policy_alone_approves_no_transition(self) -> None:
        # A non-milestone transition: only the config decides whether the policy approves it.
        for config_policy in (None, "milestone", "low_risk", "manual_all"):
            with (
                self.subTest(config=config_policy),
                self.assertRaises(CoordinatorError) as refused,
            ):
                self._mode(config_policy, {}, "code-review", "work", None, None)
            self.assertIn("both choose it", refused.exception.message)
            self.assertIn("--approved-by", refused.exception.remedy)

    def test_a_recorded_stop_ends_policy_approval_whatever_the_config(self) -> None:
        for config_policy in (None, "auto", "milestone"):
            with (
                self.subTest(config=config_policy),
                self.assertRaises(CoordinatorError) as refused,
            ):
                self._mode(
                    config_policy,
                    {},
                    "code-review",
                    "work",
                    None,
                    None,
                    auto_stop={"category": "budget-exhausted", "reason": "stale"},
                )
            self.assertIn("stopped", refused.exception.message)

    def test_a_batch_without_a_policy_is_not_approved_by_a_config_auto(self) -> None:
        with self.assertRaises(CoordinatorError) as refused:
            dispatch._dispatch_approval_mode(
                self.NO_APPROVAL,
                {"allowed_paths": ["**"], "dispatches": []},
                {"approval_policy": "auto"},
                "code-review",
                "work",
                None,
            )
        self.assertIn("both choose it", refused.exception.message)

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


def _clean(role: str, **overrides: object) -> JsonObject:
    report: JsonObject = {
        "role": role,
        "outcome": "completed",
        "blockers": "none",
        "risks": "none",
        "checks_run": [{"command": "pytest", "result": "pass", "evidence": "ok"}],
    }
    report.update(overrides)
    return report


def _axes(severity: str, findings: list[JsonObject]) -> JsonObject:
    return {
        axis: {
            "severity": severity,
            "findings": findings,
            "risks": "none",
            "blockers": "none",
        }
        for axis in ("standards", "spec")
    }


class AutoDecisionTableTests(unittest.TestCase):
    """``category`` and ``finish`` on data: the auto decision table without I/O."""

    WORK: JsonObject = {"role": "developer", "purpose": "work"}

    def _category(
        self,
        stage: str,
        report: JsonObject,
        *,
        scope: list[str] | None = None,
        bypass: bool = False,
        pressure: bool = False,
    ) -> auto_policy.Choice:
        return auto_policy.category(
            stage,
            report,
            {"role": stage, "purpose": "work"},
            scope_warnings=scope or [],
            block_bypass=bypass,
            candidate_moved=False,
            pressure_recorded=pressure,
        )

    def test_a_clean_report_with_risks_is_accepted_and_its_risks_recorded(self) -> None:
        report = _clean("architect", risks="the ledger schema grows", checks_run=[])
        choice = self._category("architect", report)
        self.assertEqual((choice.decision, choice.carry_incomplete), ("accept", False))
        self.assertEqual(
            auto_policy.accepted_risks({}, {}, report),
            [{"source": "report", "risks": "the ledger schema grows"}],
        )

    def test_incomplete_items_of_later_roles_are_carried_with_the_accept(self) -> None:
        item = {"brief_item": "x", "reason": "y", "target_role": "developer"}
        choice = self._category(
            "architect", _clean("architect", incomplete_items=[item])
        )
        self.assertEqual((choice.decision, choice.carry_incomplete), ("accept", True))

    def test_incomplete_items_of_the_role_itself_narrow_the_retry(self) -> None:
        item = {"brief_item": "x", "reason": "y", "target_role": "architect"}
        choice = self._category(
            "architect", _clean("architect", incomplete_items=[item])
        )
        self.assertEqual((choice.decision, choice.narrowed), ("retry", True))

    def test_a_review_with_findings_retries_by_its_structured_evidence(self) -> None:
        finding = {"summary": "x", "file": "a.py", "line": 1}
        choice = self._category(
            "code-review", _clean("code-review", review=_axes("warning", [finding]))
        )
        self.assertEqual((choice.decision, choice.reason_category), ("retry", None))

    def test_a_block_bypass_reruns_a_read_only_stage_and_retries_a_writer(self) -> None:
        for stage, expected in (
            ("code-review", "block-bypass"),
            ("qa", "block-bypass"),
            ("developer", "code"),
            ("architect", "code"),
        ):
            with self.subTest(stage=stage):
                choice = self._category(stage, _clean(stage), bypass=True)
                self.assertEqual(
                    (choice.decision, choice.reason_category), ("retry", expected)
                )

    def test_unfinished_developer_requirements_retry_as_requirements(self) -> None:
        for name, report, scope in (
            (
                "not covered",
                _clean(
                    "developer",
                    dod_coverage=[{"dod_item": 1, "not_covered": "out of time"}],
                ),
                [],
            ),
            ("out of scope", _clean("developer"), ["services/b.py"]),
        ):
            with self.subTest(case=name):
                choice = self._category("developer", report, scope=scope)
                self.assertEqual(
                    (choice.decision, choice.reason_category), ("retry", "requirements")
                )

    def test_recorded_context_pressure_names_its_category(self) -> None:
        blocked = _clean("developer", outcome="blocked", blockers="context is full")
        self.assertEqual(
            self._category("developer", blocked, pressure=True).reason_category,
            "context-pressure",
        )
        self.assertIsNone(self._category("developer", blocked).reason_category)

    def test_finish_stops_on_a_dead_end_an_unknown_reason_or_an_exhausted_budget(
        self,
    ) -> None:
        retry = {"route": "developer-retry", "next_action": "developer-retry"}
        cases = (
            (None, CoordinatorError("refused", remedy="x"), False, "abandon-dead-end"),
            ({**retry, "reason_category": "unknown"}, None, False, "unknown-reason"),
            (
                {**retry, "reason_category": "code"},
                None,
                True,
                "retry_policy.max_developer_retries",
            ),
        )
        for routing, refusal, exhausted, reason in cases:
            with self.subTest(reason=reason):
                stop = auto_policy.finish(
                    routing, refusal, budget_exhausted=exhausted, bug_ticket=None
                )
                assert stop is not None
                self.assertEqual(stop.reason, reason)

    def test_finish_retries_inside_the_budget(self) -> None:
        """A tooling-retry spends no developer retry, so an exhausted budget does not stop it."""
        for routing, ticket, exhausted in (
            (
                {
                    "route": "fix-forward",
                    "next_action": "developer-retry",
                    "reason_category": "code",
                },
                None,
                False,
            ),
            (
                {
                    "route": "tooling-retry",
                    "next_action": "developer-retry",
                    "reason_category": "tooling",
                },
                "#700",
                True,
            ),
        ):
            with self.subTest(route=routing["route"]):
                self.assertIsNone(
                    auto_policy.finish(
                        routing, None, budget_exhausted=exhausted, bug_ticket=ticket
                    )
                )

    def test_a_tooling_retry_without_a_bug_ticket_is_refused(self) -> None:
        with self.assertRaises(CoordinatorError) as refused:
            auto_policy.finish(
                {
                    "route": "tooling-retry",
                    "next_action": "code-review",
                    "reason_category": "tooling",
                },
                None,
                budget_exhausted=False,
                bug_ticket=None,
            )
        self.assertIn("--bug-ticket", refused.exception.remedy)

    def test_a_block_bypass_needs_a_note(self) -> None:
        with self.assertRaises(CoordinatorError):
            auto_policy.inputs_from(argparse.Namespace(block_bypass=True, note="none"))
        inputs = auto_policy.inputs_from(
            argparse.Namespace(block_bypass=True, note="ran pytest through a wrapper")
        )
        self.assertTrue(inputs.block_bypass)

    def test_a_stop_outside_the_closed_list_cannot_exist(self) -> None:
        with self.assertRaises(CoordinatorError):
            auto_policy.Stop("no-automatic-route", "operator-preference", {})


class AutoStopListTests(unittest.TestCase):
    """The closed stop list on ledger facts that need no I/O."""

    def _finding(self, reason: str) -> dict[str, str]:
        return {"key": f"{reason}:dispatch-1", "reason": reason}

    def test_each_attention_reason_maps_to_one_listed_stop(self) -> None:
        for reason, expected in (
            ("stale-dispatch", ("integrity-failure", "stale")),
            ("stale-evidence", ("integrity-failure", "stale")),
            ("retry-queued-too-long", ("integrity-failure", "stale")),
            (
                "infrastructure-retry-repeated",
                ("budget-exhausted", "attention_policy.max_infrastructure_retries"),
            ),
            (
                "tooling-retry-repeated",
                ("budget-exhausted", "tooling-retry-repeated"),
            ),
            ("unknown-reason", ("no-automatic-route", "unknown-reason")),
        ):
            with self.subTest(reason=reason):
                [stop] = auto_policy.attention_stops({}, [self._finding(reason)])
                self.assertEqual((stop.category, stop.reason), expected)

    def test_an_acknowledged_finding_is_no_stop_but_the_flag_is(self) -> None:
        finding = self._finding("stale-dispatch")
        self.assertEqual(
            auto_policy.attention_stops(
                {"attention_acknowledged": [finding["key"]]}, [finding]
            ),
            [],
        )
        [stop] = auto_policy.attention_stops(
            {
                "needs_attention": True,
                "attention_reason": "infrastructure-retry-repeated",
            },
            [],
        )
        self.assertEqual(stop.category, "budget-exhausted")

    def test_a_spent_continuation_budget_is_a_budget_stop(self) -> None:
        batch: JsonObject = {
            "dispatches": [
                {"dispatch_id": "dispatch-1", "state": "checkpointed"},
                {"dispatch_id": "dispatch-2", "state": "rate_limited"},
            ],
            "coordinator_decisions": [
                {"dispatch_id": "dispatch-1", "decision": "continue"},
                {"dispatch_id": "dispatch-1", "decision": "continue"},
                {"dispatch_id": "dispatch-2", "decision": "continue-automatic"},
            ],
        }
        stops = auto_policy._continuation_stops({}, batch)
        self.assertEqual(
            [(stop.reason, stop.evidence["dispatch_id"]) for stop in stops],
            [
                ("continuation_policy.max_continuations", "dispatch-1"),
                ("continuation_policy.max_rate_limit_resumes", "dispatch-2"),
            ],
        )

    def test_a_recorded_stop_ends_the_policy_auto_accept(self) -> None:
        config_ = {"approval_policy": "auto"}
        batch: JsonObject = {"approval_policy": "auto"}
        architect = {"role": "architect", "purpose": "work"}
        report = _clean("architect", checks_run=[])
        self.assertEqual(
            decisions._auto_accept_policy(config_, batch, architect, report), "auto"
        )
        stopped = {**batch, "auto_stop": _stop()}
        self.assertIsNone(
            decisions._auto_accept_policy(config_, stopped, architect, report)
        )
        self.assertFalse(approval.auto_active(config_, stopped))
        self.assertTrue(approval.auto_configured(config_, stopped))


class AutoReportBuildTests(unittest.TestCase):
    """``auto_report.build`` on data: the machine-readable final report."""

    def _rows(self) -> list[tuple[JsonObject, JsonObject, JsonObject | None]]:
        review_entry = {
            "dispatch_id": "dispatch-2",
            "role": "code-review",
            "decision": {"decision": "retry"},
        }
        review_brief = {
            "candidate_commit": "c" * 40,
            "carried_items": {
                "coordinator-finding": [
                    {"item_id": "coordinator-finding-1", "summary": "reset the counter"}
                ]
            },
        }
        review = _clean(
            "code-review",
            review=_axes("warning", [{"summary": "x"}]),
        )
        qa_entry = {
            "dispatch_id": "dispatch-3",
            "role": "qa",
            "decision": {"decision": "accept"},
        }
        qa = _clean("qa", qa_stages={"failed_stage": None})
        return [
            (review_entry, review_brief, review),
            (qa_entry, {"candidate_commit": "c" * 40}, qa),
        ]

    def _batch(self, **fields: object) -> JsonObject:
        batch = _approved_batch()
        batch.update(
            {
                "ticket": "#643",
                "branch": "feature/issue-643",
                "state": "completed",
                "definition_of_done": ["first", "second"],
                "allowed_paths": ["**"],
                "commit_plan": [
                    {
                        "id": "one",
                        "summary": "a",
                        "expected_paths": ["**"],
                        "covers": [1],
                    },
                    {
                        "id": "two",
                        "summary": "b",
                        "expected_paths": ["**"],
                        "covers": [1, 2],
                    },
                ],
                "coordinator_decisions": [
                    {
                        "dispatch_id": "dispatch-2",
                        "decision": "retry",
                        "approved_by": "policy:auto",
                        "next_role": "developer",
                        "routing": {
                            "route": "fix-forward",
                            "reason_category": "code",
                            "previous_role": "code-review",
                            "next_role": "developer",
                            "next_action": "developer-retry",
                        },
                    }
                ],
            }
        )
        batch["auto_decisions"][2]["evidence"]["accepted_risks"] = [
            {"source": "report", "risks": "the schema grows"}
        ]
        batch.update(fields)
        return batch

    def _build(self, batch: JsonObject, stop: JsonObject | None = None) -> JsonObject:
        return auto_report.build(
            batch,
            self._rows(),
            {},
            stop=stop,
            candidate="c" * 40,
            settled={"coordinator-finding-1"},
            coverage={1: ["a" * 40]},
            recorded_at=MOMENT,
        )

    def test_a_completed_batch_reports_every_decision_risk_retry_and_result(
        self,
    ) -> None:
        report = self._build(self._batch())

        self.assertEqual(set(report) | {"record_sha256"}, AUTO_REPORT_FIELDS)
        self.assertEqual(report["outcome"], "completed")
        self.assertEqual(len(report["decisions"]), 4)
        self.assertEqual(
            report["accepted_risks"],
            [
                {
                    "dispatch_id": "dispatch-1",
                    "source": "report",
                    "risks": "the schema grows",
                }
            ],
        )
        self.assertEqual(
            report["findings"],
            [
                {
                    "item_id": "coordinator-finding-1",
                    "source": "coordinator-finding",
                    "summary": "reset the counter",
                    "state": "settled",
                }
            ],
        )
        self.assertEqual(
            [(item["route"], item["reason_category"]) for item in report["retries"]],
            [("fix-forward", "code")],
        )
        self.assertEqual(report["budget"]["developer_retries"], {"spent": 1, "max": 1})
        self.assertEqual(report["commit_plan"]["entries"], ["one", "two"])
        self.assertEqual(
            [
                (item["dod_item"], item["plan_entries"], item["covered"])
                for item in report["dod_coverage"]
            ],
            [(1, ["one", "two"], True), (2, ["two"], False)],
        )
        self.assertEqual(
            report["review"],
            [
                {
                    "dispatch_id": "dispatch-2",
                    "candidate_commit": "c" * 40,
                    "decision": "retry",
                    "severity": {"standards": "warning", "spec": "warning"},
                    "findings": 2,
                }
            ],
        )
        self.assertEqual(
            report["qa"][0]["checks"], [{"command": "pytest", "result": "pass"}]
        )
        self.assertIn("explicit human confirmation", report["next_human_action"])
        self.assertIn("auto-merge is forbidden", report["next_human_action"])

    def test_a_stopped_batch_reports_its_stop_and_the_default_plan(self) -> None:
        stop = _stop()
        report = self._build(
            self._batch(state="awaiting-approval", commit_plan=None), stop
        )

        self.assertEqual((report["outcome"], report["stop"]), ("stopped", stop))
        self.assertEqual(report["commit_plan"]["source"], "default")
        self.assertIn("unknown-reason", report["next_human_action"])
        sealed = approval.sealed(report)
        history._validate_operational_batch_fields(
            {"batch_id": "batch-1", "auto_stop": stop, "auto_report": sealed}
        )


if __name__ == "__main__":
    unittest.main()
