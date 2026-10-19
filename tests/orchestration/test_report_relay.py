"""A coordinator delivers a worker's unchanged report; policy still decides its result."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator

import pytest

from tests.orchestration import test_recovery as recovery_tests
from tests.orchestration.test_startup_lifecycle import cli


@pytest.fixture
def reporting() -> Iterator[recovery_tests.RecoveryTests]:
    fixture = recovery_tests.RecoveryTests()
    fixture.setUp()
    try:
        yield fixture
    finally:
        fixture.tearDown()


def test_relay_preserves_worker_bytes_checks_and_author(
    reporting: recovery_tests.RecoveryTests,
) -> None:
    project = reporting.repo / ".harness/project.json"
    project.write_text(
        json.dumps(
            {"qa_gate_commands": ["make lint", "pytest tests/memory", "make verify"]}
        )
    )
    batch = reporting._create_batch()
    reporting._accepted_architect(batch["batch_id"])
    brief = reporting._dispatch(batch["batch_id"], "developer")["brief"]
    reporting._start(brief["dispatch_id"])
    candidate, changed = reporting._developer_commit("relay")
    report = reporting._developer_report(
        brief,
        candidate,
        changed,
        checks_run=[
            {"command": "make lint", "result": "pass", "evidence": "passed"},
            {
                "command": "pytest tests/memory",
                "result": "fail",
                "evidence": "dataset hash mismatch",
            },
            {"command": "make verify", "result": "not_run", "evidence": "not executed"},
        ],
    )
    from pathlib import Path

    path = Path(brief["report_staging_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(report, indent=4).encode()
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    arguments = (
        "report",
        "submit",
        "--file",
        str(path),
        "--relayed-by",
        "operator",
        "--relay-reason",
        "worker sandbox denied ledger write",
        "--source-sha256",
        digest,
    )
    result = cli(reporting, *arguments)
    assert result["state"] == "reported"
    assert path.read_bytes() == raw
    stored = json.loads(Path(result["report"]).read_text())
    assert stored == report
    assert result["report_relay"]["author_dispatch_id"] == brief["dispatch_id"]
    assert result["report_relay"]["submitted_by"] == "operator"
    assert result["report_relay"]["source_sha256"] == digest
    again = cli(reporting, *arguments)
    assert again["already_recorded"] is True
    assert cli(reporting, "ledger", "validate")["valid"] is True


def test_human_retry_note_reaches_architect_without_a_second_attention_ack(
    reporting: recovery_tests.RecoveryTests,
) -> None:
    batch = reporting._create_batch()
    prior = reporting._dispatch(batch["batch_id"], "architect")["brief"]
    reporting._start(prior["dispatch_id"])
    reporting._submit(prior["dispatch_id"], reporting._base_report(prior, "architect"))
    note = "Keep the commit plan inside the approved paths and cover every DoD item."
    reporting._decide(batch["batch_id"], "retry", note=note)
    renewed = reporting._dispatch(batch["batch_id"], "architect")["brief"]
    assert renewed["retry_reason"]["note"] == note
    assert renewed["retry_reason"]["previous_dispatch_id"] == prior["dispatch_id"]
    assert renewed["transition"]["retry_reason_sha256"]
    assert renewed["write_paths"] == prior["write_paths"]
    assert cli(reporting, "ledger", "validate")["valid"] is True


@pytest.mark.parametrize("note", ["", "   ", "none"])
def test_explicit_empty_architect_retry_reason_leaves_report_pending(
    reporting: recovery_tests.RecoveryTests,
    note: str,
) -> None:
    from harness.orchestration.core.utils import CoordinatorError

    batch = reporting._create_batch()
    prior = reporting._dispatch(batch["batch_id"], "architect")["brief"]
    reporting._start(prior["dispatch_id"])
    reporting._submit(prior["dispatch_id"], reporting._base_report(prior, "architect"))
    with pytest.raises(CoordinatorError, match="non-empty.*reason"):
        reporting._decide(batch["batch_id"], "retry", note=note)
    assert (
        cli(reporting, "dispatch", "status", "--dispatch", prior["dispatch_id"])[
            "dispatches"
        ][0]["state"]
        == "reported"
    )
    assert cli(reporting, "ledger", "validate")["valid"] is True


def test_restart_refuses_a_report_decision_pause_without_changing_ledger(
    reporting: recovery_tests.RecoveryTests,
) -> None:
    from harness.orchestration import coordinator
    from harness.orchestration.core.utils import CoordinatorError

    reporting._use_external_developer()
    config_path = reporting.repo / ".harness/orchestration.json"
    config = json.loads(config_path.read_text())
    config["approval_policy"] = "auto"
    config["worker_attestation_required"] = True
    config_path.write_text(json.dumps(config))
    batch = reporting._create_batch()
    brief = reporting._dispatch(batch["batch_id"], "architect")["brief"]
    reporting._start(brief["dispatch_id"])
    reporting._submit(
        brief["dispatch_id"],
        reporting._base_report(brief, "architect", outcome="failed"),
    )
    coordinator.auto_decide(reporting._args(batch=batch["batch_id"]))
    before = reporting._ledger_bytes()
    with pytest.raises(CoordinatorError, match="no same-stage worker"):
        coordinator.resume_stopped_batch(
            reporting._args(
                batch=batch["batch_id"],
                restart=True,
                note="review report first",
                **reporting._approval(),
            )
        )
    assert reporting._ledger_bytes() == before
