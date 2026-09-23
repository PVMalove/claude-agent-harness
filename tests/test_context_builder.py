#!/usr/bin/env python3
"""Focused public-contract tests for the standalone Context Package builder."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import patch

from harness.context_builder.context_builder import (
    ContextPackageError,
    build_context_package,
    estimate_tokens,
)
from harness.errors import HarnessError

MODULE_ROOT = Path(__file__).resolve().parents[1] / "harness" / "context_builder"


def _run(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _write(repo: Path, path: str, content: str) -> None:
    file_path = repo / path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")


class ContextBuilderFixture(unittest.TestCase):
    """Build a small real repository: a package with an import edge, its test, and an ADR."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
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
        _write(
            self.repo,
            "pkg/second_hop.py",
            "def hidden_detail():\n    return 'not direct'\n",
        )
        _write(
            self.repo,
            "pkg/consumer.py",
            "from pkg import base\n\ndef use():\n    return base.helper()\n",
        )
        _write(
            self.repo,
            "pkg/indirect.py",
            "from pkg import consumer\n\ndef call():\n    return consumer.use()\n",
        )
        _write(
            self.repo,
            "tests/test_base.py",
            "from pkg import base\n\ndef test_helper():\n    assert base.helper() == 1\n",
        )
        _write(
            self.repo,
            "docs/adr/0001-base-module.md",
            "# Base module contract\n\nDescribes the base helper contract used across the pkg package.\n",
        )
        _run("add", ".", cwd=self.repo)
        _run("commit", "-qm", "feat: base module and its adr", cwd=self.repo)
        self.base_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        _write(
            self.repo,
            "pkg/base.py",
            "from pkg import dependency\n\n\ndef helper():\n    return dependency.compose(2)\n",
        )
        _run("add", ".", cwd=self.repo)
        _run("commit", "-qm", "fix: change base helper return value", cwd=self.repo)
        self.candidate_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def tearDown(self) -> None:
        self._temporary.cleanup()


