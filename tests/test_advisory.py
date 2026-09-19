#!/usr/bin/env python3
"""Focused public-contract tests for the non-role advisory tool call."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_ROOT = Path(__file__).resolve().parents[1] / "harness" / "orchestration"
MODULE_PATH = MODULE_ROOT / "advisory.py"
from harness.orchestration.advisory import classify_risk, rank_files, summarize_log


def _imported_module_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


class AdvisoryStructureTests(unittest.TestCase):
    def test_module_has_no_ledger_or_coordinator_coupling(self) -> None:
        imports = _imported_module_names(MODULE_PATH)
        self.assertTrue(imports.isdisjoint({"ledger", "contract", "coordinator"}))

    def test_coordinator_does_not_import_advisory(self) -> None:
        self.assertNotIn("advisory", _imported_module_names(MODULE_ROOT / "coordinator.py"))


class RankFilesTests(unittest.TestCase):
    def test_ranks_by_keyword_hits_then_path(self) -> None:
        ranked = rank_files(
            ["services/payments/handler.py", "services/payments/tests/test_handler.py", "README.md"],
            ["payments", "handler"],
        )
        self.assertEqual(
            [entry["path"] for entry in ranked],
            ["services/payments/handler.py", "services/payments/tests/test_handler.py", "README.md"],
        )
        self.assertEqual(ranked[0]["score"], 2)
        self.assertEqual(ranked[-1]["score"], 0)

    def test_empty_keywords_yields_a_zero_score_for_every_path(self) -> None:
        ranked = rank_files(["a.py", "b.py"], [])
        self.assertEqual([entry["score"] for entry in ranked], [0, 0])


class SummarizeLogTests(unittest.TestCase):
    def test_flags_failure_lines_and_respects_max_lines(self) -> None:
        text = "\n".join([f"info line {i}" for i in range(5)] + ["ERROR: boom", "Traceback (most recent call last):"])
        summary = summarize_log(text, max_lines=1)
        self.assertEqual(summary["line_count"], 7)
        self.assertEqual(summary["flagged"], ["ERROR: boom"])
        self.assertEqual(summary["head"], ["info line 0"])


class ClassifyRiskTests(unittest.TestCase):
    def test_matches_known_triggers_by_word(self) -> None:
        hits = classify_risk("Add a data migration for the payments schema", ["data-migration", "outbox"])
        self.assertEqual(hits, ["data-migration"])

    def test_no_match_returns_empty(self) -> None:
        self.assertEqual(classify_risk("Fix a typo in the README", ["data-migration", "outbox"]), [])


class AdvisoryCliTests(unittest.TestCase):
    def test_output_is_ephemeral_json_and_writes_no_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            before = sorted(Path(temporary).rglob("*"))
            result = subprocess.run(
                [sys.executable, str(MODULE_PATH), "rank-files", "--keyword", "payments", "a.py", "services/payments/x.py"],
                cwd=temporary,
                check=True,
                capture_output=True,
                text=True,
            )
            after = sorted(Path(temporary).rglob("*"))
        self.assertEqual(before, after)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["advisory"], True)
        self.assertEqual(payload["kind"], "rank-files")

    def test_repeated_calls_regenerate_the_same_output(self) -> None:
        args = [sys.executable, str(MODULE_PATH), "classify-risk", "--text", "schema change", "--known-trigger", "schema-change"]
        first = subprocess.run(args, check=True, capture_output=True, text=True).stdout
        second = subprocess.run(args, check=True, capture_output=True, text=True).stdout
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
