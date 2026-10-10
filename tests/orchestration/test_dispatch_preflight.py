#!/usr/bin/env python3
"""Characterisation tests pinning dispatch preflight behaviour (issue #226)."""

from __future__ import annotations

import ast
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from harness.errors import HarnessError
from harness.orchestration import dispatch_preflight
from harness.orchestration.dispatch_preflight import PreflightError, prepare
from harness.orchestration.ledger import JsonObject


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout.strip()


class PreflightErrorInvariantTests(unittest.TestCase):
    def test_preflight_error_is_a_harness_error(self) -> None:
        self.assertTrue(issubclass(PreflightError, HarnessError))

    def test_every_raise_site_passes_a_non_empty_remedy(self) -> None:
        tree = ast.parse(Path(dispatch_preflight.__file__).read_text(encoding="utf-8"))
        sites = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Raise)
            and isinstance(node.exc, ast.Call)
            and isinstance(node.exc.func, ast.Name)
            and node.exc.func.id == "PreflightError"
        ]
        self.assertTrue(sites)
        for site in sites:
            assert isinstance(site.exc, ast.Call)
            remedies = [
                keyword.value
                for keyword in site.exc.keywords
                if keyword.arg == "remedy"
            ]
            self.assertEqual(len(remedies), 1, f"line {site.lineno} has no remedy")


class _PreflightFixture(unittest.TestCase):
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
        _git(
            self.repo,
            "worktree",
            "add",
            "-q",
            "-b",
            "feature/issue-1-x",
            str(self.worktree),
        )
        self.state: dict[str, object] = {
            "repo": str(self.repo),
            "config": {"assignment_plans": {"developer": {"runtimes": {"claude": {}}}}},
            "branch": "feature/issue-1-x",
            "worktree": str(self.worktree),
            "zone": "core",
            "base_sha": self.sha,
        }

    def _prepare(
        self, role: str = "developer", **overrides: object
    ) -> dispatch_preflight.PreparedDispatch:
        state = {**self.state, **overrides}
        return prepare(" T-1 ", role, state)  # type: ignore[arg-type]

    def _assert_rejected(
        self, remedy_part: str, role: str = "developer", **overrides: object
    ) -> PreflightError:
        with self.assertRaises(PreflightError) as ctx:
            self._prepare(role, **overrides)
        self.assertIn(remedy_part, ctx.exception.remedy)
        return ctx.exception


