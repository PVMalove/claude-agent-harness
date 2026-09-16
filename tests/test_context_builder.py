#!/usr/bin/env python3
"""Focused public-contract tests for the standalone Context Package builder."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_ROOT = Path(__file__).resolve().parents[1] / "harness" / "context_builder"
sys.path.insert(0, str(MODULE_ROOT))
from context_builder import (  # noqa: E402
    ContextPackageError,
    build_context_package,
)


def _run(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _write(repo: Path, path: str, content: str) -> None:
    file_path = repo / path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")


class ContextBuilderFixture(unittest.TestCase):
    """Build a small real repository: a package with an import edge, its test, and an ADR."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self._temporary.name) / "repo"
        self.repo.mkdir()
        _run("init", "-q", cwd=self.repo)
        _run("config", "user.email", "test@example.invalid", cwd=self.repo)
        _run("config", "user.name", "Context Builder Test", cwd=self.repo)

        _write(self.repo, "pkg/base.py", "def helper():\n    return 1\n")
        _write(
            self.repo,
            "pkg/dependency.py",
            "from pkg import second_hop\n\n"
            "class Service:\n    pass\n\n"
            "def compose(value: int) -> Service:\n    return Service()\n\n"
            "async def fetch() -> None:\n    return None\n",
        )
        _write(self.repo, "pkg/second_hop.py", "def hidden_detail():\n    return 'not direct'\n")
        _write(self.repo, "pkg/consumer.py", "from pkg import base\n\ndef use():\n    return base.helper()\n")
        _write(self.repo, "pkg/indirect.py", "from pkg import consumer\n\ndef call():\n    return consumer.use()\n")
        _write(self.repo, "tests/test_base.py", "from pkg import base\n\ndef test_helper():\n    assert base.helper() == 1\n")
        _write(
            self.repo,
            "docs/adr/0001-base-module.md",
            "# Base module contract\n\nDescribes the base helper contract used across the pkg package.\n",
        )
        _run("add", ".", cwd=self.repo)
        _run("commit", "-qm", "feat: base module and its adr", cwd=self.repo)
        self.base_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, check=True, capture_output=True, text=True
        ).stdout.strip()

        _write(
            self.repo,
            "pkg/base.py",
            "from pkg import dependency\n\n\ndef helper():\n    return dependency.compose(2)\n",
        )
        _run("add", ".", cwd=self.repo)
        _run("commit", "-qm", "fix: change base helper return value", cwd=self.repo)
        self.candidate_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, check=True, capture_output=True, text=True
        ).stdout.strip()

    def tearDown(self) -> None:
        self._temporary.cleanup()


