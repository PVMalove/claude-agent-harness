#!/usr/bin/env python3
"""Unit tests for the pure commit-plan rules (issue #478)."""

from __future__ import annotations

import re
import unittest
from collections.abc import Callable
from pathlib import Path

from harness.orchestration.core.git_utils import _candidate_commit
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.workflow import commit_plan

DOD = ["pin the plan", "validate the report", "decide the report"]


def _entry(entry_id: str, covers: list[object], **overrides: object) -> JsonObject:
    entry: JsonObject = {
        "id": entry_id,
        "summary": f"implement {entry_id}",
        "expected_paths": ["harness/orchestration/**"],
        "covers": covers,
    }
    entry.update(overrides)
    return entry


class PinnedPlanTests(unittest.TestCase):
    def test_pinned_plan_accepts_entries_covering_every_item(self) -> None:
        document = {"commit_plan": [_entry("pin", [1]), _entry("rules", [3, 2])]}

        plan = commit_plan.pinned_plan(document, DOD)

        self.assertEqual([entry["id"] for entry in plan], ["pin", "rules"])
        self.assertEqual(plan[1]["covers"], [3, 2])
        self.assertEqual(plan[0]["expected_paths"], ["harness/orchestration/**"])

    def test_pinned_plan_rejects_an_uncovered_item_with_remedy(self) -> None:
        document = {"commit_plan": [_entry("pin", [1]), _entry("rules", [3])]}

        with self.assertRaisesRegex(
            CoordinatorError, r"no entry covers definition-of-done items \[2\]"
        ) as caught:
            commit_plan.pinned_plan(document, DOD)

        self.assertIn("every definition-of-done item 1..3", caught.exception.remedy)

    def test_pinned_plan_rejects_an_unknown_item(self) -> None:
        for unknown in (0, 4, True, "1"):
            with self.subTest(unknown=unknown):
                document = {"commit_plan": [_entry("all", [1, 2, 3, unknown])]}
                with self.assertRaisesRegex(
                    CoordinatorError, "covers unknown definition-of-done items"
                ) as caught:
                    commit_plan.pinned_plan(document, DOD)
                self.assertIn("1..3", caught.exception.remedy)

    def test_pinned_plan_rejects_malformed_entries(self) -> None:
        cases: dict[str, object] = {
            "not an object": ["plan"],
            "extra key": {"commit_plan": [_entry("a", [1, 2, 3])], "note": "x"},
            "empty plan": {"commit_plan": []},
            "entry not an object": {"commit_plan": ["a"]},
            "missing field": {
                "commit_plan": [
                    {"id": "a", "summary": "s", "expected_paths": ["x"]},
                ]
            },
            "bad id": {"commit_plan": [_entry("-a", [1, 2, 3])]},
            "duplicate id": {
                "commit_plan": [_entry("a", [1, 2]), _entry("a", [3])],
            },
            "empty summary": {"commit_plan": [_entry("a", [1, 2, 3], summary=" ")]},
            "empty paths": {"commit_plan": [_entry("a", [1, 2, 3], expected_paths=[])]},
            "absolute path": {
                "commit_plan": [_entry("a", [1, 2, 3], expected_paths=["/etc/x"])]
            },
            "parent path": {
                "commit_plan": [_entry("a", [1, 2, 3], expected_paths=["a/../b"])]
            },
            "empty covers": {"commit_plan": [_entry("a", [])]},
            "duplicate covers": {"commit_plan": [_entry("a", [1, 2, 3, 3])]},
        }
        for name, document in cases.items():
            with self.subTest(case=name):
                with self.assertRaisesRegex(
                    CoordinatorError, "commit plan file is invalid"
                ):
                    commit_plan.pinned_plan(document, DOD)

    def test_default_plan_keeps_one_entry_per_item(self) -> None:
        plan = commit_plan.default_plan(DOD, ["**"])

        self.assertEqual(
            plan,
            [
                {
                    "id": f"step-{index}",
                    "summary": item,
                    "expected_paths": ["**"],
                    "covers": [index],
                }
                for index, item in enumerate(DOD, start=1)
            ],
        )

    def test_accepted_plan_sha256_reads_the_architect_accept(self) -> None:
        plan = commit_plan.default_plan(DOD, ["**"])
        digest = commit_plan.plan_sha256(plan)
        batch: JsonObject = {
            "dispatches": [
                {"role": "architect", "decision": {"decision": "retry"}},
                {
                    "role": "architect",
                    "decision": {"decision": "accept", "commit_plan_sha256": digest},
                },
                {"role": "developer", "decision": {"decision": "accept"}},
            ]
        }

        self.assertEqual(commit_plan.accepted_plan_sha256(batch), digest)
        batch["dispatches"][1]["decision"].pop("commit_plan_sha256")
        self.assertIsNone(commit_plan.accepted_plan_sha256(batch))
        self.assertIsNone(commit_plan.accepted_plan_sha256({"dispatches": []}))


