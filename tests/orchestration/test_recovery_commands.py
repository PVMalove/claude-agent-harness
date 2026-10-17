"""``coordinator.py recovery status|reload|resume`` (issue #684): headless, exit 0 or 2."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from harness.orchestration import coordinator, operational_hold, recovery_api
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.recovery_api import RecoveryInterrupted
from harness.orchestration.runtime_state_store import RuntimeStateStore
from tests.orchestration import test_recovery
from tests.orchestration.test_coordinator import pinned_sha256, repin_entry
from tests.orchestration.test_recovery_api import _approved_architect, _configure
from tests.orchestration.test_runtime_effect import (
    external_brief,
    launches,
    send_external,
)
from tests.orchestration.test_runtime_effect_faults import answer, stop_after_adapter
from tests.orchestration.test_runtime_reconcile_faults import Crash

Fixture = test_recovery.RecoveryTests
PACKAGE = Path(__file__).resolve().parents[2]
COORDINATOR = PACKAGE / "harness/orchestration/coordinator.py"


def _cli(
    fixture: Fixture, *arguments: str, prelude: str = ""
) -> subprocess.CompletedProcess[str]:
    """The coordinator CLI with no terminal and a closed standard input."""
    command = [
        "--repo",
        str(fixture.repo),
        "--state-dir",
        str(fixture.state_dir),
        *arguments,
    ]
    script = (
        f"{prelude}\nimport runpy, sys\nsys.argv = [{str(COORDINATOR)!r}, *{command!r}]\n"
        f"runpy.run_path({str(COORDINATOR)!r}, run_name='__main__')\n"
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        start_new_session=True,
        cwd=str(fixture.repo),
    )


def _store(fixture: Fixture) -> RuntimeStateStore:
    return RuntimeStateStore(fixture.repo, fixture.state_dir)


def _hold(fixture: Fixture, batch_id: str) -> None:
    operational_hold.hold(
        _store(fixture), batch_id, "runtime-compatibility", "the adapter speaks v2"
    )


def _resume(fixture: Fixture, batch_id: str, **approval: str) -> JsonObject:
    return recovery_api.resume(
        fixture.repo, fixture.state_dir, batch_id, approval=approval or None
    )


# -- the CLI -------------------------------------------------------------------------------------


def test_status_prints_the_structured_document_and_exits_0(recovery: Fixture) -> None:
    brief = _approved_architect(recovery)

    result = _cli(
        recovery, "recovery", "status", "--batch", brief["batch_id"], "--json"
    )

    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert (document["contract"], document["action"], document["outcome"]) == (
        "recoveryStatus.v1",
        "status",
        "done",
    )
    assert document["resumable"] is True


def test_a_refused_resume_prints_its_diagnostics_and_exits_2(
    recovery: Fixture,
) -> None:
    brief = _approved_architect(recovery)
    _hold(recovery, brief["batch_id"])
    coordinator.attention_check(recovery._args(batch=brief["batch_id"]))
    ledger = recovery._ledger_bytes()
    record = _store(recovery).read(brief["batch_id"])

    result = _cli(recovery, "recovery", "resume", "--batch", brief["batch_id"])

    assert result.returncode == 2, result.stdout + result.stderr
    document = json.loads(result.stdout)
    assert document["outcome"] == "refused"
    assert {item["code"] for item in document["refusals"]} == {"attention-open"}
    assert recovery._ledger_bytes() == ledger
    assert _store(recovery).read(brief["batch_id"]) == record


def test_resume_releases_a_hold_and_exits_0_without_creating_a_dispatch(
    recovery: Fixture,
) -> None:
    brief = _approved_architect(recovery)
    _hold(recovery, brief["batch_id"])
    dispatches = recovery._batch_record(brief["batch_id"])["dispatches"]

    result = _cli(recovery, "recovery", "resume", "--batch", brief["batch_id"])

    assert result.returncode == 0, result.stdout + result.stderr
    document = json.loads(result.stdout)
    assert document["outcome"] == "done"
    assert [step["step"] for step in document["steps"]] == ["release"]
    assert (document["dispatch_created"], document["worker_started"]) == (False, False)
    assert (
        document["next"]["command"]
        == f"dispatch send --dispatch {brief['dispatch_id']}"
    )
    record = _store(recovery).read(brief["batch_id"])
    assert record is not None and (record["state"], record["failure"]) == (
        "ready",
        None,
    )
    assert recovery._batch_record(brief["batch_id"])["dispatches"] == dispatches


def test_reload_exits_0_on_a_valid_and_2_on_an_invalid_configuration(
    recovery: Fixture,
) -> None:
    brief = _approved_architect(recovery)

    valid = _cli(recovery, "recovery", "reload", "--batch", brief["batch_id"])
    _configure(recovery, effort="hgh")
    invalid = _cli(recovery, "recovery", "reload", "--batch", brief["batch_id"])

    assert valid.returncode == 0, valid.stdout + valid.stderr
    assert json.loads(valid.stdout)["worker_started"] is False
    assert invalid.returncode == 2
    assert json.loads(invalid.stdout)["outcome"] == "refused"


def test_recovery_runs_without_textual(recovery: Fixture) -> None:
    brief = _approved_architect(recovery)

    result = _cli(
        recovery,
        "recovery",
        "status",
        "--batch",
        brief["batch_id"],
        prelude="import sys\nsys.modules['textual'] = None",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["resumable"] is True


def test_a_terminal_gate_without_a_terminal_refuses_instead_of_waiting(
    recovery: Fixture,
) -> None:
    """Under human_approval_gate tty the approval needs a terminal; without one the command
    refuses at once and never reads standard input."""
    batch = recovery._create_batch()
    brief = recovery._dispatch(batch["batch_id"], "architect")["brief"]
    recovery._start(brief["dispatch_id"])
    recovery._submit(
        brief["dispatch_id"],
        recovery._base_report(
            brief,
            "architect",
            outcome="blocked",
            blockers={"status": "present", "details": "operator review required"},
        ),
    )
    recovery._decide(batch["batch_id"], "block")
    _configure(recovery, human_approval_gate="tty")

    result = _cli(
        recovery,
        "recovery",
        "resume",
        "--batch",
        batch["batch_id"],
        "--approved-by",
        "Malove",
        "--approved-at",
        recovery.APPROVED_AT,
        "--note",
        "operator checked the stop",
    )

    assert result.returncode == 2, result.stdout + result.stderr
    document = json.loads(result.stdout)
    assert document["outcome"] == "refused"
    # The terminal gate refuses first: the human block itself is never reached.
    [refusal] = document["refusals"]
    assert "human_approval_gate is 'tty'" in refusal["message"]
    assert "not confirmed" not in refusal["message"]


def test_a_partial_approval_is_refused(recovery: Fixture) -> None:
    brief = _approved_architect(recovery)

    result = _cli(
        recovery,
        "recovery",
        "resume",
        "--batch",
        brief["batch_id"],
        "--approved-by",
        "Malove",
    )

    assert result.returncode == 2
    assert "together" in result.stderr


def test_the_cli_parser_names_the_recovery_commands() -> None:
    parsed = coordinator.parser().parse_args(
        ["recovery", "resume", "--batch", "batch-1", "--note", "n"]
    )
    assert (parsed.command, parsed.recovery_command, parsed.note) == (
        "recovery",
        "resume",
        "n",
    )
    assert parsed.installed_runtime_only is True
    assert parsed.handler is coordinator.recovery_resume


# -- reconciliation ------------------------------------------------------------------------------


def test_a_repeated_resume_changes_nothing_more(recovery: Fixture) -> None:
    brief = _approved_architect(recovery)
    _hold(recovery, brief["batch_id"])

    first = _resume(recovery, brief["batch_id"])
    record = _store(recovery).read(brief["batch_id"])
    ledger = recovery._ledger_bytes()
    second = _resume(recovery, brief["batch_id"])

    assert (first["outcome"], second["outcome"]) == ("done", "done")
    assert second["steps"] == []
    assert _store(recovery).read(brief["batch_id"]) == record
    assert recovery._ledger_bytes() == ledger


def test_concurrent_resumes_release_the_hold_once(recovery: Fixture) -> None:
    brief = _approved_architect(recovery)
    _hold(recovery, brief["batch_id"])
    held = _store(recovery).read(brief["batch_id"])
    assert held is not None
    outcomes: list[str] = []

    def run() -> None:
        try:
            outcomes.append(_resume(recovery, brief["batch_id"])["outcome"])
        except CoordinatorError as exc:
            outcomes.append(type(exc).__name__)

    threads = [threading.Thread(target=run) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert "done" in outcomes
    assert set(outcomes) <= {"done", "LedgerBusyError"}
    record = _store(recovery).read(brief["batch_id"])
    assert record is not None and record["state"] == "ready"
    # suspended -> reconciling -> ready: exactly one release, whoever ran it.
    assert record["revision"] == held["revision"] + 2


def test_an_interrupted_resume_keeps_its_intent_and_completes_on_a_rerun(
    recovery: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    brief = _approved_architect(recovery)
    _hold(recovery, brief["batch_id"])

    def interrupted(*_: object, **__: object) -> None:
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(recovery_api, "_reconcile", interrupted)
        with pytest.raises(RecoveryInterrupted):
            _resume(recovery, brief["batch_id"])

    restarted = recovery_api.status(
        recovery.repo, recovery.state_dir, brief["batch_id"]
    )
    assert (restarted["intent"]["action"], restarted["intent"]["status"]) == (
        "resume",
        "interrupted",
    )
    assert restarted["hold"] is not None
    assert _resume(recovery, brief["batch_id"])["outcome"] == "done"
    assert (
        recovery_api.status(recovery.repo, recovery.state_dir, brief["batch_id"])[
            "intent"
        ]["status"]
        == "done"
    )


def test_a_stale_approval_is_refused_and_not_reused(recovery: Fixture) -> None:
    brief = _approved_architect(recovery)
    _hold(recovery, brief["batch_id"])
    _configure(recovery, approval_ttl_seconds=60)
    record = _store(recovery).read(brief["batch_id"])

    result = _resume(recovery, brief["batch_id"])

    assert result["outcome"] == "refused"
    assert "approval-stale" in {item["code"] for item in result["refusals"]}
    assert _store(recovery).read(brief["batch_id"]) == record


def test_a_missing_snapshot_is_refused_and_the_brief_can_still_be_cancelled(
    recovery: Fixture,
) -> None:
    brief = _approved_architect(recovery)
    path = recovery._records() / "dispatches" / f"{brief['dispatch_id']}.json"
    corrupted = json.loads(path.read_bytes())
    del corrupted["orchestration_policy"]["configuration"]["config"]
    path.write_text(json.dumps(corrupted), encoding="utf-8")
    repin_entry(
        recovery.state_dir,
        brief["batch_id"],
        brief["dispatch_id"],
        brief_sha256=pinned_sha256(corrupted),
    )

    result = _resume(recovery, brief["batch_id"])

    assert result["outcome"] == "refused"
    assert result["status"]["hold"] is None
    cancelled = coordinator.cancel_dispatch(
        recovery._args(
            dispatch=brief["dispatch_id"],
            reason="its pinned snapshot is missing",
            runtime_stopped=False,
            **recovery._approval(),
        )
    )
    assert cancelled["state"] == "cancelled"


def test_an_unreadable_batch_is_held_through_the_store_alone(recovery: Fixture) -> None:
    brief = _approved_architect(recovery)
    path = recovery._records() / "batches" / f"{brief['batch_id']}.json"
    path.write_text("{", encoding="utf-8")
    ledger = recovery._ledger_bytes()

    result = _resume(recovery, brief["batch_id"])

    assert result["outcome"] == "refused"
    assert [step["step"] for step in result["steps"]] == ["hold"]
    held = operational_hold.current_hold(_store(recovery), brief["batch_id"])
    # The strict read names its route: history no migration vouches for is an unknown value.
    assert held is not None and held.reason == "unknown-value"
    assert "ledger migrate" in held.message
    assert recovery._ledger_bytes() == ledger


def test_an_ambiguous_hand_off_is_refused_and_never_started_again(
    recovery: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    brief, adapter = external_brief(recovery)
    stop_after_adapter(monkeypatch, ran=True)
    with pytest.raises(Crash):
        send_external(recovery, brief, adapter)
    monkeypatch.undo()
    assert len(launches(recovery)) == 1

    status = recovery_api.status(recovery.repo, recovery.state_dir, brief["batch_id"])
    assert any(item["code"] == "ambiguous-effect" for item in status["diagnostics"])
    result = _resume(recovery, brief["batch_id"])

    assert result["outcome"] == "refused"
    assert {item["code"] for item in result["refusals"]} == {"ambiguous-effect"}
    assert len(launches(recovery)) == 1


def test_a_live_worker_is_reconciled_and_no_second_one_is_started(
    recovery: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    brief, adapter = external_brief(recovery)
    stop_after_adapter(monkeypatch, ran=True)
    with pytest.raises(Crash):
        send_external(recovery, brief, adapter)
    monkeypatch.undo()
    answer(recovery.tmp, brief["dispatch_id"], launched=True, live=True)

    result = _resume(recovery, brief["batch_id"])

    assert result["outcome"] == "done"
    assert [step["step"] for step in result["steps"]] == ["reconcile"]
    assert (
        result["next"]["command"] == f"dispatch wait --dispatch {brief['dispatch_id']}"
    )
    assert len(launches(recovery)) == 1
    assert _resume(recovery, brief["batch_id"])["steps"] == []


def test_a_recorded_stop_continues_only_through_the_audited_resume_stop(
    recovery: Fixture,
) -> None:
    batch = recovery._create_batch()
    brief = recovery._dispatch(batch["batch_id"], "architect")["brief"]
    recovery._start(brief["dispatch_id"])
    recovery._submit(
        brief["dispatch_id"],
        recovery._base_report(
            brief,
            "architect",
            outcome="blocked",
            blockers={"status": "present", "details": "operator review required"},
        ),
    )
    recovery._decide(batch["batch_id"], "block")
    before = recovery._batch_record(batch["batch_id"])

    without = _resume(recovery, batch["batch_id"])

    assert without["outcome"] == "refused"
    assert {item["code"] for item in without["refusals"]} == {"approval-required"}
    route = next(
        item for item in without["status"]["checks"] if item["check"] == "lifecycle"
    )
    assert route["evidence"]["route"] == "resume-stop"
    # The audited resume-stop refuses a human block it cannot prove environmental.
    approved = _resume(
        recovery,
        batch["batch_id"],
        note="the operator checked the stop",
        **recovery._approval(),
    )
    assert approved["outcome"] == "refused"
    assert "not confirmed" in approved["refusals"][0]["message"]
    assert recovery._batch_record(batch["batch_id"]) == before


def test_an_environment_stop_resumes_through_resume_stop_and_keeps_its_auto_stop(
    recovery: Fixture,
) -> None:
    batch, _ = recovery._failed_launch()
    coordinator.auto_decide(recovery._args(batch=batch["batch_id"]))
    stopped = recovery._batch_record(batch["batch_id"])
    assert stopped["state"] == "paused"

    result = _resume(
        recovery,
        batch["batch_id"],
        note="the worker checkout is fixed",
        **recovery._approval(),
    )

    assert result["outcome"] == "done", result["refusals"]
    assert [step["step"] for step in result["steps"]] == ["resume-stop"]
    after = recovery._batch_record(batch["batch_id"])
    assert after["state"] == "awaiting-approval"
    assert after["manual_recovery"] is True
    assert after["auto_stop"] == stopped["auto_stop"]
    assert after["recovery_events"][-1]["kind"] == "resume-stop"
    assert result["next"]["command"].startswith("dispatch propose")


def test_a_terminal_batch_keeps_its_refusal(recovery: Fixture) -> None:
    batch = recovery._create_batch()
    coordinator.abandon_batch(
        recovery._args(
            batch=batch["batch_id"], reason="no longer wanted", **recovery._approval()
        )
    )
    before = recovery._batch_record(batch["batch_id"])

    result = _resume(recovery, batch["batch_id"])

    assert result["outcome"] == "refused"
    assert {item["code"] for item in result["refusals"]} == {"terminal-refusal"}
    assert recovery._batch_record(batch["batch_id"]) == before