class PrepareTests(_PreflightFixture):
    def test_prepares_a_dispatch_from_valid_state(self) -> None:
        prepared = self._prepare()
        self.assertEqual(
            (prepared.ticket, prepared.role, prepared.runtime),
            ("T-1", "developer", "claude"),
        )
        self.assertEqual(prepared.integration_ref, "base")
        self.assertEqual(
            (prepared.base_sha, prepared.worktree_sha), (self.sha, self.sha)
        )
        self.assertIsNone(prepared.candidate_sha)
        self.assertEqual(prepared.issue_branch, "feature/issue-1-x")
        self.assertEqual(prepared.worktree, str(self.worktree))
        self.assertEqual(prepared.mandatory_checks, [])
        self.assertEqual(prepared.preview_brief["zone"], "core")
        self.assertEqual(prepared.preview_brief["snapshot_commit"], self.sha)
        self.assertEqual(
            prepared.decision_packet["action"], "create and send developer dispatch"
        )
        self.assertEqual(
            prepared.decision_packet["options"],
            ["accept", "retry", "block"],
        )

    def test_to_dict_round_trips_every_field(self) -> None:
        prepared = self._prepare(
            mandatory_checks=["pytest"], integration_ref="integration/x"
        )
        data = prepared.to_dict()
        self.assertEqual(data["integration_ref"], "integration/x")
        self.assertEqual(data["mandatory_checks"], ["pytest"])
        self.assertEqual(
            cast(JsonObject, data["preview_brief"])["verification_commands"], ["pytest"]
        )
        self.assertEqual(
            cast(JsonObject, data["decision_packet"])["checks"], ["pytest"]
        )
        self.assertEqual(data["context_package"], prepared.context_package)

    def test_context_package_keeps_only_role_keys_with_values(self) -> None:
        prepared = self._prepare(
            affected_symbols=["f"],
            related_tests=[],
            architecture_decision="",
            starting_files=["a.txt"],
            contracts=["ignored-for-developer"],
        )
        self.assertEqual(
            prepared.context_package,
            {
                "snapshot_sha": self.sha,
                "role": "developer",
                "affected_symbols": ["f"],
                "starting_files": ["a.txt"],
            },
        )

    def test_unknown_role_context_is_starting_files_only(self) -> None:
        config: JsonObject = {"assignment_plans": {"qa": {"runtimes": {"claude": {}}}}}
        prepared = self._prepare(
            "qa", config=config, starting_files=["a.txt"], pinned_diff="d"
        )
        self.assertEqual(
            prepared.context_package,
            {"snapshot_sha": self.sha, "role": "qa", "starting_files": ["a.txt"]},
        )

    def test_candidate_sha_pins_the_expected_snapshot(self) -> None:
        prepared = self._prepare(candidate_sha=self.sha, snapshot_sha="unused")
        self.assertEqual(prepared.candidate_sha, self.sha)
        self.assertEqual(prepared.preview_brief["candidate_commit"], self.sha)
        self.assertEqual(prepared.context_package["snapshot_sha"], self.sha)

    def test_review_role_does_not_require_the_issue_branch(self) -> None:
        config: JsonObject = {
            "assignment_plans": {"code-review": {"runtimes": {"claude": {}}}}
        }
        prepared = self._prepare(
            "code-review", config=config, branch="feature/other", candidate_sha=self.sha
        )
        self.assertEqual(prepared.issue_branch, "feature/other")

    def test_rejects_blank_required_text(self) -> None:
        self._assert_rejected("project_state['zone']", zone=" ")
        self._assert_rejected("project_state['branch']", branch=None)

    def test_rejects_blank_ticket(self) -> None:
        with self.assertRaises(PreflightError) as ctx:
            prepare("  ", "developer", self.state)  # type: ignore[arg-type]
        self.assertEqual(
            ctx.exception.message, "project_state requires a non-empty ticket"
        )

    def test_rejects_missing_repo_directory(self) -> None:
        error = self._assert_rejected(
            "existing directory", repo=str(self.repo / "missing")
        )
        self.assertEqual(error.message, "project_state repo does not exist")

    def test_rejects_non_object_config(self) -> None:
        error = self._assert_rejected("JSON object", config=["x"])
        self.assertEqual(error.message, "project_state config must be an object")

    def test_rejects_role_without_assignment_plan(self) -> None:
        error = self._assert_rejected("assignment_plans['architect']", "architect")
        self.assertIn("no assignment plan for role 'architect'", error.message)
        self._assert_rejected(
            "assignment_plans['developer']",
            config={"assignment_plans": {"developer": []}},
        )

    def test_wraps_runtime_resolution_failures(self) -> None:
        error = self._assert_rejected("runtime/provider selection", runtime="codex")
        self.assertIn("codex", error.message)

    def test_rejects_unregistered_worktree(self) -> None:
        other = Path(self._tmp.name).resolve() / "other"
        other.mkdir()
        error = self._assert_rejected("git worktree add", worktree=str(other))
        self.assertIn("not registered", error.message)

    def test_rejects_worktree_pinned_to_another_snapshot(self) -> None:
        error = self._assert_rejected("new dispatch", snapshot_sha="deadbeef")
        self.assertIn("startup snapshot (deadbeef)", error.message)

    def test_rejects_developer_worktree_on_a_different_branch(self) -> None:
        error = self._assert_rejected(
            "checkout branch 'feature/other'", branch="feature/other"
        )
        self.assertEqual(
            error.message,
            "runtime worktree branch does not match the immutable issue branch",
        )

    def test_rejects_invalid_mandatory_checks(self) -> None:
        for bad in ("pytest", ["pytest", " "], ["pytest", 1]):
            error = self._assert_rejected("mandatory_checks", mandatory_checks=bad)
            self.assertEqual(
                error.message,
                "project_state mandatory_checks must be a list of commands",
            )

    def test_git_failures_are_reported_with_the_git_command(self) -> None:
        with self.assertRaises(PreflightError) as ctx:
            dispatch_preflight._git(
                Path(self._tmp.name), "rev-parse", "--verify", "HEAD^{commit}"
            )
        self.assertTrue(
            ctx.exception.message.startswith(
                "git rev-parse --verify HEAD^{commit} failed:"
            )
        )
        self.assertIn("git rev-parse", ctx.exception.remedy)