FIVE = ["one", "two", "three", "four", "five"]
C1, C2, C3 = "1" * 40, "2" * 40, "3" * 40


def _identity(sha: str) -> str:
    return sha


def _known_or_git(sha: str) -> str:
    """Known SHAs resolve to themselves; anything else goes to the coordinator's resolver."""
    return sha if sha in (C1, C2, C3) else _candidate_commit(Path("."), sha)


def _dispatch(
    plan: list[JsonObject] | None = None,
    *,
    retry: bool = False,
    role: str = "developer",
    items: list[str] = FIVE,
) -> JsonObject:
    return {
        "dispatch_id": "dispatch-1",
        "role": role,
        "definition_of_done": items,
        "commit_plan": commit_plan.default_plan(items, ["**"])
        if plan is None
        else plan,
        "transition": {"next_action": "developer-retry" if retry else "developer"},
    }


def _pairs(*pairs: tuple[str, str]) -> list[JsonObject]:
    return [{"commit_sha": sha, "plan_entry_id": entry} for sha, entry in pairs]


def _covered(*commits: list[str]) -> list[JsonObject]:
    return [
        {"dod_item": item, "commits": shas}
        for item, shas in enumerate(commits, start=1)
    ]


# The #443 shape: three commits for five definition-of-done items on the default plan.
MERGED = _pairs(
    (C1, "step-1"), (C1, "step-2"), (C2, "step-3"), (C3, "step-4"), (C3, "step-5")
)
MERGED_COVERAGE = _covered([C1], [C1], [C2], [C3], [C3])


