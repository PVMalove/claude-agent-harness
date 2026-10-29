"""Operational holds: a batch held by its recovery record in the runtime store (ADR 0026).

The unit tests run on every platform. The seam tests drive the coordinator CLI against the real
Rust runtime: the real coordinator builds the state in the file epoch, ``harness-runtime migrate``
imports it, and afterwards only the CLI runs, without a terminal or stdin; a hold the coordinator
does not produce in the scenario is written through ``state.write``."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pytest

from harness.json_types import JsonObject as Document
from harness.orchestration import operational_hold
from harness.orchestration.core.system_fields import load_registry
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from tests.orchestration.runtime_store_support import (
    APPROVAL,
    Recovery,
    Supervised,
    architect_report_file,
    failed_launch,
    headless,
    hold_record,
    planned_batch,
    store_records,
    write_hold,
)

BATCH = "batch-0123456789abcdef"


def _record(**changes: object) -> JsonObject:
    record: JsonObject = {
        "contract": operational_hold.HOLD_CONTRACT,
        "batch_id": BATCH,
        "state": "held",
        "failure": {
            "code": "integrity-failure",
            "step": "hold:unknown-value",
            "message": "batch state 'parked' is not a registry value",
        },
        "updated_at": "2026-10-29T00:00:00+00:00",
    }
    record.update(changes)
    return record


# -- unit: the vocabulary and the record, on every platform --------------------------------------


def test_the_reasons_are_the_registry_values() -> None:
    assert (
        operational_hold.HOLD_REASONS
        == load_registry().fields["operational_hold.reason"].allowed
    )


@pytest.mark.parametrize("reason", operational_hold.HOLD_REASONS)
def test_the_failure_step_of_a_reason_names_it_back(reason: str) -> None:
    step = operational_hold.step_for(reason)

    assert step == f"hold:{reason}"
    assert operational_hold.reason_of(step) == reason
    assert operational_hold.failure_code(reason) == "integrity-failure"


@pytest.mark.parametrize(
    "step", ["hold:legacy-quarantine", "hold:", "reconcile:detect", None, 3]
)
def test_any_other_step_names_no_reason(step: object) -> None:
    assert operational_hold.reason_of(step) is None


def test_an_unknown_reason_has_no_step() -> None:
    with pytest.raises(ValueError, match="runtime-compatibility"):
        operational_hold.step_for("runtime-compatibility")


def test_a_held_record_is_a_releasable_hold_with_its_row_revision() -> None:
    held = operational_hold.of_record(BATCH, _record(), 3)

    assert held == operational_hold.Hold(
        BATCH,
        "unknown-value",
        "integrity-failure",
        "hold:unknown-value",
        "batch state 'parked' is not a registry value",
        3,
    )
    assert held.releasable
    document = held.to_dict()
    assert document["reason"] == "unknown-value"
    assert document["revision"] == 3
    assert f"recovery resume --batch {BATCH}" in document["remedy"]


def test_a_released_record_keeps_no_hold() -> None:
    assert (
        operational_hold.of_record(BATCH, _record(state="released", failure=None), 2)
        is None
    )


@pytest.mark.parametrize(
    "record",
    [
        _record(contract="runtimeState.v1"),
        _record(batch_id="batch-other"),
        _record(state="suspended"),
        _record(failure=None),
        _record(failure={"code": "integrity-failure", "step": "hold:parked", "message": "x"}),
        _record(failure={"code": "integrity-failure", "step": "hold:unknown-value"}),
        {"contract": operational_hold.HOLD_CONTRACT, "batch_id": BATCH},
    ],
)
def test_a_record_outside_the_contract_is_an_unreadable_hold(record: JsonObject) -> None:
    with pytest.raises(operational_hold.UnreadableHold):
        operational_hold.of_record(BATCH, record, 1)


def test_the_hold_refusal_keeps_its_code_step_and_remedy() -> None:
    held = operational_hold.of_record(BATCH, _record(), 4)
    assert held is not None

    error = held.error()

    assert isinstance(error, CoordinatorError)
    assert error.code == "integrity-failure"
    assert error.step == "hold:unknown-value"
    assert error.diagnostics == {"batch_id": BATCH, "reason": "unknown-value", "revision": 4}
    assert f"batch {BATCH} is held (unknown-value)" in error.message
    assert "recovery resume" in error.remedy


def test_an_unreadable_hold_is_not_releasable_and_names_no_resume() -> None:
    held = operational_hold.Hold(
        BATCH, "unreadable-record", "integrity-failure", "hold:unreadable-record", "x", None
    )

    assert not held.releasable
    assert "ledger validate --full" in held.remedy
    assert "recovery resume" not in held.remedy


def test_a_detail_is_redacted_and_bounded() -> None:
    detail = operational_hold.sanitize_detail(
        "token=sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789 in /home/someone/repo "
        + "x" * 400
    )

    assert "sk-ant" not in detail
    assert "/home/someone" not in detail
    assert len(detail) <= operational_hold.DETAIL_LIMIT
    assert operational_hold.sanitize_detail(detail) == detail


def test_the_file_ledger_epoch_keeps_no_hold(tmp_path: Path) -> None:
    assert operational_hold.current_hold(tmp_path, BATCH) is None
    operational_hold.require_no_hold(tmp_path, BATCH)


@pytest.mark.parametrize("act", ["hold", "release"])
def test_the_file_ledger_epoch_refuses_to_hold_or_release(tmp_path: Path, act: str) -> None:
    with pytest.raises(CoordinatorError) as caught:
        if act == "hold":
            operational_hold.hold(tmp_path, BATCH, "unknown-value", "x")
        else:
            operational_hold.release(tmp_path, BATCH)

    assert "harness-runtime migrate" in caught.value.remedy


# -- seam: the gates of a held batch, through the coordinator CLI and the real Rust runtime -------

seam = pytest.mark.skipif(
    sys.platform == "win32", reason="the seam tests drive a Unix-socket runtime"
)
linux = pytest.mark.skipif(
    sys.platform != "linux", reason="the runtime supervises processes on Linux"
)
APPROVED = (
    "--approved-by",
    APPROVAL["approved_by"],
    "--approved-at",
    APPROVAL["approved_at"],
)


Build = Callable[[Path], tuple[Path, dict[str, str]]]


class Make(Protocol):
    def __call__(self, *, report: bool = ..., build: Build | None = ...) -> Recovery: ...


@pytest.fixture
def made(tmp_path: Path) -> Iterator[Make]:
    """The recovery fixture of a test, with the options it needs; stopped afterwards."""
    found: list[Recovery] = []

    def make(*, report: bool = False, build: Build | None = None) -> Recovery:
        recovery = Recovery(tmp_path, report=report, build=build)
        found.append(recovery)
        return recovery

    yield make
    for recovery in found:
        recovery.stop()


@pytest.fixture
def supervised(tmp_path: Path) -> Iterator[Supervised]:
    found = Supervised(tmp_path)
    try:
        yield found
    finally:
        found.close()


def _ok(result: subprocess.CompletedProcess[str]) -> Document:
    assert result.returncode == 0, result.stderr
    answer: Document = json.loads(result.stdout)
    return answer


def _refused_on_hold(
    recovery: Recovery, *command: str, reason: str = "unknown-value"
) -> None:
    """The command exits 2 with the hold and its recovery route, and the store keeps every row."""
    before = recovery.audit_records()

    result = recovery.cli(*command)

    assert result.returncode == 2, (result.stdout, result.stderr)
    assert f"batch {recovery.batch} is held ({reason})" in result.stderr, result.stderr
    assert f"recovery resume --batch {recovery.batch}" in result.stderr
    assert recovery.audit_records() == before


def _released(recovery: Recovery, *approval: str) -> Document:
    """``recovery resume`` releases the hold; the document it printed."""
    result, document = recovery.recovery("resume", *approval)
    assert document is not None, result.stderr
    hold = recovery.records(recovery.hold_path)[recovery.hold_path]
    assert (hold["state"], hold["failure"]) == ("released", None), document
    return document


def _sent(recovery: Recovery) -> str:
    sent = _ok(recovery.cli("dispatch", "send", "--dispatch", recovery.dispatch))
    return str(sent["worker_worktree"])


def _self_report(recovery: Recovery, worktree: str) -> list[str]:
    return [
        "dispatch",
        "self-report",
        "--dispatch",
        recovery.dispatch,
        "--model",
        "sonnet",
        "--worktree",
        worktree,
    ]


def _working(recovery: Recovery) -> None:
    _ok(recovery.cli(*_self_report(recovery, _sent(recovery))))


def _report(recovery: Recovery) -> str:
    path = f"dispatches/{recovery.dispatch}.json"
    brief = recovery.records(path)[path]
    return str(architect_report_file(recovery.repo, recovery.dispatch, brief))


def _checkpoint(recovery: Recovery) -> str:
    from harness.orchestration.core import workspace

    path = (
        workspace._prepare_agent_inbox(recovery.repo)
        / f"{recovery.dispatch}-checkpoint.json"
    )
    path.write_text(json.dumps({"dispatch_id": recovery.dispatch}), encoding="utf-8")
    return str(path)


def _blocked(recovery: Recovery) -> None:
    _ok(
        recovery.cli(
            "batch",
            "decide",
            "--batch",
            recovery.batch,
            "--decision",
            "block",
            *APPROVED,
        )
    )


def _submit(r: Recovery) -> list[str]:
    _working(r)
    return ["report", "submit", "--file", _report(r)]


def _checkpointed(r: Recovery) -> list[str]:
    _working(r)
    return ["dispatch", "checkpoint", "--file", _checkpoint(r)]


def _resume_stop(r: Recovery) -> list[str]:
    _blocked(r)
    return ["batch", "resume-stop", "--batch", r.batch, *APPROVED, "--note", "checked"]


def _rewind(r: Recovery) -> list[str]:
    _blocked(r)
    return [
        "batch",
        "rewind",
        "--batch",
        r.batch,
        "--to",
        "architect",
        *APPROVED,
        "--note",
        "checked",
    ]


@dataclass(frozen=True)
class Gate:
    """A command a held batch refuses: whether its batch has a report, the command (after the
    state it needs is prepared), and whether it succeeds once the hold is released; otherwise
    it refuses for its own reason, past the hold."""

    report: bool
    command: Callable[[Recovery], list[str]]
    passes: bool


GATES = {
    "dispatch send": Gate(
        False, lambda r: ["dispatch", "send", "--dispatch", r.dispatch], True
    ),
    "dispatch self-report": Gate(False, lambda r: _self_report(r, _sent(r)), True),
    "report submit": Gate(False, _submit, True),
    "dispatch checkpoint": Gate(False, _checkpointed, False),
    "qa run": Gate(False, lambda r: ["qa", "run", "--dispatch", r.dispatch], False),
    "dispatch publish": Gate(
        False,
        lambda r: [
            "dispatch",
            "publish",
            "--dispatch",
            r.dispatch,
            "--remote",
            "origin",
        ],
        False,
    ),
    "batch decide": Gate(
        True,
        lambda r: [
            "batch",
            "decide",
            "--batch",
            r.batch,
            "--decision",
            "block",
            *APPROVED,
        ],
        True,
    ),
    "batch auto-decide": Gate(
        True, lambda r: ["batch", "auto-decide", "--batch", r.batch], False
    ),
    "batch resume": Gate(
        False,
        lambda r: [
            "batch",
            "resume",
            "--batch",
            r.batch,
            "--reason",
            "the worker is fixed",
        ],
        False,
    ),
    "batch resume-stop": Gate(True, _resume_stop, False),
    "batch rewind": Gate(True, _rewind, False),
}


@seam
@pytest.mark.parametrize("name", list(GATES))
def test_a_held_batch_refuses_the_command_until_recovery_resume_releases_it(
    made: Make, name: str
) -> None:
    gate = GATES[name]
    recovery = made(report=gate.report)
    command = gate.command(recovery)
    recovery.hold()

    _refused_on_hold(recovery, *command)

    _released(recovery)
    after = recovery.cli(*command)
    assert "is held" not in after.stderr, after.stderr
    if gate.passes:
        assert after.returncode == 0, after.stderr


@seam
def test_a_held_batch_takes_no_new_brief_until_recovery_resume(
    made: Make,
) -> None:
    recovery = made(build=planned_batch)
    fields = [
        "--batch",
        recovery.batch,
        "--role",
        "architect",
        "--runtime",
        "claude",
        "--purpose",
        "work",
        "--model",
        "sonnet",
        "--effort",
        "high",
    ]
    digest = _ok(recovery.cli("dispatch", "propose", *fields))["transition_digest"]
    create = [
        "dispatch",
        "create",
        *fields,
        "--transition-digest",
        str(digest),
        *APPROVED,
    ]
    recovery.hold()

    _refused_on_hold(recovery, "dispatch", "propose", *fields)
    _refused_on_hold(recovery, *create)

    _released(recovery)
    brief = _ok(recovery.cli(*create))["brief"]
    assert isinstance(brief, dict) and brief["batch_id"] == recovery.batch


@seam
def test_a_batch_held_by_recovery_resume_waits_until_resume_releases_it(
    made: Make,
) -> None:
    """The real producer: a batch record the strict read refuses is held by ``recovery resume``;
    a repeated hold adds no revision, and the hold outlives the repaired record."""
    recovery = made()
    batch = recovery.records(recovery.batch_path)[recovery.batch_path]
    recovery.replace(
        recovery.batch_path,
        {key: value for key, value in batch.items() if key != "approval_policy"},
    )
    for _ in range(2):
        held, document = recovery.recovery("resume")
        assert held.returncode == 2
        assert document is not None
    hold = recovery.records(recovery.hold_path)[recovery.hold_path]
    assert operational_hold.reason_of(hold["failure"]["step"]) == "missing-value"
    assert recovery.revisions(recovery.hold_path)[recovery.hold_path] == 1
    recovery.replace(recovery.batch_path, batch)

    _refused_on_hold(
        recovery,
        "dispatch",
        "send",
        "--dispatch",
        recovery.dispatch,
        reason="missing-value",
    )

    document = _released(recovery)
    assert [step["step"] for step in document["steps"]] == ["release"]
    _sent(recovery)


@seam
def test_an_unreadable_hold_record_blocks_every_action_and_names_no_resume(
    made: Make,
) -> None:
    recovery = made()
    write_hold(
        recovery.repo,
        recovery.runtime,
        recovery.batch,
        {**hold_record(recovery.batch), "state": "suspended"},
    )
    before = recovery.audit_records()

    result = recovery.cli("dispatch", "send", "--dispatch", recovery.dispatch)

    assert result.returncode == 2
    assert f"batch {recovery.batch} is held (unreadable-record)" in result.stderr
    assert "ledger validate --full" in result.stderr
    assert "recovery resume" not in result.stderr
    assert recovery.audit_records() == before
    resumed, document = recovery.recovery("resume")
    assert resumed.returncode == 2
    assert document is not None and document["steps"] == []
    stored = recovery.records(recovery.hold_path)[recovery.hold_path]
    assert stored["state"] == "suspended"


@seam
def test_a_terminal_batch_is_never_held(made: Make) -> None:
    recovery = made()
    _ok(
        recovery.cli(
            "batch",
            "abandon",
            "--batch",
            recovery.batch,
            *APPROVED,
            "--reason",
            "not wanted",
        )
    )
    batch = recovery.records(recovery.batch_path)[recovery.batch_path]
    recovery.replace(
        recovery.batch_path,
        {key: value for key, value in batch.items() if key != "approval_policy"},
    )

    result, document = recovery.recovery("resume")

    assert result.returncode == 2
    assert document is not None and document["steps"] == []
    assert recovery.hold_path not in recovery.records(recovery.hold_path)


@seam
def test_heartbeat_attention_cancel_and_abandon_stay_available_on_a_held_batch(
    made: Make,
) -> None:
    recovery = made()
    _working(recovery)
    recovery.hold()

    _ok(recovery.cli("dispatch", "heartbeat", "--dispatch", recovery.dispatch))
    _ok(recovery.cli("dispatch", "status", "--batch", recovery.batch))
    checked = _ok(
        recovery.cli("batch", "attention", "check", "--batch", recovery.batch)
    )
    assert (checked["needs_attention"], checked["attention_reason"]) == (
        True,
        "operational-hold",
    )
    packet = _ok(recovery.cli("batch", "decision-packet", "--batch", recovery.batch))
    assert packet["options"] == []
    hold = packet["operational_hold"]
    assert isinstance(hold, dict) and hold["reason"] == "unknown-value"
    route = packet["recovery_route"]
    assert isinstance(route, dict) and route["commands"] == [
        f"recovery status --batch {recovery.batch}",
        f"batch attention resolve --batch {recovery.batch}",
        f"recovery resume --batch {recovery.batch}",
    ]
    _ok(
        recovery.cli(
            "batch",
            "attention",
            "resolve",
            "--batch",
            recovery.batch,
            "--note",
            "seen",
            *APPROVED,
        )
    )
    _ok(
        recovery.cli(
            "dispatch",
            "cancel",
            "--dispatch",
            recovery.dispatch,
            *APPROVED,
            "--runtime-stopped",
            "--reason",
            "the worker is gone",
        )
    )
    abandoned = _ok(
        recovery.cli(
            "batch",
            "abandon",
            "--batch",
            recovery.batch,
            *APPROVED,
            "--reason",
            "not wanted",
        )
    )
    assert abandoned["state"] == "abandoned"


@seam
def test_the_decision_packet_of_a_held_report_offers_no_decision(
    made: Make,
) -> None:
    recovery = made(report=True)
    recovery.hold()

    packet = _ok(recovery.cli("batch", "decision-packet", "--batch", recovery.batch))

    assert packet["dispatch_id"] == recovery.dispatch
    assert packet["options"] == []
    route_preview = packet["route_preview"]
    assert isinstance(route_preview, dict) and set(route_preview) == {"hold"}
    route = packet["recovery_route"]
    assert isinstance(route, dict) and route["commands"] == [
        f"recovery status --batch {recovery.batch}",
        f"recovery resume --batch {recovery.batch}",
    ]


@seam
def test_attention_names_the_hold_and_the_route_it_names_releases_it(
    made: Make,
) -> None:
    recovery = made()
    recovery.hold()
    checked = _ok(
        recovery.cli("batch", "attention", "check", "--batch", recovery.batch)
    )
    assert checked["attention_reason"] == "operational-hold"

    refused, document = recovery.recovery("resume")

    assert refused.returncode == 2
    assert document is not None
    assert {item["code"] for item in document["refusals"]} == {"attention-open"}
    held = recovery.records(recovery.hold_path)[recovery.hold_path]
    assert held["state"] == "held"
    _ok(
        recovery.cli(
            "batch",
            "attention",
            "resolve",
            "--batch",
            recovery.batch,
            "--note",
            "the cause is fixed",
            *APPROVED,
        )
    )
    released = _released(recovery)
    assert [step["step"] for step in released["steps"]] == ["release"]
    again = _ok(recovery.cli("batch", "attention", "check", "--batch", recovery.batch))
    assert again["needs_attention"] is False


@seam
def test_auto_decide_on_a_held_batch_records_the_operational_hold_stop_and_never_abandons(
    made: Make,
) -> None:
    recovery = made(build=failed_launch)
    recovery.hold()

    decided = _ok(recovery.cli("batch", "auto-decide", "--batch", recovery.batch))

    assert decided["outcome"] == "stopped"
    stop = decided["stop"]
    assert isinstance(stop, dict)
    assert (stop["category"], stop["reason"]) == (
        "integrity-failure",
        "operational-hold",
    )
    batch = recovery.records(recovery.batch_path)[recovery.batch_path]
    assert batch["state"] == "paused"
    assert batch["auto_stop"]["reason"] == "operational-hold"
    hold = recovery.records(recovery.hold_path)[recovery.hold_path]
    assert hold["state"] == "held"
    # With its stop recorded the automatic path is over: a further call refuses on the hold.
    _refused_on_hold(recovery, "batch", "auto-decide", "--batch", recovery.batch)


@seam
def test_an_environment_stop_resumes_through_resume_stop_and_keeps_its_auto_stop(
    made: Make,
) -> None:
    recovery = made(build=failed_launch)
    decided = _ok(recovery.cli("batch", "auto-decide", "--batch", recovery.batch))
    stop = decided["stop"]
    assert isinstance(stop, dict) and stop["reason"] == "worktree-mismatch"
    stopped = recovery.records(recovery.batch_path)[recovery.batch_path]
    assert stopped["state"] == "paused"
    recovery.hold()
    _refused_on_hold(
        recovery,
        "batch",
        "resume-stop",
        "--batch",
        recovery.batch,
        *APPROVED,
        "--note",
        "the worker checkout is fixed",
    )

    result, document = recovery.recovery(
        "resume", *APPROVED, "--note", "the worker checkout is fixed"
    )

    assert result.returncode == 0, result.stderr
    assert document is not None
    assert [step["step"] for step in document["steps"]] == ["release", "resume-stop"]
    after = recovery.records(recovery.batch_path)[recovery.batch_path]
    assert after["state"] == "awaiting-approval"
    assert after["manual_recovery"] is True
    assert after["auto_stop"] == stopped["auto_stop"]
    assert after["recovery_events"][-1]["kind"] == "resume-stop"
    assert document["next"]["command"].startswith("dispatch propose")


@linux
def test_a_held_batch_keeps_the_report_pending_when_its_worker_exits(
    supervised: Supervised,
) -> None:
    """The reaction to an ended execution stops on the hold: ``dispatch status`` shows it as
    ``reaction_error`` and the report waits until ``recovery resume`` releases the hold."""
    worktree = _ok(supervised.send())["worker_worktree"]
    _ok(
        supervised.run(
            "dispatch",
            "self-report",
            "--dispatch",
            supervised.dispatch,
            "--model",
            "sonnet",
            "--worktree",
            str(worktree),
        )
    )
    path = f"dispatches/{supervised.dispatch}.json"
    brief = store_records(supervised.repo, supervised.runtime, path)[path]
    report = architect_report_file(supervised.repo, supervised.dispatch, brief)
    _ok(supervised.run("report", "submit", "--file", str(report)))
    write_hold(
        supervised.repo,
        supervised.runtime,
        supervised.batch,
        hold_record(supervised.batch),
    )
    supervised.release(0)
    supervised.wait_execution("exited")

    viewed = _ok(
        supervised.run("dispatch", "status", "--dispatch", supervised.dispatch)
    )

    (entry,) = viewed["dispatches"]
    assert f"batch {supervised.batch} is held" in entry["reaction_error"]["error"]
    assert "recovery resume" in entry["reaction_error"]["remedy"]
    status = supervised.status()
    assert status["state"] == "working"
    assert "pending_report" in status
    document = _ok(
        headless(
            supervised.repo,
            supervised.runtime,
            "recovery",
            "resume",
            "--batch",
            supervised.batch,
        )
    )
    assert [step["step"] for step in document["steps"]] == ["release", "reconcile"]
    assert supervised.status()["state"] == "reported"
    assert len(supervised.launches()) == 1
