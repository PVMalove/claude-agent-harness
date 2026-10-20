"""The risk-reassessment policy: its transition table and guard, with no ledger or Git (issue #781).

The expectations below are written out by hand, not derived from the policy under test."""

from __future__ import annotations

import unittest

from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.workflow import risk
from harness.orchestration.workflow.risk import BOUND, IDLE, PENDING, Reassessment

C1 = "1" * 40
C2 = "2" * 40
V = "f" * 40
IDLE_STATE = Reassessment(IDLE)
PENDING_STATE = Reassessment(PENDING)


def bound(candidate: str, *triggers: str) -> Reassessment:
    return Reassessment(BOUND, candidate, triggers)


def fields_of(state: Reassessment) -> JsonObject:
    batch: JsonObject = {}
    risk.apply_reassessment(batch, state)
    return batch


class RewindAndWriterReportTableTests(unittest.TestCase):
    def test_rewind_table(self) -> None:
        rows = [
            # (state, event, superseded, expected)
            (IDLE_STATE, risk.REWIND_CLEAR, False, IDLE_STATE),
            (IDLE_STATE, risk.REWIND_CLEAR, True, IDLE_STATE),
            (bound(C1, "t"), risk.REWIND_CLEAR, True, IDLE_STATE),
            (PENDING_STATE, risk.REWIND_CLEAR, False, IDLE_STATE),
            (IDLE_STATE, risk.REWIND_VERIFICATION, False, IDLE_STATE),
            (IDLE_STATE, risk.REWIND_VERIFICATION, True, bound(V)),
            (bound(C1, "t"), risk.REWIND_VERIFICATION, False, bound(V)),
            (bound(C1, "t"), risk.REWIND_VERIFICATION, True, bound(V)),
            (PENDING_STATE, risk.REWIND_VERIFICATION, False, bound(V)),
            (IDLE_STATE, risk.REWIND_WRITER, False, IDLE_STATE),
            (IDLE_STATE, risk.REWIND_WRITER, True, PENDING_STATE),
            (bound(C1), risk.REWIND_WRITER, False, PENDING_STATE),
            (bound(C1, "t"), risk.REWIND_WRITER, True, PENDING_STATE),
            # A second rewind never turns an owed reassessment into idle.
            (PENDING_STATE, risk.REWIND_WRITER, False, PENDING_STATE),
            (PENDING_STATE, risk.REWIND_WRITER, True, PENDING_STATE),
        ]
        for state, event, superseded, expected in rows:
            with self.subTest(state=state, event=event, superseded=superseded):
                self.assertEqual(
                    risk.next_after_rewind(
                        state,
                        event=event,
                        superseded=superseded,
                        verification_candidate=V,
                    ),
                    expected,
                )

    def test_rewind_to_verification_needs_the_registered_candidate(self) -> None:
        with self.assertRaises(CoordinatorError):
            risk.next_after_rewind(
                PENDING_STATE, event=risk.REWIND_VERIFICATION, superseded=True
            )

    def test_writer_report_table(self) -> None:
        rows = [
            # (state, candidate, triggers, expected)
            (IDLE_STATE, C1, (), IDLE_STATE),
            (IDLE_STATE, C1, ("t",), bound(C1, "t")),
            (PENDING_STATE, C1, (), bound(C1)),
            (PENDING_STATE, C1, ("t",), bound(C1, "t")),
            (bound(C1, "t"), C1, (), bound(C1, "t")),
            (bound(C1, "t"), C2, (), bound(C2, "t")),
            (bound(C1), C2, ("u",), bound(C2, "u")),
        ]
        for state, candidate, triggers, expected in rows:
            with self.subTest(state=state, candidate=candidate, triggers=triggers):
                self.assertEqual(
                    risk.next_after_writer_report(state, candidate, triggers), expected
                )

    def test_a_state_round_trips_through_the_stored_fields(self) -> None:
        for state in (IDLE_STATE, PENDING_STATE, bound(C1), bound(C1, "t", "u")):
            with self.subTest(state=state):
                self.assertEqual(risk.reassessment_state(fields_of(state)), state)

    def test_a_legacy_record_without_candidate_or_marker_reads_as_pending(self) -> None:
        legacy: JsonObject = {"risk_reassessment_required": True}
        self.assertEqual(risk.reassessment_state(legacy), PENDING_STATE)


class GuardTests(unittest.TestCase):
    def test_a_legal_transition_passes(self) -> None:
        before = fields_of(PENDING_STATE)
        after = fields_of(bound(C1))
        risk.require_legal_reassessment(before, after, risk.WRITER_REPORT)

    def test_the_requirement_without_candidate_or_marker_is_refused(self) -> None:
        before = fields_of(IDLE_STATE)
        for after in (
            {"risk_reassessment_required": True},
            {"risk_reassessment_required": True, "risk_reassessment_triggers": []},
            {
                "risk_reassessment_required": True,
                "risk_reassessment_candidate": C1,
                "risk_reassessment_triggers": [],
                "risk_reassessment_pending_next": True,
            },
            {"risk_reassessment_required": False, "risk_reassessment_candidate": C1},
        ):
            with self.subTest(after=after), self.assertRaises(CoordinatorError):
                risk.require_legal_reassessment(before, after, risk.WRITER_REPORT)

    def test_a_writer_report_cannot_discharge_the_requirement(self) -> None:
        for before in (PENDING_STATE, bound(C1, "t")):
            with self.subTest(before=before), self.assertRaises(CoordinatorError):
                risk.require_legal_reassessment(
                    fields_of(before), fields_of(IDLE_STATE), risk.WRITER_REPORT
                )

    def test_a_rewind_to_the_writer_cannot_discharge_the_requirement(self) -> None:
        with self.assertRaises(CoordinatorError):
            risk.require_legal_reassessment(
                fields_of(PENDING_STATE), fields_of(IDLE_STATE), risk.REWIND_WRITER
            )

    def test_fields_carried_over_unchanged_pass_even_for_a_legacy_record(self) -> None:
        legacy: JsonObject = {"risk_reassessment_required": True}
        risk.require_legal_reassessment(legacy, dict(legacy), risk.ASSESSMENT)