class CheckReportTests(unittest.TestCase):
    def _check(
        self,
        report: JsonObject,
        dispatch: JsonObject | None = None,
        created: list[str] | None = None,
        resolve: Callable[[str], str] = _identity,
    ) -> None:
        commit_plan.check_fields_allowed(report, dispatch or _dispatch())
        commit_plan.check_report(
            report, dispatch or _dispatch(), created or [C1, C2, C3], resolve
        )

    def _refused(
        self,
        report: JsonObject,
        message: str,
        dispatch: JsonObject | None = None,
        created: list[str] | None = None,
        resolve: Callable[[str], str] = _identity,
    ) -> CoordinatorError:
        with self.assertRaisesRegex(CoordinatorError, message) as caught:
            self._check(report, dispatch, created, resolve)
        self.assertTrue(caught.exception.remedy)
        return caught.exception

    def test_one_to_one_report_needs_no_coverage_or_justification(self) -> None:
        plan = commit_plan.default_plan(FIVE[:3], ["**"])
        dispatch = _dispatch(plan, items=FIVE[:3])
        report = {"commit_map": _pairs((C1, "step-1"), (C2, "step-2"), (C3, "step-3"))}

        self._check(report, dispatch)
        self._check({**report, "dod_coverage": _covered([C1], [C2], [C3])}, dispatch)
        self._refused(
            {**report, "dod_coverage": _covered([C1], [C1], [C3])},
            r"item 2 claims commits .* covering item 2 \(step-2\)",
            dispatch,
        )
        self._refused(
            {**report, "divergence_justification": "nothing diverged"},
            "only for a commit_map that diverges",
            dispatch,
        )

    def test_merged_split_and_unclosed_entries_are_accepted_when_justified(
        self,
    ) -> None:
        justified = {"divergence_justification": "step-1 and step-2 share one seam"}
        cases = {
            "merged": {"commit_map": MERGED, "dod_coverage": MERGED_COVERAGE},
            "split": {
                "commit_map": _pairs(
                    (C1, "step-1"),
                    (C2, "step-1"),
                    (C3, "step-2"),
                    (C3, "step-3"),
                    (C3, "step-4"),
                    (C3, "step-5"),
                ),
                "dod_coverage": _covered([C1, C2], [C3], [C3], [C3], [C3]),
            },
            "unclosed": {
                "commit_map": _pairs(
                    (C1, "step-1"), (C2, "step-2"), (C3, "step-3"), (C3, "step-4")
                ),
                "dod_coverage": [
                    *_covered([C1], [C2], [C3], [C3]),
                    {"dod_item": 5, "not_covered": "moved to a follow-up issue"},
                ],
            },
        }
        for name, report in cases.items():
            with self.subTest(name):
                self._check({**report, **justified})

    def test_divergence_requires_coverage_and_a_justification(self) -> None:
        missing_coverage = self._refused(
            {"commit_map": MERGED}, "diverges from the commit plan .* no dod_coverage"
        )
        self.assertIn("not_covered", missing_coverage.remedy)
        missing_justification = self._refused(
            {"commit_map": MERGED, "dod_coverage": MERGED_COVERAGE},
            f"commit {C1} merges step-1, step-2.*without a divergence_justification",
        )
        self.assertIn("merged, split or added", missing_justification.remedy)
        self._refused(
            {
                "commit_map": MERGED,
                "dod_coverage": MERGED_COVERAGE,
                "divergence_justification": " ",
            },
            "without a divergence_justification",
        )

    def test_structural_mapping_errors_are_rejected_with_a_remedy(self) -> None:
        unmapped = self._refused(
            {"commit_map": _pairs((C1, "step-1"), (C2, "step-2"))},
            r"each created commit to at least one plan entry \(unmapped",
        )
        self.assertIn(C3, unmapped.message)
        foreign = self._refused(
            {"commit_map": [*MERGED, *_pairs(("4" * 40, "step-1"))]},
            "names commits this dispatch did not create",
        )
        self.assertIn("rebase target", foreign.remedy)
        unknown = self._refused(
            {"commit_map": [*MERGED, *_pairs((C1, "step-9"))]},
            r"unknown plan entries \['step-9'\]",
        )
        self.assertIn("step-1, step-2, step-3, step-4, step-5", unknown.remedy)
        self._refused(
            {"commit_map": [*MERGED, *_pairs((C1, "step-1"))]}, "repeats the pairs"
        )

    def test_coverage_records_are_checked_structurally(self) -> None:
        justified = {"commit_map": MERGED, "divergence_justification": "merged"}
        cases: dict[str, tuple[object, str]] = {
            "not a list": ({"1": [C1]}, "must be a list"),
            "bad record": ([{"dod_item": 1}], "must be {dod_item, commits}"),
            "both forms": (
                [{"dod_item": 1, "commits": [C1], "not_covered": "x"}],
                "must be {dod_item, commits}",
            ),
            "unknown item": (
                [*MERGED_COVERAGE, {"dod_item": 6, "commits": [C1]}],
                "unknown definition-of-done item 6",
            ),
            "boolean item": (
                [{"dod_item": True, "commits": [C1]}],
                "unknown definition-of-done item True",
            ),
            "repeated item": (
                [*MERGED_COVERAGE, {"dod_item": 1, "commits": [C2]}],
                "item 1 more than once",
            ),
            "missing item": (
                MERGED_COVERAGE[:4],
                r"no record for definition-of-done items \[5\]",
            ),
            "uncovered without a reason": (
                [*MERGED_COVERAGE[:4], {"dod_item": 5, "not_covered": " "}],
                "item 5 is not_covered without a reason",
            ),
            "empty commits": (
                [*MERGED_COVERAGE[:4], {"dod_item": 5, "commits": []}],
                "commits must be a non-empty list",
            ),
            "foreign commit": (
                [*MERGED_COVERAGE[:4], {"dod_item": 5, "commits": ["4" * 40]}],
                "item 5 names commits this dispatch did not create",
            ),
        }
        for name, (coverage, message) in cases.items():
            with self.subTest(name):
                self._refused({**justified, "dod_coverage": coverage}, message)

    def test_a_coverage_claim_must_follow_commit_map_and_covers(self) -> None:
        # The #478 code-review shape: item 3 claimed by C1 while step-3 stays unclosed.
        report = {
            "commit_map": _pairs(
                (C1, "step-1"), (C1, "step-2"), (C2, "step-4"), (C3, "step-5")
            ),
            "dod_coverage": _covered([C1], [C1], [C1], [C2], [C3]),
            "divergence_justification": "step-1 and step-2 share one seam",
        }
        refused = self._refused(
            report,
            re.escape(
                f"dod_coverage item 3 claims commits ['{C1}'], but commit_map maps none "
                "of them to a plan entry covering item 3 (step-3)"
            ),
        )
        self.assertIn("map one of the claimed commits to step-3", refused.remedy)
        self.assertIn("not_covered", refused.remedy)

        honest = [
            *report["dod_coverage"][:2],
            {"dod_item": 3, "not_covered": "step-3 deferred"},
        ]
        self._check({**report, "dod_coverage": [*honest, *report["dod_coverage"][3:]]})

    def test_a_coverage_claim_reads_pinned_covers_and_resolves_short_shas(self) -> None:
        plan = [_entry("pin", [1]), _entry("rules", [3, 2])]
        dispatch = _dispatch(plan, items=FIVE[:3])
        commit_map = _pairs((C1, "pin"), (C2, "pin"), (C3, "rules"))
        justified = {"commit_map": commit_map, "divergence_justification": "split"}
        short = {C3[:7]: C3}

        self._check(
            {**justified, "dod_coverage": _covered([C1, C2], [C2, C3[:7]], [C3])},
            dispatch,
            resolve=lambda sha: short.get(sha, sha),
        )
        refused = self._refused(
            {**justified, "dod_coverage": _covered([C1], [C1, C2], [C3])},
            r"item 2 claims commits .* covering item 2 \(rules\)",
            dispatch,
        )
        self.assertIn(C2, refused.message)

    def test_not_covered_stays_valid_for_an_item_the_mapping_covers(self) -> None:
        report = {
            "commit_map": MERGED,
            "dod_coverage": [
                *MERGED_COVERAGE[:2],
                {"dod_item": 3, "not_covered": "step-3 landed without its tests"},
                *MERGED_COVERAGE[3:],
            ],
            "divergence_justification": "step-1 and step-2 share one seam",
        }

        self._check(report)
        self.assertEqual(
            commit_plan.not_covered(report),
            [{"dod_item": 3, "reason": "step-3 landed without its tests"}],
        )

    def test_commit_map_shape_errors_keep_their_messages(self) -> None:
        missing = self._refused({"commit_map": []}, "requires commit_map")
        self.assertIn("one or more commit_plan entries", missing.remedy)
        self.assertIn("rebase target", missing.remedy)
        missing_retry = self._refused(
            {}, "requires commit_map", _dispatch(retry=True), [C1]
        )
        self.assertIn("one distinct commit_plan entry", missing_retry.remedy)
        self.assertNotIn("one or more", missing_retry.remedy)
        self._refused(
            {"commit_map": [{"commit_sha": C1}]},
            "must contain only commit_sha and plan_entry_id",
        )
        self._refused(
            {"commit_map": [{"commit_sha": 1, "plan_entry_id": "step-1"}]},
            "must use string SHA and plan entry id",
        )
        self._refused(
            {"commit_map": MERGED},
            "dispatch commit_plan is malformed",
            {**_dispatch(), "commit_plan": [{"summary": "no id"}]},
        )

    def test_developer_retry_keeps_one_distinct_entry_per_new_commit(self) -> None:
        retry = _dispatch(retry=True)
        self._check(
            {"commit_map": _pairs((C1, "step-1"), (C2, "step-3"))}, retry, [C1, C2]
        )
        for name, commit_map in {
            "merged": _pairs((C1, "step-1"), (C1, "step-2")),
            "shared entry": _pairs((C1, "step-1"), (C2, "step-1")),
            "unmapped": _pairs((C1, "step-1")),
            "unknown entry": _pairs((C1, "step-1"), (C2, "step-9")),
        }.items():
            with self.subTest(name):
                refused = self._refused(
                    {"commit_map": commit_map},
                    "one distinct immutable plan entry",
                    retry,
                    [C1, C2],
                )
                self.assertIn("need not close every plan entry", refused.remedy)
                self.assertNotIn("full plan", refused.remedy)

    def test_a_malformed_reported_sha_names_its_report_field(self) -> None:
        justified = {"commit_map": MERGED, "divergence_justification": "merged"}
        cases: dict[str, tuple[JsonObject, str]] = {
            "dod_coverage commit": (
                {
                    **justified,
                    "dod_coverage": [
                        *MERGED_COVERAGE[:4],
                        {"dod_item": 5, "commits": ["not-a-sha"]},
                    ],
                },
                "dod_coverage[].commits",
            ),
            "commit_map commit": (
                {"commit_map": [*MERGED, *_pairs(("not-a-sha", "step-1"))]},
                "commit_map[].commit_sha",
            ),
        }
        for name, (report, field) in cases.items():
            with self.subTest(name):
                refused = self._refused(
                    report,
                    f"{re.escape(field)} entry 'not-a-sha' is not the hexadecimal SHA",
                    resolve=_known_or_git,
                )
                self.assertIn(field, refused.remedy)
                self.assertNotIn("candidate_commit", refused.message)
                self.assertNotIn("candidate_commit", refused.remedy)

    def test_coverage_fields_are_refused_outside_an_initial_or_rebase_developer_report(
        self,
    ) -> None:
        fields = {"dod_coverage": MERGED_COVERAGE, "divergence_justification": "x"}
        for name, dispatch in {
            "developer-retry": _dispatch(retry=True),
            "another role": _dispatch(role="code-review"),
            "no commit plan": _dispatch([]),
        }.items():
            for field, value in fields.items():
                with self.subTest(name, field=field):
                    with self.assertRaisesRegex(
                        CoordinatorError, f"\\['{field}'\\] belong only to"
                    ):
                        commit_plan.check_fields_allowed({field: value}, dispatch)
        commit_plan.check_fields_allowed(fields, _dispatch())
        commit_plan.check_fields_allowed({}, _dispatch(retry=True))