class RetryStartTests(_PreflightFixture):
    """Issue #524: a developer retry starts from a compact handoff inside the smart zone."""

    def setUp(self) -> None:
        super().setUp()
        (self.worktree / "big.py").write_text("x" * 8000, encoding="utf-8")
        for name in ("changed.py", "named.py", "d.py", "guide.md"):
            (self.worktree / name).write_text("y", encoding="utf-8")
        section: JsonObject = {
            "heading": "A",
            "level": 2,
            "start_line": 1,
            "end_line": 9,
        }
        self.package: JsonObject = {
            "context_package_id": "pkg-1",
            "estimated_tokens": 5000,
            "starting_files": [
                {"path": "big.py", "reason": "seed", "sections": []},
                {"path": "changed.py", "reason": "seed", "sections": []},
                {"path": "named.py", "reason": "seed", "sections": []},
                {"path": "d.py", "reason": "seed", "sections": []},
                {"path": "guide.md", "reason": "index", "sections": [section]},
            ],
        }
        self.handoff: JsonObject = {
            "context_package_id": "pkg-1",
            "commit_plan": [
                {
                    "id": "c1",
                    "summary": "s",
                    "expected_paths": ["changed.py"],
                    "covers": [1],
                }
            ],
            "developer_report": {
                "dispatch_id": "d-1",
                "outcome": "completed",
                "commit_sha": "abc",
                "changed_files": ["changed.py"],
                "commit_map": [{"commit_sha": "abc", "plan_entry_id": "c1"}],
                "output": "o" * 4000,
            },
            "retry_decision": {
                "dispatch_id": "d-2",
                "role": "code-review",
                "route": "developer-retry",
                "reason_category": "code",
                "rationale": "r" * 400,
                "findings": [
                    {
                        "axis": "spec",
                        "severity": "warning",
                        "summary": "named.py misses a case",
                        "evidence": "quoted log " * 200,
                    }
                ],
            },
        }

    def _retry(self, limit: int) -> JsonObject:
        return self._prepared_retry(limit).retry_start or {}

    def _prepared_retry(self, limit: int) -> dispatch_preflight.PreparedDispatch:
        config: JsonObject = {
            "assignment_plans": {"developer": {"runtimes": {"claude": {}}}},
            "adaptive_continuation_policy": {
                "context_limit": limit,
                "context_warn_ratio": 0.5,
            },
        }
        return self._prepare(
            config=config, retry_handoff=self.handoff, retry_package=self.package
        )

    def test_no_retry_start_without_a_developer_retry_handoff(self) -> None:
        self.assertIsNone(self._prepare().retry_start)
        config: JsonObject = {"assignment_plans": {"qa": {"runtimes": {"claude": {}}}}}
        qa = self._prepare("qa", config=config, retry_handoff=self.handoff)
        self.assertIsNone(qa.retry_start)

    def test_below_the_threshold_the_handoff_and_starting_files_pass_unchanged(
        self,
    ) -> None:
        start = self._retry(200000)
        estimate = cast(JsonObject, start["context_estimate"])
        self.assertEqual(estimate["threshold"], 100000)
        self.assertFalse(estimate["compacted"])
        self.assertEqual(estimate["before"], estimate["after"])
        self.assertEqual(start["handoff"], self.handoff)
        self.assertEqual(start["starting_files"], self.package["starting_files"])
        self.assertIsNone(start["warning"])

    def test_above_the_threshold_the_compact_reduces_the_start_into_the_smart_zone(
        self,
    ) -> None:
        start = self._retry(10000)
        estimate = cast(dict[str, int], start["context_estimate"])
        self.assertTrue(estimate["compacted"])
        self.assertGreater(estimate["before"], estimate["threshold"])
        self.assertLessEqual(estimate["after"], estimate["threshold"])
        self.assertIsNone(start["warning"])
        self.assertEqual(
            [item["path"] for item in cast(list[JsonObject], start["starting_files"])],
            ["changed.py", "named.py", "guide.md"],
        )
        handoff = cast(JsonObject, start["handoff"])
        self.assertEqual(handoff["context_package_id"], "pkg-1")
        self.assertEqual(handoff["commit_plan"], self.handoff["commit_plan"])
        self.assertNotIn("output", cast(JsonObject, handoff["developer_report"]))
        decision = cast(JsonObject, handoff["retry_decision"])
        self.assertNotIn("rationale", decision)
        self.assertEqual(
            decision["findings"],
            [
                {
                    "axis": "spec",
                    "severity": "warning",
                    "summary": "named.py misses a case",
                }
            ],
        )

    def test_a_compact_short_of_the_threshold_warns_without_blocking(self) -> None:
        prepared = self._prepared_retry(1000)
        start = prepared.retry_start or {}
        estimate = cast(dict[str, int], start["context_estimate"])
        self.assertTrue(estimate["compacted"])
        self.assertLess(estimate["after"], estimate["before"])
        self.assertGreater(estimate["after"], estimate["threshold"])
        self.assertIn("above the smart-zone threshold", cast(str, start["warning"]))
        self.assertEqual(
            (
                prepared.decision_packet["retry_context_estimate"],
                prepared.decision_packet["retry_context_warning"],
            ),
            (estimate, start["warning"]),
        )