class ContextBuilderTests(ContextBuilderFixture):
    def test_builds_diff_files_graph_tests_cards_and_hashes(self) -> None:
        package = build_context_package(
            self.repo,
            self.base_commit,
            self.candidate_commit,
            min_starting_files=1,
            max_starting_files=10,
        )

        self.assertEqual(package.base_commit, self.base_commit)
        self.assertEqual(package.candidate_commit, self.candidate_commit)
        self.assertIn("dependency.compose(2)", package.diff)
        self.assertIn("return 1", package.diff)

        paths = {item.path for item in package.starting_files}
        self.assertIn("pkg/base.py", paths)
        base_reason = next(
            item.reason for item in package.starting_files if item.path == "pkg/base.py"
        )
        self.assertIn("changed in diff", base_reason)

        self.assertIn("pkg/base.py", package.symbol_graph)
        self.assertIn(
            "pkg/consumer.py", package.symbol_graph["pkg/base.py"]["imported_by"]
        )

        self.assertEqual(package.related_tests, ["tests/test_base.py"])

        self.assertTrue(package.precedent_cards)
        self.assertEqual(package.precedent_cards[0].id, "0001-base-module")
        self.assertEqual(package.precedent_cards[0].title, "Base module contract")

        self.assertIn("pkg/base.py", package.file_hashes)
        self.assertIn("tests/test_base.py", package.file_hashes)
        self.assertGreater(package.size_bytes, 0)

    def test_output_is_byte_identical_across_repeated_builds(self) -> None:
        first = build_context_package(
            self.repo, self.base_commit, self.candidate_commit, min_starting_files=1
        )
        second = build_context_package(
            self.repo, self.base_commit, self.candidate_commit, min_starting_files=1
        )

        self.assertEqual(first.to_json(), second.to_json())

    def test_expands_below_minimum_starting_files_via_the_import_graph(self) -> None:
        package = build_context_package(
            self.repo,
            self.base_commit,
            self.candidate_commit,
            min_starting_files=2,
            max_starting_files=10,
        )

        paths = {item.path for item in package.starting_files}
        self.assertGreaterEqual(len(paths), 2)
        self.assertIn("pkg/base.py", paths)
        self.assertIn("pkg/consumer.py", paths)
        consumer_reason = next(
            item.reason
            for item in package.starting_files
            if item.path == "pkg/consumer.py"
        )
        self.assertIn("pkg/base.py", consumer_reason)

    def test_fails_clearly_instead_of_truncating_when_the_size_limit_is_exceeded(
        self,
    ) -> None:
        with self.assertRaises(ContextPackageError):
            build_context_package(
                self.repo,
                self.base_commit,
                self.candidate_commit,
                min_starting_files=1,
                max_package_size_bytes=1,
            )

    def test_reports_a_deterministic_token_estimate_and_enforces_it(self) -> None:
        package = build_context_package(
            self.repo,
            self.base_commit,
            self.candidate_commit,
            min_starting_files=1,
            max_package_tokens=10_000,
        )

        self.assertGreater(package.estimated_tokens, 0)
        with self.assertRaisesRegex(ContextPackageError, "max_package_tokens"):
            build_context_package(
                self.repo,
                self.base_commit,
                self.candidate_commit,
                min_starting_files=1,
                max_package_tokens=1,
            )

    def test_fails_clearly_instead_of_silently_returning_fewer_than_the_minimum_starting_files(
        self,
    ) -> None:
        with self.assertRaises(ContextPackageError):
            build_context_package(
                self.repo, self.base_commit, self.candidate_commit, min_starting_files=5
            )

    def test_symbol_graph_depth_bounds_how_far_indirect_dependents_are_included(
        self,
    ) -> None:
        shallow = build_context_package(
            self.repo,
            self.base_commit,
            self.candidate_commit,
            min_starting_files=1,
            symbol_graph_depth=1,
        )
        deep = build_context_package(
            self.repo,
            self.base_commit,
            self.candidate_commit,
            min_starting_files=1,
            symbol_graph_depth=2,
        )

        self.assertNotIn("pkg/indirect.py", shallow.symbol_graph)
        self.assertIn("pkg/indirect.py", deep.symbol_graph)

    def test_adds_ast_signatures_for_direct_dependencies_without_following_second_hop(
        self,
    ) -> None:
        package = build_context_package(
            self.repo, self.base_commit, self.candidate_commit, min_starting_files=1
        )

        self.assertEqual(
            package.symbol_graph["pkg/dependency.py"]["context"],
            [
                "module",
                "class Service:",
                "def compose(value: int) -> Service:",
                "async def fetch() -> None:",
            ],
        )
        self.assertNotIn("context", package.symbol_graph["pkg/second_hop.py"])

    def test_uses_first_thirty_lines_for_a_non_python_direct_dependency_from_the_public_builder(
        self,
    ) -> None:
        _write(
            self.repo,
            "pkg/notes.txt",
            "\n".join(f"line {index}" for index in range(35)),
        )
        _run("add", ".", cwd=self.repo)
        _run("commit", "-qm", "test: add text dependency", cwd=self.repo)
        base_with_notes = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        _write(
            self.repo,
            "pkg/base.py",
            "from pkg import dependency, notes\n\n\ndef helper():\n    return dependency.compose(2)\n",
        )
        _run("add", ".", cwd=self.repo)
        _run("commit", "-qm", "test: import text dependency", cwd=self.repo)
        candidate_with_notes = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        package = build_context_package(
            self.repo, base_with_notes, candidate_with_notes, min_starting_files=1
        )

        self.assertEqual(
            package.symbol_graph["pkg/notes.txt"]["context"],
            [f"line {index}" for index in range(30)],
        )

    def test_fails_clearly_instead_of_silently_including_too_many_related_tests(
        self,
    ) -> None:
        with self.assertRaisesRegex(ContextPackageError, "max_related_tests"):
            build_context_package(
                self.repo,
                self.base_commit,
                self.candidate_commit,
                min_starting_files=1,
                max_related_tests=0,
            )

    def test_added_file_content_is_not_double_counted_against_its_diff(self) -> None:
        _write(
            self.repo,
            "pkg/generated.py",
            "\n".join(f"VALUE_{index} = {index}" for index in range(400)),
        )
        _run("add", ".", cwd=self.repo)
        _run("commit", "-qm", "feat: add a large generated file", cwd=self.repo)
        candidate_with_addition = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        package = build_context_package(
            self.repo,
            self.candidate_commit,
            candidate_with_addition,
            min_starting_files=1,
        )

        self.assertIn(
            "pkg/generated.py", {item.path for item in package.starting_files}
        )
        # A brand-new file's unified diff already contains 100% of its content as `+` lines.
        # Re-reading and re-counting that same content for the token estimate would roughly
        # double it for this dominant file; assert it stays close to the diff-only estimate
        # instead of drifting toward double.
        diff_only_estimate = estimate_tokens(package.diff)
        self.assertLess(package.estimated_tokens, diff_only_estimate * 1.5)

    def test_every_raise_path_is_a_harness_error_with_a_message_and_a_remedy(
        self,
    ) -> None:
        repo, base, candidate = self.repo, self.base_commit, self.candidate_commit
        cases: tuple[tuple[str, str, Callable[[], object]], ...] = (
            (
                "git diff",
                "git diff",
                lambda: build_context_package(repo, base, "0" * 40),
            ),
            (
                "direct import-graph neighbours",
                "lower min_starting_files below 5",
                lambda: build_context_package(
                    repo, base, candidate, min_starting_files=5
                ),
            ),
            (
                "must be >=1",
                "got min=3, max=2",
                lambda: build_context_package(
                    repo, base, candidate, min_starting_files=3, max_starting_files=2
                ),
            ),
            (
                "static starting file",
                f"seed_paths that exist at {candidate}",
                lambda: build_context_package(
                    repo,
                    candidate,
                    candidate,
                    min_starting_files=999,
                    max_starting_files=1000,
                ),
            ),
            (
                "max_related_tests=0",
                "max_related_tests above 1",
                lambda: build_context_package(
                    repo, base, candidate, min_starting_files=1, max_related_tests=0
                ),
            ),
            (
                "max_package_size_bytes=1",
                "raise max_package_size_bytes above",
                lambda: build_context_package(
                    repo,
                    base,
                    candidate,
                    min_starting_files=1,
                    max_package_size_bytes=1,
                ),
            ),
            (
                "max_package_tokens=1",
                "max_package_tokens above",
                lambda: build_context_package(
                    repo, base, candidate, min_starting_files=1, max_package_tokens=1
                ),
            ),
        )
        for expected_message, expected_remedy, build in cases:
            with self.subTest(expected_message=expected_message):
                with self.assertRaises(ContextPackageError) as raised:
                    build()
                self.assertIsInstance(raised.exception, HarnessError)
                self.assertIn(expected_message, raised.exception.message)
                self.assertIn(expected_remedy, raised.exception.remedy)

    def test_makes_no_model_call_and_stays_pure_python_over_git_plumbing(self) -> None:
        module_source = (MODULE_ROOT / "context_builder.py").read_text(encoding="utf-8")
        for banned in ("anthropic", "openai", "requests.post", "http://", "https://"):
            self.assertNotIn(banned, module_source)

    def test_a_policy_denied_changed_file_never_reaches_the_package(self) -> None:
        """A path denied by `repo_map_policy.deny_paths` must not become a starting file,
        appear in `symbol_graph`, or be hashed into the package, even though it is a changed file."""
        (self.repo / ".harness").mkdir(exist_ok=True)
        (self.repo / ".harness" / "orchestration.json").write_text(
            json.dumps({"repo_map_policy": {"deny_paths": ["pkg/base.py"]}}),
            encoding="utf-8",
        )

        package = build_context_package(
            self.repo,
            self.base_commit,
            self.candidate_commit,
            min_starting_files=1,
        )

        paths = {item.path for item in package.starting_files}
        self.assertNotIn("pkg/base.py", paths)
        self.assertNotIn("pkg/base.py", package.symbol_graph)
        self.assertNotIn("pkg/base.py", package.file_hashes)


