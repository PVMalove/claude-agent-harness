"""A real dispatched role can preserve evidence after live config breaks."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness.orchestration import coordinator
from harness.orchestration.core import workspace
from harness.orchestration.core.utils import _canonical
from harness.json_types import JsonObject
from tests.orchestration import test_coordinator as fixtures


@pytest.fixture
def dispatched_architect() -> (
    Iterator[tuple[fixtures.CoordinatorLedgerMigrationTests, JsonObject]]
):
    case = fixtures.CoordinatorLedgerMigrationTests()
    case.setUp()
    try:
        batch = case._create_batch()
        case._approve_batch(batch["batch_id"])
        brief = case._create_architect_dispatch(batch["batch_id"])["brief"]
        coordinator.send_dispatch(
            fixtures._ns(
                repo=str(case.repo),
                state_dir=str(case.state_dir),
                dispatch=brief["dispatch_id"],
                adapter=None,
                adapter_arg=None,
                checkout=None,
            )
        )
        coordinator.self_report_dispatch(
            fixtures._ns(
                repo=str(case.repo),
                state_dir=str(case.state_dir),
                dispatch=brief["dispatch_id"],
                model="sonnet",
                worktree=str(case.tmp / "worktree"),
            )
        )
        yield case, brief
    finally:
        case.tearDown()
        # unittest runs cleanups only for its own runner: stop the config patch here too.
        case.doCleanups()


def architect_report(brief: JsonObject) -> JsonObject:
    return {
        "dispatch_id": brief["dispatch_id"],
        "ticket": brief["ticket"],
        "role": "architect",
        "outcome": "completed",
        "output": "architecture recorded",
        "commit_sha": "not applicable — read-only role",
        "changed_files": [],
        "checks_run": [],
        "risks": {"status": "none", "details": ""},
        "blockers": {"status": "none", "details": ""},
        "next_coordinator_action": "dispatch developer",
        "report_language": "ru",
    }


def test_report_is_persisted_under_pinned_policy_when_live_config_is_invalid(
    dispatched_architect: tuple[fixtures.CoordinatorLedgerMigrationTests, JsonObject],
) -> None:
    case, brief = dispatched_architect
    report = architect_report(brief)
    inbox = workspace._prepare_agent_inbox(case.repo) / "report.json"
    inbox.write_text(json.dumps(report))
    (case.repo / ".harness/orchestration.json").write_text("{")

    result = coordinator.submit_report(
        fixtures._ns(
            repo=str(case.repo),
            state_dir=str(case.state_dir),
            file=str(inbox),
        )
    )

    assert result["state"] == "reported"
    assert Path(result["report"]).is_file()
    assert result["transition_blocked"] is True
    assert "next_dispatch_id" not in result
    assert (
        brief["orchestration_policy"]["configuration"]["contract"] == "systemFields.v1"
    )


def test_corrupt_snapshot_captures_input_without_accepting_report(
    dispatched_architect: tuple[fixtures.CoordinatorLedgerMigrationTests, JsonObject],
) -> None:
    case, brief = dispatched_architect
    report = architect_report(brief)
    inbox = workspace._prepare_agent_inbox(case.repo) / "report.json"
    inbox.write_text(json.dumps(report))
    root = case._records_root()
    path = root / "dispatches" / f"{brief['dispatch_id']}.json"
    corrupted = json.loads(path.read_bytes())
    corrupted["orchestration_policy"]["configuration"]["canonical_sha256"] = "0" * 64
    path.write_text(json.dumps(corrupted))
    # The batch pins the corrupted brief, so only the snapshot's own digest refuses it.
    batch_path = root / "batches" / f"{brief['batch_id']}.json"
    batch = json.loads(batch_path.read_bytes())
    for entry in batch["dispatches"]:
        if entry["dispatch_id"] == brief["dispatch_id"]:
            entry["brief_sha256"] = hashlib.sha256(
                _canonical(corrupted).encode("utf-8")
            ).hexdigest()
    batch_path.write_text(json.dumps(batch))

    with pytest.raises(coordinator.CoordinatorError):
        coordinator.submit_report(
            fixtures._ns(
                repo=str(case.repo),
                state_dir=str(case.state_dir),
                file=str(inbox),
            )
        )

    captures = list((root / "unaccepted-evidence").glob("*.json"))
    assert len(captures) == 1
    capture = json.loads(captures[0].read_bytes())
    assert capture["accepted"] is False
    assert capture["input"] == report
    assert not (root / "reports" / f"{brief['dispatch_id']}.json").exists()


def test_checkpoint_survives_invalid_live_config() -> None:
    case = fixtures.CoordinatorRetryRoutingTests()
    case.setUp()
    try:
        case._patch_config(worker_attestation_required=True)
        batch = case._create_batch()
        case._accepted_architect(batch["batch_id"])
        brief = case._dispatch(batch["batch_id"], "developer")["brief"]
        case._start(brief["dispatch_id"])
        candidate, changed = case._developer_commit("checkpoint")
        (case.repo / ".harness/orchestration.json").write_text("{")

        checkpoint = case._checkpoint_writer(brief, candidate, changed)

        stored = list((case._records() / "checkpoints").glob("*.json"))
        assert len(stored) == 1
        assert (
            json.loads(stored[0].read_bytes())["commit_sha"] == checkpoint["commit_sha"]
        )
        assert (
            case._batch_record(batch["batch_id"])["dispatches"][-1]["state"]
            == "checkpointed"
        )
    finally:
        case.tearDown()
        # unittest runs cleanups only for its own runner: stop the config patch here too.
        case.doCleanups()


def test_qa_runner_preserves_report_if_gate_breaks_live_configuration() -> None:
    import shlex
    import subprocess
    import sys

    case = fixtures.CoordinatorRetryRoutingTests()
    case.setUp()
    try:
        case.state_dir = case.repo / ".harness/orchestration/state"
        path = case.repo / ".harness/orchestration.json"
        script = f"from pathlib import Path; Path({str(path)!r}).write_text('{{')"
        command = (
            subprocess.list2cmdline(["python", "-c", script])
            if sys.platform == "win32"
            else "python -c " + shlex.quote(script)
        )
        path.write_text(json.dumps({"verification_commands": [command]}))
        batch_id, candidate = case._qa_batch()
        brief = case._dispatch(batch_id, "qa", candidate=candidate)["brief"]

        result = coordinator.run_qa(
            fixtures._ns(
                repo=str(case.repo),
                state_dir=None,
                dispatch=brief["dispatch_id"],
                lease_seconds=None,
            )
        )

        assert result["state"] == "reported"
        assert Path(result["report"]).is_file()
        assert result["transition_blocked"] is True
        assert "next_dispatch_id" not in result
    finally:
        case.tearDown()
        # unittest runs cleanups only for its own runner: stop the config patch here too.
        case.doCleanups()


@pytest.mark.parametrize("policy", [None, 42, []])
def test_invalid_policy_container_is_rejected_with_a_domain_error(
    dispatched_architect: tuple[fixtures.CoordinatorLedgerMigrationTests, JsonObject],
    policy: object,
) -> None:
    case, brief = dispatched_architect
    path = case._records_root() / "dispatches" / f"{brief['dispatch_id']}.json"
    record = json.loads(path.read_bytes())
    record["orchestration_policy"] = policy
    path.write_text(json.dumps(record))
    with pytest.raises(coordinator.CoordinatorError):
        coordinator.send_dispatch(
            fixtures._ns(
                repo=str(case.repo),
                state_dir=str(case.state_dir),
                dispatch=brief["dispatch_id"],
                adapter=None,
                adapter_arg=None,
                checkout=None,
            )
        )
