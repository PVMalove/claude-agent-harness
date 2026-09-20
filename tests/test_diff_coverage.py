#!/usr/bin/env python3
"""Tests for scripts/diff-coverage: only executable statements count toward the gate, files nobody
imports still appear in the coverage run, and the 70% threshold is exact (issue #231)."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import types
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "diff-coverage"


def _load() -> types.ModuleType:
    loader = importlib.machinery.SourceFileLoader("diff_coverage_under_test", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


diff_coverage = _load()


def _file(executed: list[int], missing: list[int]) -> dict[str, list[int]]:
    return {"executed_lines": executed, "missing_lines": missing, "excluded_lines": []}


class SummarizeCoverageTests(unittest.TestCase):
    def test_lines_that_are_not_statements_are_not_counted(self) -> None:
        # Lines 2 (blank), 3 (comment) and 5 (continuation of the statement at 4) are added but are
        # not statements, so they must not enter the denominator.
        changed = {"pkg/mod.py": {1, 2, 3, 4, 5}}
        files = {"pkg/mod.py": _file(executed=[1, 4], missing=[])}

        covered, total, uncovered = diff_coverage.summarize_coverage(changed, files)

        self.assertEqual((covered, total, uncovered), (2, 2, []))

    def test_a_missing_statement_is_counted_and_reported(self) -> None:
        changed = {"pkg/mod.py": {1, 2, 3}}
        files = {"pkg/mod.py": _file(executed=[1], missing=[2, 3])}

        covered, total, uncovered = diff_coverage.summarize_coverage(changed, files)

        self.assertEqual((covered, total), (1, 3))
        self.assertEqual(uncovered, ["pkg/mod.py:2", "pkg/mod.py:3"])

    def test_only_changed_lines_are_counted(self) -> None:
        changed = {"pkg/mod.py": {2}}
        files = {"pkg/mod.py": _file(executed=[1, 3], missing=[2])}

        self.assertEqual(diff_coverage.summarize_coverage(changed, files), (0, 1, ["pkg/mod.py:2"]))

    def test_a_changed_file_never_imported_counts_as_uncovered(self) -> None:
        # coverage run --source lists a never-imported file with every statement missing.
        changed = {"scripts/tool.py": {1, 2, 4}}
        files = {"scripts/tool.py": _file(executed=[], missing=[1, 2, 4])}

        covered, total, _ = diff_coverage.summarize_coverage(changed, files)

        self.assertEqual((covered, total), (0, 3))

    def test_a_changed_file_absent_from_the_report_is_fully_uncovered(self) -> None:
        covered, total, uncovered = diff_coverage.summarize_coverage({"x/unmeasured.py": {3, 4}}, {})

        self.assertEqual((covered, total), (0, 2))
        self.assertEqual(uncovered, ["x/unmeasured.py:3", "x/unmeasured.py:4"])

    def test_statement_free_change_has_nothing_to_cover(self) -> None:
        changed = {"pkg/mod.py": {1, 2}}
        files = {"pkg/mod.py": _file(executed=[5], missing=[6])}

        self.assertEqual(diff_coverage.summarize_coverage(changed, files), (0, 0, []))


class ThresholdTests(unittest.TestCase):
    def test_threshold_is_still_seventy_percent(self) -> None:
        self.assertEqual(diff_coverage.THRESHOLD_PERCENT, 70.0)

    def test_exactly_the_threshold_passes(self) -> None:
        self.assertTrue(diff_coverage.meets_threshold(7, 10))
        self.assertTrue(diff_coverage.meets_threshold(70, 100))

    def test_just_below_the_threshold_fails(self) -> None:
        self.assertFalse(diff_coverage.meets_threshold(6999, 10000))
        self.assertFalse(diff_coverage.meets_threshold(69, 100))

    def test_full_coverage_passes(self) -> None:
        self.assertTrue(diff_coverage.meets_threshold(5, 5))


class SourceDirsTests(unittest.TestCase):
    def test_every_changed_file_directory_is_a_coverage_source(self) -> None:
        changed = {
            "harness/a.py": {1},
            "harness/b.py": {1},
            "skills/first-party/pvmalove/qa-gate/scripts/test_summary.py": {1},
        }

        sources = diff_coverage.source_dirs(changed)

        self.assertEqual(
            sources,
            sorted(
                {
                    str(diff_coverage.ROOT / "harness"),
                    str(diff_coverage.ROOT / "skills/first-party/pvmalove/qa-gate/scripts"),
                }
            ),
        )


if __name__ == "__main__":
    unittest.main()
