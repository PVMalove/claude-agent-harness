#!/usr/bin/env python3
"""Focused public-contract tests for the standalone Context Package builder.

The default (offline, bundle-free) test run exercises Repo Map's minimal tier: no tree-sitter
grammar bundle is configured, so Repo Map returns empty `edges` and no `signatures` for every file
(ADR 0024/0023 degradation). Graph widening, symbol-graph depth, and dependency-signature behaviour
that need real edges are therefore covered separately by the `HARNESS_PARSER_BUNDLE_DIR`-gated tests
below, following the pattern in tests/test_repo_map_tree_sitter.py: unset, they skip; set with a
missing bundle, they fail (an unavailable artifact is a failure, not a silent skip).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import patch

import pytest

from harness.context_builder.context_builder import (
    ContextPackageError,
    _dependency_context,
    _fallback_excerpt,
    build_context_package,
    estimate_tokens,
)
from harness.errors import HarnessError

MODULE_ROOT = Path(__file__).resolve().parents[1] / "harness" / "context_builder"
BUNDLE_ENV = "HARNESS_PARSER_BUNDLE_DIR"


def _run(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _write(repo: Path, path: str, content: str) -> None:
    file_path = repo / path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


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
        self.base_commit = _head(self.repo)

        _write(
            self.repo,
            "pkg/base.py",
            "from pkg import dependency\n\n\ndef helper():\n    return dependency.compose(2)\n",
        )
        _run("add", ".", cwd=self.repo)
        _run("commit", "-qm", "fix: change base helper return value", cwd=self.repo)
        self.candidate_commit = _head(self.repo)

    def tearDown(self) -> None:
        self._temporary.cleanup()


class ContextBuilderTests(ContextBuilderFixture):
    """Public-builder behaviour under Repo Map's minimal (bundle-free) tier."""

    def test_builds_diff_starting_files_cards_and_hashes(self) -> None:
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

        # Repo Map has no grammar bundle configured, so it runs at minimal tier: no edges are
        # returned at all, and `symbol_graph` reflects that honestly instead of inventing a graph.
        self.assertEqual(
            package.symbol_graph["pkg/base.py"],
            {"imports": [], "imported_by": [], "references": [], "referenced_by": []},
        )
        self.assertEqual(package.related_tests, [])

        self.assertTrue(package.precedent_cards)
        self.assertEqual(package.precedent_cards[0].id, "0001-base-module")
        self.assertEqual(package.precedent_cards[0].title, "Base module contract")

        self.assertIn("pkg/base.py", package.file_hashes)
        self.assertGreater(package.size_bytes, 0)

        self.assertEqual(package.schema_version, 2)
        self.assertEqual(package.parser, "path-only")
        assert package.parser_provenance is not None
        self.assertEqual(package.parser_provenance["tier"], "minimal")
        self.assertEqual(package.parser_provenance["parser"], "path-only")
        self.assertIn("degradation_reason", package.parser_provenance)
        self.assertIn("token_estimator_version", package.parser_provenance)
        self.assertIsInstance(package.parser_provenance["parser_provenance"], dict)

    def test_output_is_byte_identical_across_repeated_builds(self) -> None:
        first = build_context_package(
            self.repo, self.base_commit, self.candidate_commit, min_starting_files=1
        )
        second = build_context_package(
            self.repo, self.base_commit, self.candidate_commit, min_starting_files=1
        )

        self.assertEqual(first.to_json(), second.to_json())

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
        # At minimal tier the import graph is empty, so widening below the minimum can never
        # succeed from a single changed file -- it fails clearly instead of under-delivering.
        with self.assertRaises(ContextPackageError):
            build_context_package(
                self.repo, self.base_commit, self.candidate_commit, min_starting_files=2
            )

    def test_added_file_content_is_not_double_counted_against_its_diff(self) -> None:
        _write(
            self.repo,
            "pkg/generated.py",
            "\n".join(f"VALUE_{index} = {index}" for index in range(400)),
        )
        _run("add", ".", cwd=self.repo)
        _run("commit", "-qm", "feat: add a large generated file", cwd=self.repo)
        candidate_with_addition = _head(self.repo)

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
                "lower min_starting_files below 2",
                lambda: build_context_package(
                    repo, base, candidate, min_starting_files=2
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


class DependencyContextUnitTests(unittest.TestCase):
    """Direct unit coverage for the 30-line fallback vs. Repo Map signature preference.

    Repo Map's minimal tier never returns signatures, so the public builder cannot exercise the
    "prefer signatures, else 30-line fallback" branch end-to-end without a grammar bundle; these
    call `_dependency_context` directly instead.
    """

    def test_uses_repo_map_signatures_when_present(self) -> None:
        context = _dependency_context(
            import_graph={"seed.py": {"dep.py"}},
            seeds=["seed.py"],
            files={"dep.py": "def f():\n    pass\n"},
            signatures_by_path={"dep.py": ["def f()"]},
        )
        self.assertEqual(context, {"dep.py": ["def f()"]})

    def test_falls_back_to_the_first_thirty_lines_without_signatures(self) -> None:
        text = "\n".join(f"line {index}" for index in range(35))
        context = _dependency_context(
            import_graph={"seed.py": {"dep.txt"}},
            seeds=["seed.py"],
            files={"dep.txt": text},
            signatures_by_path={},
        )
        self.assertEqual(context, {"dep.txt": [f"line {index}" for index in range(30)]})

    def test_fallback_excerpt_is_bounded_at_thirty_lines(self) -> None:
        text = "\n".join(str(index) for index in range(40))
        self.assertEqual(_fallback_excerpt(text), [str(index) for index in range(30)])


class GuardHotPathTests(unittest.TestCase):
    """No network, `.venv`, `requirements.txt`, target-project mutation, or `uv run` in the hot path."""

    def test_repo_map_is_invoked_with_sys_executable_and_no_uv(self) -> None:
        calls: list[list[str]] = []
        real_run = subprocess.run

        def _spy(
            args: list[str],
            *,
            capture_output: bool = True,
            text: bool = True,
            encoding: str = "utf-8",
            errors: str = "replace",
            check: bool = False,
        ) -> subprocess.CompletedProcess[str]:
            if any(str(item).endswith("repo_map.py") for item in args):
                calls.append([str(item) for item in args])
            return real_run(
                args,
                capture_output=capture_output,
                text=text,
                encoding=encoding,
                errors=errors,
                check=check,
            )

        fixture = ContextBuilderFixture()
        fixture.setUp()
        try:
            with patch(
                "harness.context_builder.context_builder.subprocess.run", side_effect=_spy
            ):
                build_context_package(
                    fixture.repo,
                    fixture.base_commit,
                    fixture.candidate_commit,
                    min_starting_files=1,
                )
        finally:
            fixture.tearDown()

        self.assertEqual(len(calls), 1)
        invoked = calls[0]
        self.assertEqual(invoked[0], sys.executable)
        # -B: no .pyc bytecode caches written into the target repository.
        self.assertIn("-B", invoked)
        self.assertNotIn("uv", invoked)
        for token in invoked:
            self.assertNotIn("uv run", token)

    def test_a_real_invocation_writes_no_bytecode_cache_under_the_harness_package(
        self,
    ) -> None:
        """A real (non-mocked) subprocess run must not create a new __pycache__ under harness/ --
        that mutation is exactly what broke a review dispatch's checkout-clean invariant
        (git status --porcelain must be empty) before -B was added to the Repo Map invocation."""
        harness_root = MODULE_ROOT.parent  # .../harness
        before = {path for path in harness_root.rglob("__pycache__")}
        fixture = ContextBuilderFixture()
        fixture.setUp()
        try:
            build_context_package(
                fixture.repo, fixture.base_commit, fixture.candidate_commit, min_starting_files=1
            )
        finally:
            fixture.tearDown()
        after = {path for path in harness_root.rglob("__pycache__")}
        self.assertEqual(after - before, set())

    def test_hot_path_source_has_no_network_venv_requirements_or_uv_run(self) -> None:
        module_source = (MODULE_ROOT / "context_builder.py").read_text(encoding="utf-8")
        for banned in (
            "uv run",
            ".venv",
            "requirements.txt",
            "urlopen",
            "requests.get",
            "socket.",
        ):
            self.assertNotIn(banned, module_source)


# --- Repo Map full (bundle) tier: graph widening, symbol-graph depth, and signature-based
# dependency context need real edges, which only exist with a verified tree-sitter grammar bundle
# (ADR 0023/0024). Follows the skip/fail pattern of tests/test_repo_map_tree_sitter.py.


@pytest.fixture
def bundle_dir() -> Path:
    configured = os.environ.get(BUNDLE_ENV)
    if not configured:
        pytest.skip(f"{BUNDLE_ENV} is not set; the bundle CI job runs these tests")
    path = Path(configured)
    if not (path / "parser_bundle.lock.json").is_file():
        pytest.fail(f"{BUNDLE_ENV}={configured} has no parser_bundle.lock.json")
    return path


def _bundle_repo(tmp_path: Path, bundle: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "project"
    repo.mkdir()
    _run("init", "-q", cwd=repo)
    _run("config", "user.email", "test@example.invalid", cwd=repo)
    _run("config", "user.name", "Test", cwd=repo)

    (repo / ".harness").mkdir()
    (repo / ".harness" / "orchestration.json").write_text(
        json.dumps(
            {
                "repo_map_policy": {
                    "tier": "full",
                    "parser_bundle_registry_paths": [str(bundle)],
                    "parser_bundle_timeout_seconds": 120,
                }
            }
        ),
        encoding="utf-8",
    )

    _write(repo, "pkg/__init__.py", "")
    _write(repo, "pkg/base_util.py", "def util() -> int:\n    return 1\n")
    _write(
        repo,
        "pkg/helper.py",
        "from pkg import base_util\n\n\ndef render(value: int) -> str:\n    return str(base_util.util())\n",
    )
    _write(
        repo,
        "pkg/consumer.py",
        "from pkg import helper\n\n\ndef use() -> str:\n    return helper.render(1)\n",
    )
    _write(
        repo,
        "pkg/caller.py",
        "from pkg import consumer\n\n\ndef trigger() -> str:\n    return consumer.use()\n",
    )
    _write(
        repo,
        "tests/test_consumer.py",
        "from pkg import consumer\n\n\ndef test_use() -> None:\n    assert consumer.use() == '1'\n",
    )
    _write(
        repo,
        "docs/adr/0001-consumer.md",
        "# Consumer contract\n\nDescribes the consumer module used across pkg.\n",
    )
    _run("add", ".", cwd=repo)
    _run("commit", "-qm", "feat: base package", cwd=repo)
    base_commit = _head(repo)

    _write(
        repo,
        "pkg/consumer.py",
        "from pkg import helper\n\n\ndef use() -> str:\n    return helper.render(2)\n",
    )
    _run("add", ".", cwd=repo)
    _run("commit", "-qm", "fix: change consumer value", cwd=repo)
    candidate_commit = _head(repo)
    return repo, base_commit, candidate_commit


def test_full_tier_symbol_graph_depth_bounds_indirect_dependents(
    tmp_path: Path, bundle_dir: Path
) -> None:
    repo, base, candidate = _bundle_repo(tmp_path, bundle_dir)

    shallow = build_context_package(
        repo, base, candidate, min_starting_files=1, symbol_graph_depth=1
    )
    deep = build_context_package(
        repo, base, candidate, min_starting_files=1, symbol_graph_depth=2
    )

    assert "pkg/base_util.py" not in shallow.symbol_graph
    assert "pkg/base_util.py" in deep.symbol_graph


def test_full_tier_expands_below_minimum_starting_files_via_the_import_graph(
    tmp_path: Path, bundle_dir: Path
) -> None:
    repo, base, candidate = _bundle_repo(tmp_path, bundle_dir)

    package = build_context_package(
        repo, base, candidate, min_starting_files=2, max_starting_files=10
    )

    paths = {item.path for item in package.starting_files}
    assert len(paths) >= 2
    assert "pkg/consumer.py" in paths


def test_full_tier_uses_repo_map_signatures_for_a_direct_dependency(
    tmp_path: Path, bundle_dir: Path
) -> None:
    repo, base, candidate = _bundle_repo(tmp_path, bundle_dir)

    package = build_context_package(repo, base, candidate, min_starting_files=1)

    assert package.symbol_graph["pkg/helper.py"]["context"] == [
        "def render(value: int) -> str"
    ]
    assert package.related_tests == ["tests/test_consumer.py"]
    assert package.parser == "bundle"
    assert package.parser_provenance is not None
    assert package.parser_provenance["tier"] == "full"


def test_full_tier_fails_clearly_instead_of_silently_including_too_many_related_tests(
    tmp_path: Path, bundle_dir: Path
) -> None:
    repo, base, candidate = _bundle_repo(tmp_path, bundle_dir)

    with pytest.raises(ContextPackageError, match="max_related_tests"):
        build_context_package(
            repo, base, candidate, min_starting_files=1, max_related_tests=0
        )


if __name__ == "__main__":
    unittest.main()
