"""Pure operational guards of the orchestration coordinator (issue #250): the transition digest, the
read-only retry idempotency key, context-pressure levels, attention findings and the pluggable
extension registry. None of these touch git or the ledger."""

from __future__ import annotations

import unittest

from harness.errors import HarnessError
from harness.orchestration import extensions, operational_guards as guards


def _transition(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "batch_id": "batch-1", "previous_dispatch_id": "dispatch-0", "previous_role": "code-review",
        "reason_category": "transport", "next_role": "code-review", "next_action": "code-review",
        "purpose": "work", "candidate_sha": "a" * 40, "base_sha": "b" * 40,
        "review_scope": ["services/x.py"], "verification_commands": ["true"],
        "context_package_id": "context-package-1", "required_gates": ["review"],
    }
    fields.update(overrides)
    return fields


def _key(**overrides: object) -> str:
    fields: dict[str, object] = {
        "role": "code-review", "candidate_sha": "a" * 40, "base_sha": "b" * 40,
        "review_scope": ["services/x.py"], "reason_category": "transport", "verification_commands": ["true"],
    }
    fields.update(overrides)
    return guards.retry_idempotency_key(**fields)  # type: ignore[arg-type]


class TransitionDigestTests(unittest.TestCase):
    def test_digest_is_stable_and_independent_of_key_order(self) -> None:
        forward = _transition()
        backward = dict(reversed(list(forward.items())))

        self.assertEqual(guards.transition_digest(forward), guards.transition_digest(backward))
        self.assertRegex(guards.transition_digest(forward), r"^[0-9a-f]{64}$")

    def test_every_bound_field_changes_the_digest(self) -> None:
        baseline = guards.transition_digest(_transition())
        changes = {
            "batch_id": "batch-2", "previous_dispatch_id": "dispatch-9", "previous_role": "qa",
            "reason_category": "verification-infrastructure", "next_role": "qa", "next_action": "qa",
            "purpose": "publish", "candidate_sha": "c" * 40, "base_sha": "d" * 40,
            "review_scope": ["services/y.py"], "verification_commands": ["make test"],
            "context_package_id": "context-package-2", "required_gates": ["review", "qa"],
        }
        self.assertEqual(set(changes), set(guards.TRANSITION_FIELDS))
        for field, value in changes.items():
            with self.subTest(field=field):
                self.assertNotEqual(guards.transition_digest(_transition(**{field: value})), baseline)

    def test_an_incomplete_or_extended_transition_is_rejected(self) -> None:
        missing = _transition()
        del missing["candidate_sha"]
        with self.assertRaises(HarnessError):
            guards.transition_digest(missing)
        with self.assertRaises(HarnessError):
            guards.transition_digest(_transition(extra="x"))


class RetryIdempotencyKeyTests(unittest.TestCase):
    def test_key_is_deterministic(self) -> None:
        self.assertEqual(_key(), _key())

    def test_every_component_changes_the_key(self) -> None:
        baseline = _key()
        changes = {
            "role": "qa", "candidate_sha": "c" * 40, "base_sha": "d" * 40, "review_scope": ["other.py"],
            "reason_category": "verification-infrastructure", "verification_commands": ["make test"],
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                self.assertNotEqual(_key(**{field: value}), baseline)

    def test_only_read_only_roles_and_publish_carry_a_key(self) -> None:
        self.assertEqual(guards.keyed_role("code-review", "work"), "code-review")
        self.assertEqual(guards.keyed_role("qa", "work"), "qa")
        self.assertEqual(guards.keyed_role("architect", "work"), "architect")
        self.assertEqual(guards.keyed_role("developer", "publish"), "publish")
        self.assertIsNone(guards.keyed_role("developer", "work"))


class ContextPressureLevelTests(unittest.TestCase):
    def test_levels_follow_the_warning_threshold_and_the_limit(self) -> None:
        cases = {0: "ok", 119_999: "ok", 120_000: "warning", 149_999: "warning", 150_000: "critical", 400_000: "critical"}
        for observed, level in cases.items():
            with self.subTest(observed=observed):
                self.assertEqual(guards.pressure_level(observed, 150_000, 0.8), (120_000, level))


class AttentionFindingTests(unittest.TestCase):
    def test_finding_key_names_reason_and_subject_and_the_top_finding_follows_priority(self) -> None:
        stale = guards.attention_finding("stale-dispatch", "dispatch-1", last_safe_action="a", recommended_human_action="b")
        unknown = guards.attention_finding("unknown-reason", "dispatch-2", last_safe_action="c", recommended_human_action="d")

        self.assertEqual(stale["key"], "stale-dispatch:dispatch-1")
        self.assertIs(guards.top_finding([stale, unknown]), unknown)

    def test_an_unknown_attention_reason_is_rejected(self) -> None:
        with self.assertRaises(HarnessError):
            guards.attention_finding("bored", "x", last_safe_action="a", recommended_human_action="b")


class _Recorder:
    def __init__(self) -> None:
        self.events: list[extensions.AttentionEvent] = []

    def notify(self, event: extensions.AttentionEvent) -> None:
        self.events.append(event)


class ExtensionRegistryTests(unittest.TestCase):
    def tearDown(self) -> None:
        extensions.unregister("human_notifier", "recorder")

    def test_defaults_are_inert_built_ins(self) -> None:
        self.assertEqual(
            extensions.selected({}),
            {kind: "none" for kind in extensions.EXTENSION_KINDS},
        )
        self.assertIsNone(extensions.transport_health("none").probe("dispatch-1"))
        self.assertIsNone(extensions.verification_environment_health("none").probe("dispatch-1"))
        self.assertIsNone(extensions.context_telemetry_provider("none").observe("dispatch-1"))
        self.assertIsNone(extensions.retry_reason_classifier("none").classify(
            extensions.ClassificationFacts(stage="qa", outcome="blocked", transport=None, verification=None),
        ))
        extensions.human_notifier("none").notify(
            extensions.AttentionEvent("batch-1", "stale-dispatch", "now", "a", "b"),
        )

    def test_a_registered_implementation_is_selected_by_name(self) -> None:
        recorder = _Recorder()
        extensions.register("human_notifier", "recorder", recorder)
        self.assertEqual(
            extensions.selected({"extensions": {"human_notifier": "recorder"}})["human_notifier"], "recorder",
        )

        extensions.human_notifier("recorder").notify(extensions.AttentionEvent("batch-1", "r", "t", "a", "b"))

        self.assertEqual(recorder.events[0].batch_id, "batch-1")

    def test_an_unknown_kind_or_name_fails_closed(self) -> None:
        with self.assertRaises(HarnessError):
            extensions.register("prompt_rewriter", "x", object())
        with self.assertRaises(HarnessError):
            extensions.human_notifier("not-registered")
        with self.assertRaises(HarnessError):
            extensions.selected({"extensions": {"prompt_rewriter": "x"}})


if __name__ == "__main__":
    unittest.main()