class DivergenceAndCoverageTests(unittest.TestCase):
    def test_divergence_names_merged_split_and_unclosed_entries(self) -> None:
        report = {
            "commit_map": _pairs(
                (C1, "step-2"), (C1, "step-1"), (C2, "step-3"), (C3, "step-3")
            ),
            "divergence_justification": "why",
        }

        record = commit_plan.divergence(report, _dispatch(), _identity)

        self.assertEqual(
            record,
            {
                "developer_dispatch_id": "dispatch-1",
                "justification": "why",
                "merged_commits": [
                    {"commit_sha": C1, "plan_entry_ids": ["step-1", "step-2"]}
                ],
                "split_entries": [{"plan_entry_id": "step-3", "commit_shas": [C2, C3]}],
                "unclosed_entries": ["step-4", "step-5"],
            },
        )

    def test_no_divergence_for_one_to_one_retry_or_other_roles(self) -> None:
        one_to_one = {
            "commit_map": _pairs((C1, "step-1"), (C2, "step-2"), (C3, "step-3"))
        }
        three = _dispatch(items=FIVE[:3])
        self.assertIsNone(commit_plan.divergence(one_to_one, three, _identity))
        merged = {"commit_map": MERGED}
        self.assertIsNone(
            commit_plan.divergence(merged, _dispatch(retry=True), _identity)
        )
        self.assertIsNone(
            commit_plan.divergence(merged, _dispatch(role="qa"), _identity)
        )

    def test_coverage_comes_from_the_report_or_is_derived_from_covers(self) -> None:
        report = {"commit_map": MERGED, "dod_coverage": MERGED_COVERAGE}
        self.assertEqual(
            commit_plan.coverage(report, _dispatch(), _identity),
            (MERGED_COVERAGE, "report"),
        )
        pinned = commit_plan.pinned_plan(
            {
                "commit_plan": [
                    {
                        "id": "first",
                        "summary": "s",
                        "expected_paths": ["**"],
                        "covers": [1, 2],
                    },
                    {
                        "id": "second",
                        "summary": "s",
                        "expected_paths": ["**"],
                        "covers": [3],
                    },
                ]
            },
            FIVE[:3],
        )
        derived = {"commit_map": _pairs((C1, "first"), (C2, "second"))}
        self.assertEqual(
            commit_plan.coverage(derived, _dispatch(pinned, items=FIVE[:3]), _identity),
            (_covered([C1], [C1], [C2]), "derived"),
        )
        legacy = [{"id": "step-1"}, {"id": "step-2"}]
        self.assertEqual(
            commit_plan.coverage(
                {"commit_map": _pairs((C1, "step-1"), (C2, "step-2"))},
                _dispatch(legacy, items=FIVE[:2]),
                _identity,
            ),
            (_covered([C1], [C2]), "derived"),
        )
        self.assertEqual(
            commit_plan.coverage(report, _dispatch(retry=True), _identity),
            (None, None),
        )


class NotCoveredTests(unittest.TestCase):
    def test_not_covered_lists_the_open_items_with_their_reasons(self) -> None:
        report = {
            "dod_coverage": [
                *MERGED_COVERAGE[:4],
                {"dod_item": 5, "not_covered": "moved to a follow-up issue"},
            ]
        }

        self.assertEqual(
            commit_plan.not_covered(report),
            [{"dod_item": 5, "reason": "moved to a follow-up issue"}],
        )
        self.assertEqual(commit_plan.not_covered({"dod_coverage": MERGED_COVERAGE}), [])
        self.assertEqual(commit_plan.not_covered({}), [])

    def test_is_developer_retry_reads_the_transition(self) -> None:
        self.assertTrue(commit_plan.is_developer_retry(_dispatch(retry=True)))
        self.assertFalse(commit_plan.is_developer_retry(_dispatch()))
        self.assertFalse(commit_plan.is_developer_retry({"role": "developer"}))


if __name__ == "__main__":
    unittest.main()
