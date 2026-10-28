"""Parallel commands on seam 1: two real coordinator processes and a real runtime (ADR 0020,
section 3).

A command reads from one read snapshot of the store, so a commit of another batch between two of
its reads neither refuses it nor shows it half of two states, and a reader never waits for a
writer. A decision over every batch (the concurrency budget, a duplicate batch) is guarded by the
generation revision: of two commands that decided on the same state only one commits.

A debug build of ``harness-runtime`` stops at ``state.read:in-snapshot`` (after the first
``SELECT`` of a snapshot), ``state.write:before-begin`` and ``state.write:in-transaction`` when
``HARNESS_RUNTIME_TEST_PAUSE_AT`` names one. Each hook fires once for a runtime process, so the
setup of a test runs on a runtime without hooks and the test restarts it with the hook armed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness.json_types import JsonObject

from .runtime_store_support import (
    APPROVAL,
    ARCHITECT_DISPATCH,
    STATE_REL,
    ServingRuntime,
    add_coordinator_batch,
    architect_report_file,
    coordinator_popen,
    coordinator_repository,
    coordinator_with,
    migrate,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the seam tests drive a Unix-socket runtime"
)

IN_SNAPSHOT = "state.read:in-snapshot"
BEFORE_BEGIN = "state.write:before-begin"
IN_TRANSACTION = "state.write:in-transaction"
PAUSE = "HARNESS_RUNTIME_TEST_PAUSE_AT"


def _remedy(stderr: str) -> str:
    return next(line for line in stderr.splitlines() if line.startswith("REMEDY:"))


class Batches:
    """A cut-over repository with ``count`` batches and the runtime that serves it.

    Each batch has an architect dispatch that is working when ``working`` is true; otherwise the
    batches stay approved (``awaiting-approval``), without a dispatch."""

    def __init__(
        self, tmp: Path, *, count: int, budget: int, working: bool, auto: bool = False
    ) -> None:
        self.repo = coordinator_repository(tmp)
        config: JsonObject = {
            "provider_profiles": {},
            "assignment_plans": {},
            "concurrency_budget": budget,
            "verification_commands": [],
        }
        if auto:
            config.update(
                approval_policy="auto",
                human_approval_gate="trusted",
                worker_attestation_required=True,
            )
        (self.repo / ".harness" / "orchestration.json").write_text(
            json.dumps(config), encoding="utf-8"
        )
        self.batches: list[str] = []
        self.dispatches: list[str] = []
        self.briefs: list[JsonObject] = []
        for number in range(count):
            ids, brief = add_coordinator_batch(
                self.repo,
                tmp / f"worktree-{number}",
                ticket=f"#{900 + number}",
                dispatch=working,
            )
            self.batches.append(ids["batch_id"])
            if working:
                self.dispatches.append(ids["dispatch_id"])
                self.briefs.append(brief)
        result = migrate(self.repo)
        assert result.returncode == 0, result.stderr
        self.runtime = ServingRuntime(self.repo)
        for dispatch in self.dispatches:
            sent = self.run("dispatch", "send", "--dispatch", dispatch)
            assert sent.returncode == 0, sent.stderr
            reported = self.run(
                "dispatch",
                "self-report",
                "--dispatch",
                dispatch,
                "--model",
                "sonnet",
                "--worktree",
                json.loads(sent.stdout)["worker_worktree"],
            )
            assert reported.returncode == 0, reported.stderr

    def run(self, *command: str) -> subprocess.CompletedProcess[str]:
        return coordinator_with(self.repo, self.runtime, *command)

    def start(self, *command: str) -> subprocess.Popen[str]:
        return coordinator_popen(self.repo, self.runtime, *command)

    def arm(self, step: str) -> None:
        """Restart the runtime with the pause hook at ``step``."""
        xdg = self.runtime.xdg
        self.runtime.kill()
        self.runtime = ServingRuntime(self.repo, env={PAUSE: step}, xdg=xdg)

    def heartbeat(self, number: int) -> subprocess.CompletedProcess[str]:
        return self.run("dispatch", "heartbeat", "--dispatch", self.dispatches[number])

    def dispatch_create(self, number: int) -> list[str]:
        """The arguments of ``dispatch create`` of an architect in batch ``number``."""
        fields = [
            "--batch",
            self.batches[number],
            "--role",
            str(ARCHITECT_DISPATCH["role"]),
            "--runtime",
            str(ARCHITECT_DISPATCH["runtime"]),
            "--model",
            str(ARCHITECT_DISPATCH["model"]),
            "--effort",
            str(ARCHITECT_DISPATCH["effort"]),
        ]
        proposed = self.run("dispatch", "propose", *fields)
        assert proposed.returncode == 0, proposed.stderr
        digest = json.loads(proposed.stdout)["transition_digest"]
        return [
            "dispatch",
            "create",
            *fields,
            "--transition-digest",
            digest,
            "--approved-by",
            APPROVAL["approved_by"],
            "--approved-at",
            APPROVAL["approved_at"],
        ]

    def stop(self) -> None:
        self.runtime.stop()


@pytest.fixture
def two_working(tmp_path: Path) -> Iterator[Batches]:
    batches = Batches(tmp_path, count=2, budget=99, working=True)
    try:
        yield batches
    finally:
        batches.stop()


def test_two_batches_commit_while_one_command_reads_from_a_paused_snapshot(
    two_working: Batches,
) -> None:
    two_working.arm(IN_SNAPSHOT)
    first = two_working.start(
        "dispatch", "heartbeat", "--dispatch", two_working.dispatches[0]
    )
    two_working.runtime.wait_paused(IN_SNAPSHOT)

    second = two_working.heartbeat(1)
    two_working.runtime.resume()
    stdout, stderr = first.communicate(timeout=60)

    assert second.returncode == 0, second.stderr
    assert first.returncode == 0, stderr
    for output in (second.stderr, stderr):
        assert "ledger_busy" not in output and "store_busy" not in output
    assert json.loads(stdout)["dispatch_id"] == two_working.dispatches[0]


@pytest.fixture
def auto_working(tmp_path: Path) -> Iterator[Batches]:
    # ``batch auto-report`` reports on a batch planned under ``approval_policy: auto``.
    batches = Batches(tmp_path, count=1, budget=9, working=True, auto=True)
    try:
        yield batches
    finally:
        batches.stop()


def test_read_commands_answer_the_state_before_a_writer_paused_in_its_transaction(
    auto_working: Batches,
) -> None:
    batch, dispatch = auto_working.batches[0], auto_working.dispatches[0]
    report = architect_report_file(auto_working.repo, dispatch, auto_working.briefs[0])
    auto_working.arm(IN_TRANSACTION)
    writer = auto_working.start("report", "submit", "--file", str(report))
    auto_working.runtime.wait_paused(IN_TRANSACTION)

    reads = {}
    started = time.monotonic()
    for name, command in (
        ("dispatch status", ("dispatch", "status", "--dispatch", dispatch)),
        ("auto-report", ("batch", "auto-report", "--batch", batch)),
        ("decision-packet", ("batch", "decision-packet", "--batch", batch)),
        ("qa status", ("qa", "status")),
    ):
        reads[name] = auto_working.run(*command)
    elapsed = time.monotonic() - started
    auto_working.runtime.resume()
    _, stderr = writer.communicate(timeout=60)
    after = auto_working.run("dispatch", "status", "--dispatch", dispatch)

    for name, result in reads.items():
        assert result.returncode == 0, f"{name}: {result.stderr}"
    assert elapsed < 30
    # The readers see the state before the transaction in flight, never half of it.
    assert '"state": "working"' in reads["dispatch status"].stdout
    assert '"state": "reported"' not in reads["dispatch status"].stdout
    assert writer.returncode == 0, stderr
    assert '"state": "reported"' in after.stdout


@pytest.fixture
def planned(tmp_path: Path) -> Iterator[Batches]:
    # Two approved batches without a dispatch: each counts the other, so a budget of two has one
    # free slot for each of them.
    batches = Batches(tmp_path, count=2, budget=2, working=False)
    try:
        yield batches
    finally:
        batches.stop()


def test_two_dispatch_creates_under_a_budget_with_one_free_slot_admit_one(
    planned: Batches,
) -> None:
    first_command = planned.dispatch_create(0)
    second_command = planned.dispatch_create(1)
    planned.arm(BEFORE_BEGIN)
    first = planned.start(*first_command)
    planned.runtime.wait_paused(BEFORE_BEGIN)

    second = planned.run(*second_command)
    planned.runtime.resume()
    _, stderr = first.communicate(timeout=60)

    assert second.returncode == 0, second.stderr
    assert first.returncode == 2, stderr
    assert _remedy(stderr).startswith("REMEDY: [revision_conflict] ")
    assert "generation" in stderr
    listed = planned.run("batch", "list", "--open")
    assert listed.returncode == 0, listed.stderr
    states = {
        batch["batch_id"]: batch["state"] for batch in json.loads(listed.stdout)["batches"]
    }
    assert states[planned.batches[1]] == "active"
    assert states[planned.batches[0]] == "awaiting-approval"
    assert (planned.repo / STATE_REL / "runtime.db").is_file()
