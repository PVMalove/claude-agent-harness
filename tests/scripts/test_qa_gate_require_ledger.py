"""The require hook against the real coordinator CLI and a real ledger: accepted QA of HEAD opens
the PR; unaccepted QA, another SHA and a tampered report keep blocking."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness.orchestration import coordinator
from tests.orchestration import test_coordinator as coordinator_tests

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "harness" / "project" / "hooks" / "qa-gate-state.py"
# Stands in for the installed `.harness/orchestration/coordinator.py`: the real CLI, from source.
SHIM = f"""\
import sys

sys.path.insert(0, {str(ROOT)!r})
from harness.orchestration.coordinator import main

raise SystemExit(main())
"""


class Pipeline:
    """A ticket branch taken through architect, developer, review and QA on a real ledger."""

    def __init__(self) -> None:
        self.fixture = coordinator_tests.CoordinatorRetryRoutingTests()
        self.fixture.setUp()
        fx = self.fixture
        # The CLI reads the repository's own ledger: put the fixture's ledger there.
        fx.state_dir = fx.repo / coordinator.STATE_REL
        (fx.repo / ".harness" / "orchestration" / "coordinator.py").write_text(SHIM)
        self.batch_id = fx._create_batch()["batch_id"]
        fx._accepted_architect(self.batch_id)
        self.candidate = fx._accepted_candidate(self.batch_id)

    def close(self) -> None:
        self.fixture.tearDown()

    def accept_qa(self) -> None:
        """Review and QA of the candidate, both accepted by the coordinator."""
        self.fixture._accepted_review_and_qa(self.batch_id, self.candidate)

    def report_qa_without_a_decision(self) -> None:
        """Review accepted, QA reported but not decided."""
        fx = self.fixture
        fx._reported_review(self.batch_id, self.candidate)
        fx._decide(self.batch_id, "accept")
        fx._reported_qa(self.batch_id, self.candidate)

    def qa_report_path(self) -> Path:
        """The QA completion report file in the ledger."""
        batch = self.fixture._batch_record(self.batch_id)
        entry = next(item for item in batch["dispatches"] if item["role"] == "qa")
        return Path(self.fixture._records() / entry["report"])

    def require(self) -> subprocess.CompletedProcess[str]:
        """Run the hook for a PR of the issue branch, as in a linked worktree."""
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
        command = f"gh pr create --head {self.fixture.branch}"
        return subprocess.run(
            [sys.executable, str(HOOK), "require"],
            cwd=self.fixture.worktree,
            env=env,
            input=json.dumps({"tool_input": {"command": command}}),
            capture_output=True,
            text=True,
            check=False,
        )


@pytest.fixture
def pipeline() -> Iterator[Pipeline]:
    found = Pipeline()
    try:
        yield found
    finally:
        found.close()


def test_accepted_qa_of_the_same_sha_opens_the_pr(pipeline: Pipeline) -> None:
    pipeline.accept_qa()
    result = pipeline.require()
    assert result.returncode == 0, result.stderr


def test_qa_reported_but_not_accepted_keeps_blocking(pipeline: Pipeline) -> None:
    pipeline.report_qa_without_a_decision()
    assert pipeline.require().returncode == 2


def test_no_qa_at_all_keeps_blocking(pipeline: Pipeline) -> None:
    assert pipeline.require().returncode == 2


def test_accepted_qa_of_another_sha_keeps_blocking(pipeline: Pipeline) -> None:
    pipeline.accept_qa()
    worktree = pipeline.fixture.worktree
    (worktree / "later.txt").write_text("later\n")
    coordinator_tests._git(worktree, "add", "-A")
    coordinator_tests._git(worktree, "commit", "-m", "later")
    assert pipeline.require().returncode == 2


def test_a_tampered_report_keeps_blocking(pipeline: Pipeline) -> None:
    pipeline.accept_qa()
    path = pipeline.qa_report_path()
    report = json.loads(path.read_text(encoding="utf-8"))
    report["output"] = "tampered"
    path.write_text(json.dumps(report), encoding="utf-8")
    assert pipeline.require().returncode == 2
