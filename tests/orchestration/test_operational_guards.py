"""Pure operational guards of the orchestration coordinator (issue #250): the transition digest, the
read-only retry idempotency key, context-pressure levels, attention findings and the pluggable
extension registry. None of these touch git or the ledger."""

from __future__ import annotations

import unittest

import pytest

from harness.errors import HarnessError
from harness.orchestration import extensions
from harness.orchestration import operational_guards as guards


def _transition(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "batch_id": "batch-1",
        "previous_dispatch_id": "dispatch-0",
        "previous_role": "code-review",
        "reason_category": "transport",
        "next_role": "code-review",
        "next_action": "code-review",
        "purpose": "work",
        "candidate_sha": "a" * 40,
        "base_sha": "b" * 40,
        "review_scope": ["services/x.py"],
        "verification_commands": ["true"],
        "context_package_id": "context-package-1",
        "required_gates": ["review"],
    }
    fields.update(overrides)
    return fields


def _built(**extra: str) -> dict[str, object]:
    fields = _transition()
    return guards.build_transition(**fields, **extra)  # type: ignore[arg-type]


def _key(**overrides: object) -> str:
    fields: dict[str, object] = {
        "role": "code-review",
        "candidate_sha": "a" * 40,
        "base_sha": "b" * 40,
        "review_scope": ["services/x.py"],
        "reason_category": "transport",
        "verification_commands": ["true"],
    }
    fields.update(overrides)
    return guards.retry_idempotency_key(**fields)  # type: ignore[arg-type]


class TransitionDigestTests(unittest.TestCase):
    def test_digest_is_stable_and_independent_of_key_order(self) -> None:
        forward = _transition()
        backward = dict(reversed(list(forward.items())))

        self.assertEqual(
            guards.transition_digest(forward), guards.transition_digest(backward)
        )
        self.assertRegex(guards.transition_digest(forward), r"^[0-9a-f]{64}$")

    def test_every_bound_field_changes_the_digest(self) -> None:
        baseline = guards.transition_digest(_transition())
        changes = {
            "batch_id": "batch-2",
            "previous_dispatch_id": "dispatch-9",
            "previous_role": "qa",
            "reason_category": "verification-infrastructure",
            "next_role": "qa",
            "next_action": "qa",
            "purpose": "publish",
            "candidate_sha": "c" * 40,
            "base_sha": "d" * 40,
            "review_scope": ["services/y.py"],
            "verification_commands": ["make test"],
            "context_package_id": "context-package-2",
            "required_gates": ["review", "qa"],
        }
        self.assertEqual(set(changes), set(guards.TRANSITION_FIELDS))
        for field, value in changes.items():
            with self.subTest(field=field):
                self.assertNotEqual(
                    guards.transition_digest(_transition(**{field: value})), baseline
                )

    def test_an_incomplete_or_extended_transition_is_rejected(self) -> None:
        missing = _transition()
        del missing["candidate_sha"]
        with self.assertRaises(HarnessError):
            guards.transition_digest(missing)
        with self.assertRaises(HarnessError):
            guards.transition_digest(_transition(extra="x"))

    def test_a_carried_items_digest_is_bound_only_when_the_brief_carries_items(
        self,
    ) -> None:
        section = {"coordinator-finding": [{"item_id": "coordinator-finding-1"}]}
        digest = guards.carried_items_digest(section)
        bound = _transition(carried_items_sha256=digest)

        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertNotEqual(
            guards.transition_digest(bound), guards.transition_digest(_transition())
        )
        self.assertNotEqual(
            guards.transition_digest(bound),
            guards.transition_digest(_transition(carried_items_sha256="0" * 64)),
        )
        self.assertNotIn("carried_items_sha256", _built())
        self.assertEqual(
            _built(carried_items_sha256=digest)["carried_items_sha256"], digest
        )

    def test_a_rebase_target_is_bound_only_when_the_brief_carries_one(self) -> None:
        target = "e" * 40
        bound = _transition(rebase_target_sha=target)

        self.assertEqual(
            guards.OPTIONAL_TRANSITION_FIELDS,
            (
                "recovery_event_sha256",
                "carried_items_sha256",
                "rebase_target_sha",
                "delta_review_sha256",
                "infrastructure_retry_sha256",
                "infrastructure_attempt_sha256",
            ),
        )
        self.assertNotEqual(
            guards.transition_digest(bound), guards.transition_digest(_transition())
        )
        self.assertNotEqual(
            guards.transition_digest(bound),
            guards.transition_digest(_transition(rebase_target_sha="f" * 40)),
        )
        self.assertNotIn("rebase_target_sha", _built())
        self.assertEqual(_built(rebase_target_sha=target)["rebase_target_sha"], target)
        both = _built(carried_items_sha256="0" * 64, rebase_target_sha=target)
        self.assertRegex(guards.transition_digest(both), r"^[0-9a-f]{64}$")

    def test_a_delta_review_scope_is_bound_only_when_the_brief_carries_one(
        self,
    ) -> None:
        """Issue #625: a code-review brief after a fix-forward binds its delta_review_scope."""
        digest = guards.delta_review_digest({"mode": "delta", "escalations": []})
        bound = _transition(delta_review_sha256=digest)

        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertNotEqual(
            digest, guards.delta_review_digest({"mode": "full", "escalations": []})
        )
        self.assertNotEqual(
            guards.transition_digest(bound), guards.transition_digest(_transition())
        )
        self.assertNotIn("delta_review_sha256", _built())
        self.assertEqual(
            _built(delta_review_sha256=digest)["delta_review_sha256"], digest
        )
        with self.assertRaises(HarnessError) as unknown:
            guards.transition_digest(_transition(delta_review_scope=digest))
        self.assertIn("delta_review_sha256 only when", unknown.exception.remedy)