class ToolingRestartWorktreeTests(_PreflightFixture):
    """Issue #502: a tooling restart inherits exactly the changes a blocked commit left behind."""

    def setUp(self) -> None:
        super().setUp()
        self.uncommitted = ["src/a.py"]
        self.config: JsonObject = {
            "assignment_plans": {"developer": {"runtimes": {"claude": {}}}},
            "backend_zones": {"core": {"paths": ["src/*"]}},
        }

    def _handoff(self, route: str = "tooling-retry") -> JsonObject:
        blocker: JsonObject = {
            "tool": "PreToolUse:Bash hook",
            "command": "git commit -m 'feat: a'",
            "message": "Blocked",
        }
        if self.uncommitted:
            blocker["uncommitted_files"] = list(self.uncommitted)
        return {
            "context_package_id": "pkg-1",
            "commit_plan": None,
            "developer_report": {
                "dispatch_id": "d-1",
                "outcome": "blocked",
                "commit_sha": self.sha,
                "changed_files": ["src/done.py"],
                "commit_map": [],
                "tooling_blocker": blocker,
            },
            "retry_decision": {
                "dispatch_id": "d-1",
                "role": "developer",
                "route": route,
                "reason_category": "tooling" if route == "tooling-retry" else "code",
                "findings": [],
            },
        }

    def _write(self, *paths: str) -> None:
        for path in paths:
            target = self.worktree / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("wip", encoding="utf-8")

    def _restart(
        self, route: str = "tooling-retry"
    ) -> dispatch_preflight.PreparedDispatch:
        return self._prepare(config=self.config, retry_handoff=self._handoff(route))

    def _rejected(self) -> PreflightError:
        with self.assertRaises(PreflightError) as ctx:
            self._restart()
        return ctx.exception

    def test_the_listed_uncommitted_changes_inside_the_zone_pass(self) -> None:
        self._write("src/a.py")
        start = self._restart().retry_start or {}
        handoff = cast(JsonObject, start["handoff"])
        report = cast(JsonObject, handoff["developer_report"])
        blocker = cast(JsonObject, report["tooling_blocker"])
        self.assertEqual(blocker["uncommitted_files"], ["src/a.py"])

    def test_a_modified_tracked_file_counts_as_an_uncommitted_change(self) -> None:
        (self.worktree / "a.txt").write_text("changed", encoding="utf-8")
        self.uncommitted = ["a.txt"]
        error = self._rejected()
        self.assertIn("outside the batch zone 'core': a.txt", error.message)
        self.assertIn("a.txt", error.remedy)

    def test_a_clean_worktree_without_listed_changes_passes_as_before(self) -> None:
        self.uncommitted = []
        self.assertIsNotNone(self._restart().retry_start)

    def test_an_extra_file_is_rejected_by_name(self) -> None:
        self._write("src/a.py", "src/b.py")
        error = self._rejected()
        self.assertIn("not listed in the blocked report: src/b.py", error.message)
        self.assertIn("src/b.py", error.remedy)
        self.assertNotIn("src/a.py", error.message)

    def test_a_missing_file_is_rejected_by_name(self) -> None:
        self.uncommitted = ["src/a.py", "src/c.py"]
        self._write("src/a.py")
        error = self._rejected()
        self.assertIn("missing from the worktree: src/c.py", error.message)
        self.assertIn("src/c.py", error.remedy)

    def test_a_clean_worktree_misses_every_listed_file(self) -> None:
        error = self._rejected()
        self.assertIn("missing from the worktree: src/a.py", error.message)

    def test_a_file_outside_the_zone_is_rejected_by_name(self) -> None:
        self._write("src/a.py", "docs/x.md")
        error = self._rejected()
        self.assertIn("outside the batch zone 'core': docs/x.md", error.message)
        self.assertIn("docs/x.md", error.remedy)

    def test_a_restart_without_zone_paths_is_rejected(self) -> None:
        self._write("src/a.py")
        self.config = {"assignment_plans": self.config["assignment_plans"]}
        error = self._rejected()
        self.assertIn("backend_zones", error.remedy)

    def test_a_dirty_worktree_of_another_developer_retry_is_not_checked(self) -> None:
        self._write("src/a.py", "docs/x.md")
        self.assertIsNotNone(self._restart("developer-retry").retry_start)

    def test_the_compacted_handoff_keeps_only_the_uncommitted_files(self) -> None:
        self._write("src/a.py")
        self.config["adaptive_continuation_policy"] = {
            "context_limit": 10,
            "context_warn_ratio": 0.5,
        }
        package: JsonObject = {
            "context_package_id": "pkg-1",
            "estimated_tokens": 50,
            "starting_files": [
                {"path": "src/a.py", "reason": "seed", "sections": []},
                {"path": "src/other.py", "reason": "seed", "sections": []},
            ],
        }
        prepared = self._prepare(
            config=self.config, retry_handoff=self._handoff(), retry_package=package
        )
        start = prepared.retry_start or {}
        self.assertTrue(cast(JsonObject, start["context_estimate"])["compacted"])
        report = cast(
            JsonObject, cast(JsonObject, start["handoff"])["developer_report"]
        )
        self.assertEqual(report["tooling_blocker"], {"uncommitted_files": ["src/a.py"]})
        self.assertEqual(
            [item["path"] for item in cast(list[JsonObject], start["starting_files"])],
            ["src/a.py"],
        )


