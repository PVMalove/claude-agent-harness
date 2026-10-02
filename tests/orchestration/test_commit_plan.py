#!/usr/bin/env python3
"""Unit tests for the pure commit-plan rules (issue #478)."""

from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