class RetryIdempotencyKeyTests(unittest.TestCase):
    def test_key_is_deterministic(self) -> None:
        self.assertEqual(_key(), _key())

    def test_every_component_changes_the_key(self) -> None:
        baseline = _key()
        changes = {
            "role": "qa",
            "candidate_sha": "c" * 40,
            "base_sha": "d" * 40,
            "review_scope": ["other.py"],
            "reason_category": "verification-infrastructure",
            "verification_commands": ["make test"],
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
        cases = {
            0: "ok",
            119_999: "ok",
            120_000: "warning",
            149_999: "warning",
            150_000: "critical",
            400_000: "critical",
        }
        for observed, level in cases.items():
            with self.subTest(observed=observed):
                self.assertEqual(
                    guards.pressure_level(observed, 150_000, 0.8), (120_000, level)
                )


class AttentionFindingTests(unittest.TestCase):
    def test_finding_key_names_reason_and_subject_and_the_top_finding_follows_priority(
        self,
    ) -> None:
        stale = guards.attention_finding(
            "stale-dispatch",
            "dispatch-1",
            last_safe_action="a",
            recommended_human_action="b",
        )
        unknown = guards.attention_finding(
            "unknown-reason",
            "dispatch-2",
            last_safe_action="c",
            recommended_human_action="d",
        )

        self.assertEqual(stale["key"], "stale-dispatch:dispatch-1")
        self.assertIs(guards.top_finding([stale, unknown]), unknown)

    def test_a_tooling_retry_streak_counts_consecutive_tooling_retries_of_one_candidate(
        self,
    ) -> None:
        def tooling(candidate: str | None) -> dict[str, object]:
            return {
                "decision": "retry",
                "routing": {"route": "tooling-retry", "candidate_commit": candidate},
            }

        accept: dict[str, object] = {"decision": "accept"}
        rerun: dict[str, object] = {
            "decision": "retry",
            "routing": {"route": "same-candidate-rerun", "candidate_commit": "a"},
        }
        cases: list[tuple[list[dict[str, object]], int]] = [
            ([], 0),
            ([accept], 0),
            ([tooling("a")], 1),
            ([tooling("a"), tooling("a"), tooling("a")], 3),
            ([tooling("a"), accept, tooling("a")], 1),
            ([tooling("a"), rerun, tooling("a"), tooling("a")], 2),
            ([tooling("b"), tooling("a"), tooling("a")], 2),
            ([tooling(None), tooling(None), tooling(None)], 3),
            ([tooling("a"), tooling("a"), accept], 0),
        ]
        for decisions, streak in cases:
            with self.subTest(decisions=decisions):
                self.assertEqual(guards.tooling_retry_streak(decisions), streak)

    def test_a_repeated_tooling_retry_follows_a_repeated_infrastructure_retry(
        self,
    ) -> None:
        findings = [
            guards.attention_finding(
                reason, "dispatch-1", last_safe_action="a", recommended_human_action="b"
            )
            for reason in (
                "retry-queued-too-long",
                "tooling-retry-repeated",
                "infrastructure-retry-repeated",
            )
        ]

        self.assertEqual(
            guards.top_finding(findings)["reason"], "infrastructure-retry-repeated"
        )
        self.assertEqual(
            guards.top_finding(findings[:2])["reason"], "tooling-retry-repeated"
        )
        self.assertEqual(guards.MAX_CONSECUTIVE_TOOLING_RETRIES, 2)

    def test_an_unknown_attention_reason_is_rejected(self) -> None:
        with self.assertRaises(HarnessError):
            guards.attention_finding(
                "bored", "x", last_safe_action="a", recommended_human_action="b"
            )


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
        self.assertIsNone(
            extensions.verification_environment_health("none").probe("dispatch-1")
        )
        self.assertIsNone(
            extensions.context_telemetry_provider("none").observe("dispatch-1")
        )
        self.assertIsNone(
            extensions.retry_reason_classifier("none").classify(
                extensions.ClassificationFacts(
                    stage="qa", outcome="blocked", transport=None, verification=None
                ),
            )
        )
        extensions.human_notifier("none").notify(
            extensions.AttentionEvent("batch-1", "stale-dispatch", "now", "a", "b"),
        )

    def test_a_registered_implementation_is_selected_by_name(self) -> None:
        recorder = _Recorder()
        extensions.register("human_notifier", "recorder", recorder)
        self.assertEqual(
            extensions.selected({"extensions": {"human_notifier": "recorder"}})[
                "human_notifier"
            ],
            "recorder",
        )

        extensions.human_notifier("recorder").notify(
            extensions.AttentionEvent("batch-1", "r", "t", "a", "b")
        )

        self.assertEqual(recorder.events[0].batch_id, "batch-1")

    def test_an_unknown_kind_or_name_fails_closed(self) -> None:
        with self.assertRaises(HarnessError):
            extensions.register("prompt_rewriter", "x", object())
        with self.assertRaises(HarnessError):
            extensions.human_notifier("not-registered")
        with self.assertRaises(HarnessError):
            extensions.selected({"extensions": {"prompt_rewriter": "x"}})


def test_the_transition_field_remedy_names_every_optional_field() -> None:
    with pytest.raises(guards.GuardError) as caught:
        guards.transition_digest({})

    for field in (*guards.OPTIONAL_TRANSITION_FIELDS, guards.ACCESS_TRANSITION_FIELD):
        assert field in caught.value.remedy


if __name__ == "__main__":
    unittest.main()