def _patch_repo_map_call(
    fake: subprocess.CompletedProcess[str],
) -> AbstractContextManager[object]:
    """Patch only the Repo Map subprocess call, leaving `_run_git`'s own `subprocess.run` calls
    (issued through the same module attribute) untouched."""
    real_run = subprocess.run

    def _dispatch(
        args: list[str],
        *,
        capture_output: bool = True,
        text: bool = True,
        encoding: str = "utf-8",
        errors: str = "replace",
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        if any(str(item).endswith("repo_map.py") for item in args):
            return fake
        return real_run(
            args,
            capture_output=capture_output,
            text=text,
            encoding=encoding,
            errors=errors,
            check=check,
        )

    return patch("harness.context_builder.context_builder.subprocess.run", side_effect=_dispatch)


class RepoMapContractTests(ContextBuilderFixture):
    """`_run_repo_map`'s error mapping: only JSON crosses the process boundary."""

    def test_invalid_json_on_stdout_raises_a_contract_error_with_a_remedy(self) -> None:
        fake = subprocess.CompletedProcess(
            args=["repo_map"], returncode=0, stdout="not json", stderr=""
        )
        with _patch_repo_map_call(fake):
            with self.assertRaises(ContextPackageError) as raised:
                build_context_package(
                    self.repo, self.base_commit, self.candidate_commit, min_starting_files=1
                )
        self.assertIn("invalid JSON", raised.exception.message)
        self.assertTrue(raised.exception.remedy)

    def test_a_missing_required_field_raises_a_contract_error_with_a_remedy(self) -> None:
        incomplete = json.dumps({"schema_version": 1, "commit": "x" * 40})
        fake = subprocess.CompletedProcess(
            args=["repo_map"], returncode=0, stdout=incomplete, stderr=""
        )
        with _patch_repo_map_call(fake):
            with self.assertRaises(ContextPackageError) as raised:
                build_context_package(
                    self.repo, self.base_commit, self.candidate_commit, min_starting_files=1
                )
        self.assertIn("contract violation", raised.exception.message)
        self.assertIn("repo_map.schema.json", raised.exception.remedy)

    def test_an_unsupported_schema_version_raises_a_contract_error_with_a_remedy(
        self,
    ) -> None:
        payload = json.dumps(
            {
                "schema_version": 2,
                "commit": "x" * 40,
                "tier": "minimal",
                "parser": "path-only",
                "degradation_reason": "n/a",
                "token_estimator_version": "n/a",
                "parser_provenance": {},
                "files": [],
                "edges": [],
                "diagnostics": [],
                "estimated_tokens": 0,
            }
        )
        fake = subprocess.CompletedProcess(
            args=["repo_map"], returncode=0, stdout=payload, stderr=""
        )
        with _patch_repo_map_call(fake):
            with self.assertRaises(ContextPackageError) as raised:
                build_context_package(
                    self.repo, self.base_commit, self.candidate_commit, min_starting_files=1
                )
        self.assertIn("schema_version", raised.exception.message)
        self.assertTrue(raised.exception.remedy)

    def test_a_nonzero_exit_parses_the_error_remedy_stderr_contract(self) -> None:
        fake = subprocess.CompletedProcess(
            args=["repo_map"],
            returncode=2,
            stdout="",
            stderr="ERROR: something went wrong\nREMEDY: do the specific fix\n",
        )
        with _patch_repo_map_call(fake):
            with self.assertRaises(ContextPackageError) as raised:
                build_context_package(
                    self.repo, self.base_commit, self.candidate_commit, min_starting_files=1
                )
        self.assertIn("something went wrong", raised.exception.message)
        self.assertIn("do the specific fix", raised.exception.remedy)

    def test_a_nonzero_exit_with_unrecognised_stderr_still_raises_with_a_remedy(
        self,
    ) -> None:
        fake = subprocess.CompletedProcess(
            args=["repo_map"], returncode=1, stdout="", stderr="totally unexpected crash\n"
        )
        with _patch_repo_map_call(fake):
            with self.assertRaises(ContextPackageError) as raised:
                build_context_package(
                    self.repo, self.base_commit, self.candidate_commit, min_starting_files=1
                )
        self.assertIn("totally unexpected crash", raised.exception.message)
        self.assertTrue(raised.exception.remedy)


if __name__ == "__main__":
    unittest.main()
