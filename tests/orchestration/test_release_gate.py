"""The Release Gate of push and merge (ADR 0026, section 4; ADR 0021, section 7; ADR 0023, section 5).

Real ledger, real Git (a local bare remote) and, for the runtime-store epoch, the real
``harness-runtime serve``: every refusal is reached through the coordinator CLI. The file-ledger
epoch keeps the Python Git checks of the pair and asks no runtime verdict (ADR 0020, section 6).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from harness.orchestration import coordinator
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.workflow import release_gate
from tests.orchestration import runtime_store_support as support
from tests.orchestration.test_ci_collect import PR
from tests.orchestration.test_integration_record import PublishedBranch, _git
from tests.orchestration.test_pr_continuation import NextFixture


def _tree(repo: Path, commit: str) -> str:
    return _git(repo, "rev-parse", f"{commit}^{{tree}}")


def _commit_tree(repo: Path, tree: str, *parents: str) -> str:
    arguments = [item for parent in parents for item in ("-p", parent)]
    return _git(repo, "commit-tree", tree, *arguments, "-m", "merged")


class FileLedgerReleaseGateTests(NextFixture):
    """The Python Git checks of the pair, in the file-ledger epoch."""

    def candidate(self) -> str:
        return str(self.published["candidate"])

    def target(self) -> str:
        return str(self.prepared["target_sha"])

    def merged(self, merge_commit: str, pull_request: int | None = PR) -> JsonObject:
        return coordinator.integration_next(
            self.branch.args(
                record=self.record_id,
                ticket=None,
                branch=None,
                batch=None,
                pull_request=pull_request,
                merge_commit=merge_commit,
            )
        )

    def land(self, commit: str) -> None:
        _git(self.branch.repo, "push", "-f", "origin", f"{commit}:refs/heads/master")

    def test_a_target_that_moved_after_qa_asks_for_a_refresh_never_a_handoff(
        self,
    ) -> None:
        self.branch.advance_integration_ref()

        result = self.next(PR)

        self.assertEqual(result["step"], "refresh")
        self.assertNotIn("handoff", result)

    def test_a_squash_merge_with_the_candidate_tree_is_merged(self) -> None:
        repo = self.branch.repo
        squash = _commit_tree(repo, _tree(repo, self.candidate()), self.target())
        self.land(squash)
        before = self.branch.snapshot()

        result = self.merged(squash)

        self.assertEqual(result["step"], "merged")
        self.assertEqual(result["merged_tree_sha"], _tree(repo, self.candidate()))
        self.assertEqual(result["candidate_tree_sha"], _tree(repo, self.candidate()))
        self.assertEqual(self.branch.snapshot(), before)

    def test_a_merge_with_another_tree_is_a_blocker(self) -> None:
        repo = self.branch.repo
        other = _commit_tree(repo, _tree(repo, self.target()), self.target())
        self.land(other)

        result = self.merged(other)

        self.assertEqual(result["step"], "merge-tree-mismatch")
        self.assertEqual(result["refusals"], ["merged_tree_mismatch"])
        self.assertIn("new full QA", result["next"][0])
        self.assertIn("blocker", result["next"][0])

    def test_a_squash_onto_a_target_that_moved_is_a_tree_mismatch(self) -> None:
        repo = self.branch.repo
        self.branch.advance_integration_ref()
        _git(repo, "merge", "--squash", self.candidate())
        _git(repo, "commit", "-m", "squash")
        squash = _git(repo, "rev-parse", "HEAD")
        _git(repo, "push", "origin", "master")

        result = self.merged(squash)

        self.assertEqual(result["step"], "merge-tree-mismatch")
        self.assertNotEqual(result["merged_tree_sha"], result["candidate_tree_sha"])

    def test_a_merge_commit_that_is_not_fetched_names_the_fetch(self) -> None:
        with self.assertRaises(CoordinatorError) as raised:
            self.merged("e" * 40)

        self.assertIn("git fetch origin master", raised.exception.remedy)

    def test_a_merge_commit_needs_its_pull_request(self) -> None:
        with self.assertRaises(CoordinatorError) as raised:
            self.merged(self.candidate(), pull_request=None)

        self.assertIn("--pull-request", raised.exception.remedy)

    def test_the_public_cli_reaches_the_tree_check(self) -> None:
        repo = self.branch.repo
        squash = _commit_tree(repo, _tree(repo, self.candidate()), self.target())
        parsed = coordinator.parser().parse_args(
            [
                "--repo",
                str(repo),
                "integration",
                "next",
                "--record",
                self.record_id,
                "--pull-request",
                str(PR),
                "--merge-commit",
                squash,
            ]
        )

        self.assertIs(parsed.handler, coordinator.integration_next)
        self.assertEqual(parsed.handler(parsed)["step"], "merged")

    def test_a_moved_issue_branch_refuses_the_handoff(self) -> None:
        repo = self.branch.repo
        foreign = _commit_tree(repo, _tree(repo, self.candidate()), self.candidate())
        _git(repo, "push", "-f", "origin", f"{foreign}:refs/heads/{self.branch.branch}")
        before = self.branch.snapshot()

        result = self.next(PR)

        self.assertEqual(result["step"], "release-gate-refused")
        self.assertEqual(result["refusals"], ["branch_head_mismatch"])
        self.assertIsNone(result["qa_evidence"])
        self.assertNotIn("handoff", result)
        self.assertEqual(self.branch.snapshot(), before)

    def test_a_target_outside_the_candidate_history_is_refused(self) -> None:
        repo = self.branch.repo
        orphan = _commit_tree(repo, _tree(repo, self.target()))
        pair = {"candidate_sha": self.candidate(), "target_sha": orphan}

        refusals = release_gate.git_refusals(repo, "origin", self.branch.branch, pair)

        self.assertEqual(refusals, ["target_not_in_candidate_history"])

    def test_a_refreshed_pair_with_only_a_hand_linked_ci_check_is_not_handed_over(
        self,
    ) -> None:
        refreshed = self.refresh()
        coordinator.integration_link_evidence(
            self.branch.args(
                record=self.record_id,
                kind="ci",
                result="passed",
                reference="manual-ci-run",
                artifact_sha256=None,
                candidate_commit=refreshed["new_candidate_sha"],
                target_commit=_git(self.branch.repo, "rev-parse", "origin/master"),
            )
        )

        result = self.next(PR)

        self.assertEqual(result["step"], "verify")
        self.assertNotIn("handoff", result)


# -- the runtime-store epoch ---------------------------------------------------------------------


def _ls_remote(repo: Path, branch: str) -> str:
    return _git(repo, "ls-remote", "origin", f"refs/heads/{branch}")


def _cut_over(repo: Path) -> support.ServingRuntime:
    migrated = support.migrate(repo)
    assert migrated.returncode == 0, migrated.stderr
    return support.ServingRuntime(repo)


def _remedy(result: subprocess.CompletedProcess[str]) -> str:
    return result.stderr.split("REMEDY:", 1)[1].strip()


@pytest.fixture
def approved_publish() -> object:
    """A branch whose candidate has accepted review and QA, and an approved publish dispatch."""
    support.runtime_binary()
    branch = PublishedBranch()
    branch.fixture.state_dir = branch.repo / ".harness/orchestration/state"
    fx = branch.fixture
    batch_id = fx._create_batch()["batch_id"]
    fx._accepted_architect(batch_id)
    candidate = fx._accepted_candidate(batch_id, "x")
    fx._accepted_review_and_qa(batch_id, candidate)
    brief = fx._dispatch(batch_id, "developer", purpose="publish", candidate=candidate)[
        "brief"
    ]
    yield branch, candidate, brief["dispatch_id"]
    branch.close()


def test_store_publish_without_a_full_qa_run_of_the_pair_pushes_nothing(
    approved_publish: tuple[PublishedBranch, str, str],
) -> None:
    branch, candidate, dispatch_id = approved_publish
    repo = branch.repo
    runtime = _cut_over(repo)
    try:
        remote_before = _ls_remote(repo, branch.branch)

        refused = support.coordinator_with(
            repo,
            runtime,
            "dispatch",
            "publish",
            "--dispatch",
            dispatch_id,
            "--remote",
            "origin",
        )

        assert refused.returncode == 2, refused.stdout
        # The staged QA report of the fixture names no QA log: a cached result, no full run.
        assert _remedy(refused).startswith("[evidence_unverifiable")
        assert _ls_remote(repo, branch.branch) == remote_before
        status = f"dispatch-status/{dispatch_id}.json"
        assert (
            support.store_records(repo, runtime, status)[status]["state"] == "approved"
        )
        target = _git(repo, "rev-parse", "origin/master")
        pair = (
            f"reports/integration-pair/{release_gate.pair_id(candidate, target)}.json"
        )
        recorded = support.store_records(repo, runtime, pair)[pair]
        assert recorded["candidate_tree_sha"] == _tree(repo, candidate)
    finally:
        runtime.stop()

    unavailable = support.coordinator_with(
        repo,
        runtime,
        "dispatch",
        "publish",
        "--dispatch",
        dispatch_id,
        "--remote",
        "origin",
    )

    assert unavailable.returncode == 2
    assert "runtime_unavailable" in unavailable.stderr
    assert _ls_remote(repo, branch.branch) == remote_before


@pytest.fixture
def prepared_store() -> object:
    """A published and prepared branch whose ledger is then cut over to the runtime store."""
    support.runtime_binary()
    branch = PublishedBranch()
    branch.fixture.state_dir = branch.repo / ".harness/orchestration/state"
    branch.publish()
    record_id = branch.prepare()["integration_record_id"]
    runtime = _cut_over(branch.repo)
    yield branch, record_id, runtime
    runtime.stop()
    branch.close()


def test_store_next_without_an_ok_verdict_never_hands_over(
    prepared_store: tuple[PublishedBranch, str, support.ServingRuntime],
) -> None:
    branch, record_id, runtime = prepared_store
    command = ("integration", "next", "--record", record_id, "--pull-request", str(PR))

    refused = support.coordinator_with(branch.repo, runtime, *command)

    assert refused.returncode == 0, refused.stderr
    result = json.loads(refused.stdout)
    assert result["step"] == "release-gate-refused"
    assert result["refusals"] == ["evidence_refused"]
    assert result["qa_evidence"]["verdict"] == "refused"
    assert "evidence_unverifiable" in release_gate.violation_codes(
        result["qa_evidence"]
    )

    runtime.stop()
    unavailable = support.coordinator_with(branch.repo, runtime, *command)

    assert unavailable.returncode == 2
    assert "handoff" not in unavailable.stdout
