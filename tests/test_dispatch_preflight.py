#!/usr/bin/env python3
"""Characterisation tests pinning dispatch preflight behaviour (issue #226)."""

from __future__ import annotations

import ast
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast

from harness.errors import HarnessError
from harness.orchestration import dispatch_preflight
from harness.orchestration.dispatch_preflight import PreflightError, prepare
from harness.orchestration.ledger import JsonObject


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, encoding="utf-8", check=True
    )
    return result.stdout.strip()


class PreflightErrorInvariantTests(unittest.TestCase):
    def test_preflight_error_is_a_harness_error(self) -> None:
        self.assertTrue(issubclass(PreflightError, HarnessError))

    def test_every_raise_site_passes_a_non_empty_remedy(self) -> None:
        tree = ast.parse(Path(dispatch_preflight.__file__).read_text(encoding="utf-8"))
        sites = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
            and isinstance(node.exc.func, ast.Name) and node.exc.func.id == "PreflightError"
        ]
        self.assertEqual(len(sites), 11)
        for site in sites:
            assert isinstance(site.exc, ast.Call)
            remedies = [keyword.value for keyword in site.exc.keywords if keyword.arg == "remedy"]
            self.assertEqual(len(remedies), 1, f"line {site.lineno} has no remedy")


class PrepareTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.repo = root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@example.com")
        _git(self.repo, "config", "user.name", "t")
        (self.repo / "a.txt").write_text("a", encoding="utf-8")
        _git(self.repo, "add", "a.txt")
        _git(self.repo, "commit", "-q", "-m", "init")
        self.sha = _git(self.repo, "rev-parse", "HEAD")
        self.worktree = root / "wt"
        _git(self.repo, "worktree", "add", "-q", "-b", "feature/issue-1-x", str(self.worktree))
        self.state: dict[str, object] = {
            "repo": str(self.repo),
            "config": {"assignment_plans": {"developer": {"runtimes": {"claude": {}}}}},
            "branch": "feature/issue-1-x",
            "worktree": str(self.worktree),
            "zone": "core",
            "base_sha": self.sha,
        }

    def _prepare(self, role: str = "developer", **overrides: object) -> dispatch_preflight.PreparedDispatch:
        state = {**self.state, **overrides}
        return prepare(" T-1 ", role, state)  # type: ignore[arg-type]

    def _assert_rejected(self, remedy_part: str, role: str = "developer", **overrides: object) -> PreflightError:
        with self.assertRaises(PreflightError) as ctx:
            self._prepare(role, **overrides)
        self.assertIn(remedy_part, ctx.exception.remedy)
        return ctx.exception

    def test_prepares_a_dispatch_from_valid_state(self) -> None:
        prepared = self._prepare()
        self.assertEqual((prepared.ticket, prepared.role, prepared.runtime), ("T-1", "developer", "claude"))
        self.assertEqual(prepared.integration_ref, "base")
        self.assertEqual((prepared.base_sha, prepared.worktree_sha), (self.sha, self.sha))
        self.assertIsNone(prepared.candidate_sha)
        self.assertEqual(prepared.issue_branch, "feature/issue-1-x")
        self.assertEqual(prepared.worktree, str(self.worktree))
        self.assertEqual(prepared.mandatory_checks, [])
        self.assertEqual(prepared.preview_brief["zone"], "core")
        self.assertEqual(prepared.preview_brief["snapshot_commit"], self.sha)
        self.assertEqual(prepared.decision_packet["action"], "create and send developer dispatch")
        self.assertEqual(
            prepared.decision_packet["options"], ["accept", "retry", "block", "full review", "delta-review"]
        )

    def test_to_dict_round_trips_every_field(self) -> None:
        prepared = self._prepare(mandatory_checks=["pytest"], integration_ref="integration/x")
        data = prepared.to_dict()
        self.assertEqual(data["integration_ref"], "integration/x")
        self.assertEqual(data["mandatory_checks"], ["pytest"])
        self.assertEqual(cast(JsonObject, data["preview_brief"])["verification_commands"], ["pytest"])
        self.assertEqual(cast(JsonObject, data["decision_packet"])["checks"], ["pytest"])
        self.assertEqual(data["context_package"], prepared.context_package)

    def test_context_package_keeps_only_role_keys_with_values(self) -> None:
        prepared = self._prepare(
            affected_symbols=["f"], related_tests=[], architecture_decision="", starting_files=["a.txt"],
            contracts=["ignored-for-developer"],
        )
        self.assertEqual(
            prepared.context_package,
            {"snapshot_sha": self.sha, "role": "developer", "affected_symbols": ["f"], "starting_files": ["a.txt"]},
        )

    def test_unknown_role_context_is_starting_files_only(self) -> None:
        config: JsonObject = {"assignment_plans": {"qa": {"runtimes": {"claude": {}}}}}
        prepared = self._prepare("qa", config=config, starting_files=["a.txt"], pinned_diff="d")
        self.assertEqual(
            prepared.context_package, {"snapshot_sha": self.sha, "role": "qa", "starting_files": ["a.txt"]}
        )

    def test_candidate_sha_pins_the_expected_snapshot(self) -> None:
        prepared = self._prepare(candidate_sha=self.sha, snapshot_sha="unused")
        self.assertEqual(prepared.candidate_sha, self.sha)
        self.assertEqual(prepared.preview_brief["candidate_commit"], self.sha)
        self.assertEqual(prepared.context_package["snapshot_sha"], self.sha)

    def test_review_role_does_not_require_the_issue_branch(self) -> None:
        config: JsonObject = {"assignment_plans": {"code-review": {"runtimes": {"claude": {}}}}}
        prepared = self._prepare("code-review", config=config, branch="feature/other")
        self.assertEqual(prepared.issue_branch, "feature/other")

    def test_rejects_blank_required_text(self) -> None:
        self._assert_rejected("project_state['zone']", zone=" ")
        self._assert_rejected("project_state['branch']", branch=None)

    def test_rejects_blank_ticket(self) -> None:
        with self.assertRaises(PreflightError) as ctx:
            prepare("  ", "developer", self.state)  # type: ignore[arg-type]
        self.assertEqual(ctx.exception.message, "project_state requires a non-empty ticket")

    def test_rejects_missing_repo_directory(self) -> None:
        error = self._assert_rejected("existing directory", repo=str(self.repo / "missing"))
        self.assertEqual(error.message, "project_state repo does not exist")

    def test_rejects_non_object_config(self) -> None:
        error = self._assert_rejected("JSON object", config=["x"])
        self.assertEqual(error.message, "project_state config must be an object")

    def test_rejects_role_without_assignment_plan(self) -> None:
        error = self._assert_rejected("assignment_plans['architect']", "architect")
        self.assertIn("no assignment plan for role 'architect'", error.message)
        self._assert_rejected("assignment_plans['developer']", config={"assignment_plans": {"developer": []}})

    def test_wraps_runtime_resolution_failures(self) -> None:
        error = self._assert_rejected("runtime/provider selection", runtime="codex")
        self.assertIn("codex", error.message)

    def test_rejects_unregistered_worktree(self) -> None:
        other = Path(self._tmp.name).resolve() / "other"
        other.mkdir()
        error = self._assert_rejected("git worktree add", worktree=str(other))
        self.assertEqual(error.message, "worktree is not registered by git worktree")

    def test_rejects_worktree_pinned_to_another_snapshot(self) -> None:
        error = self._assert_rejected("checkout deadbeef", snapshot_sha="deadbeef")
        self.assertIn("expected snapshot deadbeef", error.message)

    def test_rejects_developer_worktree_on_a_different_branch(self) -> None:
        error = self._assert_rejected("checkout branch 'feature/other'", branch="feature/other")
        self.assertEqual(error.message, "write/planning worktree is not on the resolved issue branch")

    def test_rejects_invalid_mandatory_checks(self) -> None:
        for bad in ("pytest", ["pytest", " "], ["pytest", 1]):
            error = self._assert_rejected("mandatory_checks", mandatory_checks=bad)
            self.assertEqual(error.message, "project_state mandatory_checks must be a list of commands")

    def test_git_failures_are_reported_with_the_git_command(self) -> None:
        with self.assertRaises(PreflightError) as ctx:
            dispatch_preflight._git(Path(self._tmp.name), "rev-parse", "--verify", "HEAD^{commit}")
        self.assertTrue(ctx.exception.message.startswith("git rev-parse --verify HEAD^{commit} failed:"))
        self.assertIn("git rev-parse", ctx.exception.remedy)


if __name__ == "__main__":
    unittest.main()
