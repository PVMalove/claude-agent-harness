"""The risk-reassessment policy: its transition table and guard, with no ledger or Git (issue #781).

The expectations below are written out by hand, not derived from the policy under test."""

from __future__ import annotations

import itertools
import random
import unittest
from collections.abc import Callable

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


class AssessmentTableTests(unittest.TestCase):
    def test_assessment_table(self) -> None:
        rows = [
            # (state, assessed candidate, matched triggers, expected)
            (IDLE_STATE, C1, ["t"], IDLE_STATE),
            (bound(C1), C1, [], IDLE_STATE),
            (bound(C1, "t"), C1, ["t", "u"], IDLE_STATE),
            # Only the bound candidate discharges the requirement.
            (bound(C1), C2, ["t"], bound(C1)),
            # Every owed trigger must be matched.
            (bound(C1, "t", "u"), C1, ["t"], bound(C1, "t", "u")),
            # Nothing is bound yet: an assessment cannot discharge a pending requirement.
            (PENDING_STATE, C1, ["t"], PENDING_STATE),
            (PENDING_STATE, C2, [], PENDING_STATE),
        ]
        for state, candidate, matched, expected in rows:
            with self.subTest(state=state, candidate=candidate, matched=matched):
                self.assertEqual(
                    risk.next_after_assessment(state, candidate, matched), expected
                )

    def test_repeating_an_assessment_changes_nothing(self) -> None:
        for state in (IDLE_STATE, PENDING_STATE, bound(C1), bound(C1, "t")):
            for candidate in (C1, C2):
                with self.subTest(state=state, candidate=candidate):
                    once = risk.next_after_assessment(state, candidate, ["t"])
                    self.assertEqual(
                        risk.next_after_assessment(once, candidate, ["t"]), once
                    )


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


Step = Callable[[Reassessment], Reassessment]
Event = tuple[str, str, Step]


def _rewind(kind: str, superseded: bool) -> Step:
    def step(state: Reassessment) -> Reassessment:
        return risk.next_after_rewind(
            state, event=kind, superseded=superseded, verification_candidate=V
        )

    return step


def _writer(candidate: str, triggers: tuple[str, ...]) -> Step:
    def step(state: Reassessment) -> Reassessment:
        return risk.next_after_writer_report(state, candidate, triggers)

    return step


def _assess(candidate: str, matched: list[str]) -> Step:
    def step(state: Reassessment) -> Reassessment:
        return risk.next_after_assessment(state, candidate, matched)

    return step


def all_events() -> list[Event]:
    """Every event the policy knows, over two candidates and a small trigger vocabulary."""
    events: list[Event] = []
    for kind, superseded in itertools.product(
        (risk.REWIND_CLEAR, risk.REWIND_VERIFICATION, risk.REWIND_WRITER),
        (False, True),
    ):
        events.append(
            (f"{kind}/superseded={superseded}", kind, _rewind(kind, superseded))
        )
    for candidate, triggers in itertools.product((C1, C2), ((), ("t",))):
        events.append(
            (
                f"writer/{candidate[0]}/{triggers}",
                risk.WRITER_REPORT,
                _writer(candidate, triggers),
            )
        )
    for candidate, matched in itertools.product((C1, C2), ([], ["t"], ["t", "u"])):
        events.append(
            (
                f"assess/{candidate[0]}/{matched}",
                risk.ASSESSMENT,
                _assess(candidate, matched),
            )
        )
    return events


ALL_STATES = (
    IDLE_STATE,
    bound(C1),
    bound(C1, "t"),
    bound(C2),
    PENDING_STATE,
)
LEGACY_FIELDS: JsonObject = {"risk_reassessment_required": True}


class TransitionEnumerationTests(unittest.TestCase):
    """Property-style checks by exhaustive and seeded enumeration, using only the standard library."""

    def test_every_state_and_event_gives_a_legal_state_and_transition(self) -> None:
        for state, (name, kind, step) in itertools.product(ALL_STATES, all_events()):
            with self.subTest(state=state, event=name):
                after = step(state)
                before_fields = fields_of(state)
                batch = fields_of(after)
                risk.require_legal_reassessment(before_fields, batch, kind)
                self.assertEqual(risk.reassessment_state(batch), after)

    def test_a_legacy_record_moves_only_to_a_legal_state(self) -> None:
        legacy = risk.reassessment_state(LEGACY_FIELDS)
        self.assertEqual(legacy, PENDING_STATE)
        for name, kind, step in all_events():
            with self.subTest(event=name):
                after = step(legacy)
                risk.require_legal_reassessment(LEGACY_FIELDS, fields_of(after), kind)

    def test_the_requirement_is_discharged_only_by_the_bound_candidate_or_a_clear_rewind(
        self,
    ) -> None:
        for state, (name, kind, step) in itertools.product(ALL_STATES, all_events()):
            after = step(state)
            if state.kind == IDLE or after.kind != IDLE:
                continue
            with self.subTest(state=state, event=name):
                if kind == risk.REWIND_CLEAR:
                    continue
                self.assertEqual(kind, risk.ASSESSMENT)
                self.assertEqual(state.kind, BOUND)
                candidate = name.split("/")[1]
                self.assertEqual((state.candidate or "?")[0], candidate)

    def test_a_writer_report_or_a_writer_rewind_never_discharges(self) -> None:
        for state, (name, kind, step) in itertools.product(ALL_STATES, all_events()):
            if state.kind == IDLE or kind not in (
                risk.WRITER_REPORT,
                risk.REWIND_WRITER,
                risk.REWIND_VERIFICATION,
            ):
                continue
            with self.subTest(state=state, event=name):
                self.assertNotEqual(step(state).kind, IDLE)

    def test_assessment_and_writer_report_are_idempotent(self) -> None:
        for state, (name, kind, step) in itertools.product(ALL_STATES, all_events()):
            if kind not in (risk.ASSESSMENT, risk.WRITER_REPORT):
                continue
            with self.subTest(state=state, event=name):
                once = step(state)
                self.assertEqual(step(once), once)

    def test_seeded_event_sequences_keep_the_requirement_bound_to_an_actionable_state(
        self,
    ) -> None:
        events = all_events()
        for seed in (781, 1, 2, 3, 4, 5, 6, 7):
            rng = random.Random(seed)
            batch: JsonObject = {}
            for step_number in range(rng.randint(1, 12)):
                name, kind, step = rng.choice(events)
                before = risk.reassessment_fields(batch)
                state = risk.reassessment_state(batch)
                after = step(state)
                proposed = dict(batch)
                risk.apply_reassessment(proposed, after)
                risk.require_legal_reassessment(before, proposed, kind)
                batch = proposed
                with self.subTest(seed=seed, step=step_number, event=name):
                    if batch["risk_reassessment_required"]:
                        self.assertTrue(
                            batch.get("risk_reassessment_candidate")
                            or batch.get("risk_reassessment_pending_next")
                        )
                    else:
                        self.assertNotIn("risk_reassessment_candidate", batch)
                        self.assertNotIn("risk_reassessment_pending_next", batch)