class ContextBuilderTests(ContextBuilderFixture):
    def test_builds_diff_files_graph_tests_cards_and_hashes(self) -> None:
        package = build_context_package(
            self.repo, self.base_commit, self.candidate_commit, min_starting_files=1, max_starting_files=10
        )

        self.assertEqual(package.base_commit, self.base_commit)
        self.assertEqual(package.candidate_commit, self.candidate_commit)
        self.assertIn("dependency.compose(2)", package.diff)
        self.assertIn("return 1", package.diff)

        paths = {item.path for item in package.starting_files}
        self.assertIn("pkg/base.py", paths)
        base_reason = next(item.reason for item in package.starting_files if item.path == "pkg/base.py")
        self.assertIn("changed in diff", base_reason)

        self.assertIn("pkg/base.py", package.symbol_graph)
        self.assertIn("pkg/consumer.py", package.symbol_graph["pkg/base.py"]["imported_by"])

        self.assertEqual(package.related_tests, ["tests/test_base.py"])

        self.assertTrue(package.precedent_cards)
        self.assertEqual(package.precedent_cards[0].id, "0001-base-module")
        self.assertEqual(package.precedent_cards[0].title, "Base module contract")

        self.assertIn("pkg/base.py", package.file_hashes)
        self.assertIn("tests/test_base.py", package.file_hashes)
        self.assertGreater(package.size_bytes, 0)

    def test_output_is_byte_identical_across_repeated_builds(self) -> None:
        first = build_context_package(self.repo, self.base_commit, self.candidate_commit, min_starting_files=1)
        second = build_context_package(self.repo, self.base_commit, self.candidate_commit, min_starting_files=1)

        self.assertEqual(first.to_json(), second.to_json())

    def test_expands_below_minimum_starting_files_via_the_import_graph(self) -> None:
        package = build_context_package(
            self.repo, self.base_commit, self.candidate_commit, min_starting_files=2, max_starting_files=10
        )

        paths = {item.path for item in package.starting_files}
        self.assertGreaterEqual(len(paths), 2)
        self.assertIn("pkg/base.py", paths)
        self.assertIn("pkg/consumer.py", paths)
        consumer_reason = next(item.reason for item in package.starting_files if item.path == "pkg/consumer.py")
        self.assertIn("pkg/base.py", consumer_reason)

    def test_fails_clearly_instead_of_truncating_when_the_size_limit_is_exceeded(self) -> None:
        with self.assertRaises(ContextPackageError):
            build_context_package(
                self.repo, self.base_commit, self.candidate_commit, min_starting_files=1, max_package_size_bytes=1
            )

    def test_reports_a_deterministic_token_estimate_and_enforces_it(self) -> None:
        package = build_context_package(
            self.repo, self.base_commit, self.candidate_commit, min_starting_files=1, max_package_tokens=10_000
        )

        self.assertGreater(package.estimated_tokens, 0)
        with self.assertRaisesRegex(ContextPackageError, "max_package_tokens"):
            build_context_package(
                self.repo, self.base_commit, self.candidate_commit, min_starting_files=1, max_package_tokens=1
            )

    def test_fails_clearly_instead_of_silently_returning_fewer_than_the_minimum_starting_files(self) -> None:
        with self.assertRaises(ContextPackageError):
            build_context_package(self.repo, self.base_commit, self.candidate_commit, min_starting_files=5)

    def test_symbol_graph_depth_bounds_how_far_indirect_dependents_are_included(self) -> None:
        shallow = build_context_package(
            self.repo, self.base_commit, self.candidate_commit, min_starting_files=1, symbol_graph_depth=1
        )
        deep = build_context_package(
            self.repo, self.base_commit, self.candidate_commit, min_starting_files=1, symbol_graph_depth=2
        )

        self.assertNotIn("pkg/indirect.py", shallow.symbol_graph)
        self.assertIn("pkg/indirect.py", deep.symbol_graph)

    def test_adds_ast_signatures_for_direct_dependencies_without_following_second_hop(self) -> None:
        package = build_context_package(self.repo, self.base_commit, self.candidate_commit, min_starting_files=1)

        self.assertEqual(
            package.symbol_graph["pkg/dependency.py"]["context"],
            ["module", "class Service:", "def compose(value: int) -> Service:", "async def fetch() -> None:"],
        )
        self.assertNotIn("context", package.symbol_graph["pkg/second_hop.py"])

    def test_uses_first_thirty_lines_for_a_non_python_direct_dependency_from_the_public_builder(self) -> None:
        _write(self.repo, "pkg/notes.txt", "\n".join(f"line {index}" for index in range(35)))
        _run("add", ".", cwd=self.repo)
        _run("commit", "-qm", "test: add text dependency", cwd=self.repo)
        base_with_notes = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, check=True, capture_output=True, text=True
        ).stdout.strip()
        _write(
            self.repo,
            "pkg/base.py",
            "from pkg import dependency, notes\n\n\ndef helper():\n    return dependency.compose(2)\n",
        )
        _run("add", ".", cwd=self.repo)
        _run("commit", "-qm", "test: import text dependency", cwd=self.repo)
        candidate_with_notes = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, check=True, capture_output=True, text=True
        ).stdout.strip()

        package = build_context_package(self.repo, base_with_notes, candidate_with_notes, min_starting_files=1)

        self.assertEqual(
            package.symbol_graph["pkg/notes.txt"]["context"],
            [f"line {index}" for index in range(30)],
        )

    def test_makes_no_model_call_and_stays_pure_python_over_git_plumbing(self) -> None:
        module_source = (MODULE_ROOT / "context_builder.py").read_text(encoding="utf-8")
        for banned in ("anthropic", "openai", "requests.post", "http://", "https://"):
            self.assertNotIn(banned, module_source)


if __name__ == "__main__":
    unittest.main()