class RuntimeAccessPreflightTests(_PreflightFixture):
    def test_authored_access_is_visible_and_unverified_without_worker_proof(
        self,
    ) -> None:
        config = self.state["config"]
        assert isinstance(config, dict)
        config["access_policy"] = {
            "defaults": {"mode": "sandbox", "network": {"hosts": ["github.com"]}}
        }
        result = self._prepare()
        access = cast(JsonObject, result.decision_packet["runtime_access"])
        plan = cast(JsonObject, access["plan"])
        verification = cast(JsonObject, access["verification"])
        self.assertEqual(plan["mode"], "sandbox")
        self.assertEqual(verification["status"], "unverified")
        self.assertIn("new runtime session", str(verification["remedy"]))

    def test_publish_preview_resolves_the_publish_operation_like_the_brief(
        self,
    ) -> None:
        config = self.state["config"]
        assert isinstance(config, dict)
        config["access_policy"] = {
            "defaults": {"mode": "sandbox", "network": {"hosts": ["github.com"]}},
            "operations": {"publish": {"network": {"hosts": ["pypi.org"]}}},
        }

        def plan(purpose: str) -> JsonObject:
            access = self._prepare(purpose=purpose).decision_packet["runtime_access"]
            return cast(JsonObject, cast(JsonObject, access)["plan"])

        work = plan("work")
        publish = plan("publish")

        self.assertEqual(cast(JsonObject, work["network"])["hosts"], ["github.com"])
        self.assertEqual(cast(JsonObject, publish["network"])["hosts"], ["pypi.org"])
        self.assertEqual(
            cast(JsonObject, publish["sources"])["network"], "operations.publish"
        )


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired(["git"], 60),
        FileNotFoundError(2, "No such file or directory", "git"),
    ],
)
def test_a_git_command_that_hangs_or_cannot_start_is_a_preflight_error(
    tmp_path: Path, failure: Exception
) -> None:
    timeouts: list[object] = []

    def stalled(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        timeouts.append(kwargs.get("timeout"))
        raise failure

    with (
        patch.object(subprocess, "run", side_effect=stalled),
        pytest.raises(PreflightError) as caught,
    ):
        dispatch_preflight._git(tmp_path, "worktree", "list", "--porcelain")

    assert caught.value.__cause__ is failure
    assert caught.value.message.startswith("git worktree list --porcelain ")
    assert "git worktree list --porcelain" in caught.value.remedy
    assert len(timeouts) == 1
    assert isinstance(timeouts[0], int) and timeouts[0] > 0


if __name__ == "__main__":
    unittest.main()
