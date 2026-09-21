#!/usr/bin/env python3
"""Tests for scripts/diff_coverage.py: only executable statements count toward the gate, files nobody
imports still appear in the coverage run, and the 70% threshold is exact (issue #231)."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "diff_coverage.py"


def _load() -> types.ModuleType:
    loader = importlib.machinery.SourceFileLoader(
        "diff_coverage_under_test", str(SCRIPT)
    )
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

        self.assertEqual(
            diff_coverage.summarize_coverage(changed, files), (0, 1, ["pkg/mod.py:2"])
        )

    def test_a_changed_file_never_imported_counts_as_uncovered(self) -> None:
        # coverage run --source lists a never-imported file with every statement missing.
        changed = {"scripts/tool.py": {1, 2, 4}}
        files = {"scripts/tool.py": _file(executed=[], missing=[1, 2, 4])}

        covered, total, _ = diff_coverage.summarize_coverage(changed, files)

        self.assertEqual((covered, total), (0, 3))

    def test_a_changed_file_absent_from_the_report_is_fully_uncovered(self) -> None:
        covered, total, uncovered = diff_coverage.summarize_coverage(
            {"x/unmeasured.py": {3, 4}}, {}
        )

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
                    str(
                        diff_coverage.ROOT
                        / "skills/first-party/pvmalove/qa-gate/scripts"
                    ),
                }
            ),
        )


class RepoRelativeTests(unittest.TestCase):
    def test_absolute_native_path_under_the_root_becomes_repo_relative(self) -> None:
        native = str(diff_coverage.ROOT / "harness" / "a.py").replace("/", "\\")

        self.assertEqual(diff_coverage._repo_relative(native), "harness/a.py")

    def test_relative_backslash_path_is_only_normalised(self) -> None:
        self.assertEqual(diff_coverage._repo_relative("harness\\a.py"), "harness/a.py")

    def test_absolute_path_outside_the_root_is_left_absolute(self) -> None:
        self.assertEqual(
            diff_coverage._repo_relative("/elsewhere/a.py"), "/elsewhere/a.py"
        )


class MainCoverageRunTests(unittest.TestCase):
    def test_coverage_run_is_given_the_changed_file_directories_as_sources(
        self,
    ) -> None:
        changed = {"harness/a.py": {1}, "scripts/tool.py": {2}}
        with (
            tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary,
            mock.patch.object(diff_coverage, "_merge_base", return_value="base"),
            mock.patch.object(diff_coverage, "_changed_lines", return_value=changed),
            mock.patch.object(
                diff_coverage, "COVERAGE_DATA_FILE", Path(temporary) / ".coverage"
            ),
            mock.patch.dict(diff_coverage.os.environ, {}),
            mock.patch.object(
                diff_coverage.subprocess,
                "run",
                return_value=types.SimpleNamespace(returncode=3),
            ) as run,
        ):
            exit_code = diff_coverage.main()

        self.assertEqual(exit_code, 3)
        command = run.call_args.args[0]
        self.assertIn(
            f"--source={','.join(diff_coverage.source_dirs(changed))}", command
        )


class ChangedLinesTests(unittest.TestCase):
    def _git(self, root: Path, *args: str) -> str:
        return subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@t",
                *args,
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()

    def test_a_file_renamed_to_py_counts_only_its_real_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._git(root, "init", "-q")
            (root / "tool").write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
            self._git(root, "add", "-A")
            self._git(root, "commit", "-q", "-m", "base")
            base = self._git(root, "rev-parse", "HEAD")
            self._git(root, "mv", "tool", "tool.py")
            (root / "tool.py").write_text("a = 1\nb = 20\nc = 3\n", encoding="utf-8")
            (root / "notes.txt").write_text("not python\n", encoding="utf-8")
            self._git(root, "add", "-A")
            self._git(root, "commit", "-q", "-m", "rename")

            with mock.patch.object(diff_coverage, "ROOT", root):
                changed = diff_coverage._changed_lines(base)

        self.assertEqual(changed, {"tool.py": {2}})

    def test_the_clean_room_script_is_not_gated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._git(root, "init", "-q")
            (root / "scripts").mkdir()
            (root / "scripts" / "test_clean_room.py").write_text(
                "a = 1\n", encoding="utf-8"
            )
            (root / "other.py").write_text("b = 1\n", encoding="utf-8")
            self._git(root, "add", "-A")
            self._git(root, "commit", "-q", "-m", "base")
            base = self._git(root, "rev-parse", "HEAD")
            (root / "scripts" / "test_clean_room.py").write_text(
                "a = 2\n", encoding="utf-8"
            )
            (root / "other.py").write_text("b = 2\n", encoding="utf-8")
            self._git(root, "add", "-A")
            self._git(root, "commit", "-q", "-m", "change")

            with mock.patch.object(diff_coverage, "ROOT", root):
                changed = diff_coverage._changed_lines(base)

        self.assertEqual(changed, {"other.py": {1}})


if __name__ == "__main__":
    unittest.main()
