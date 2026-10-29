"""``coordinator.py recovery status|reload|resume|fix|abort`` against the real Rust runtime: the
coordinator CLI as a console runs it, without a terminal or stdin (ADR 0026).

The state is built by the real coordinator in the file epoch, imported by ``harness-runtime
migrate`` and served by ``harness-runtime serve``; afterwards only the CLI runs. Data the
coordinator never writes goes in through ``state.write``, and a fault that data cannot express is
a prelude in the coordinator process."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness.json_types import JsonObject as Document
from harness.orchestration import coordinator
from tests.orchestration.runtime_store_support import (
    APPROVAL,
    Recovery,
    Supervised,
    coordinator_popen,
    headless,
)

seam = pytest.mark.skipif(
    sys.platform == "win32", reason="the seam tests drive a Unix-socket runtime"
)
linux = pytest.mark.skipif(
    sys.platform != "linux", reason="the runtime supervises processes on Linux"
)
APPROVED = ("--approved-by", APPROVAL["approved_by"], "--approved-at", APPROVAL["approved_at"])


@pytest.fixture
def recovery(tmp_path: Path) -> Iterator[Recovery]:
    found = Recovery(tmp_path)
    try:
        yield found
    finally:
        found.stop()


@pytest.fixture
def reported(tmp_path: Path) -> Iterator[Recovery]:
    """A batch whose architect dispatch reported: it awaits its decision."""
    found = Recovery(tmp_path, report=True)
    try:
        yield found
    finally:
        found.stop()


@pytest.fixture
def supervised(tmp_path: Path) -> Iterator[Supervised]:
    found = Supervised(tmp_path)
    try:
        yield found
    finally:
        found.close()


def _hold_record(recovery: Recovery) -> Document | None:
    return recovery.records(recovery.hold_path).get(recovery.hold_path)


def _intent(recovery: Recovery) -> Document:
    return recovery.records(recovery.intent_path)[recovery.intent_path]


def _decide(recovery: Recovery, decision: str) -> None:
    result = recovery.cli(
        "batch", "decide", "--batch", recovery.batch, "--decision", decision, *APPROVED
    )
    assert result.returncode == 0, result.stderr


# -- the process contract ------------------------------------------------------------------------


@seam
def test_status_prints_the_structured_document_and_exits_0(recovery: Recovery) -> None:
    before = recovery.audit_records()

    result, document = recovery.recovery("status", "--json")

    assert result.returncode == 0, result.stderr
    assert document is not None
    assert (document["contract"], document["action"], document["outcome"]) == (
        "recoveryStatus.v1",
        "status",
        "done",
    )
    assert [item["check"] for item in document["checks"]][0] == "config"
    assert len(document["checks"]) == 13
    assert document["resumable"] is True
    assert recovery.audit_records() == before


@seam
def test_a_refused_resume_prints_its_diagnostics_and_exits_2(recovery: Recovery) -> None:
    batch = recovery.records(recovery.batch_path)[recovery.batch_path]
    recovery.replace(
        recovery.batch_path,
        {
            **batch,
            "needs_attention": True,
            "attention_reason": "stale-dispatch",
            "attention_since": "2026-10-29T00:00:00+00:00",
            "last_safe_action": "dispatch heartbeat",
            "recommended_human_action": "check the worker",
        },
    )
    revision = recovery.revisions(recovery.batch_path)[recovery.batch_path]

    result, document = recovery.recovery("resume")

    assert result.returncode == 2, result.stderr
    assert document is not None and document["outcome"] == "refused"
    assert {item["code"] for item in document["refusals"]} == {"attention-open"}
    assert document["dispatch_created"] is False
    assert recovery.revisions(recovery.batch_path)[recovery.batch_path] == revision
    assert _hold_record(recovery) is None
    assert (_intent(recovery)["action"], _intent(recovery)["status"]) == (
        "resume",
        "refused",
    )


@seam
def test_resume_releases_a_hold_and_exits_0_without_creating_a_dispatch(
    recovery: Recovery,
) -> None:
    recovery.hold()

    result, document = recovery.recovery("resume")

    assert result.returncode == 0, result.stderr
    assert document is not None
    assert [step["step"] for step in document["steps"]] == ["release"]
    assert (document["dispatch_created"], document["worker_started"]) == (False, False)
    assert document["next"]["command"] == f"dispatch send --dispatch {recovery.dispatch}"
    hold = _hold_record(recovery)
    assert hold is not None and (hold["state"], hold["failure"]) == ("released", None)
    assert recovery.revisions(recovery.hold_path)[recovery.hold_path] == 2
    dispatches = recovery.cli("dispatch", "status", "--batch", recovery.batch)
    assert dispatches.returncode == 0, dispatches.stderr


@seam
def test_reload_exits_0_on_a_valid_and_2_on_an_invalid_configuration(
    recovery: Recovery,
) -> None:
    valid, document = recovery.recovery("reload")
    assert valid.returncode == 0, valid.stderr
    assert document is not None and document["runtime"]["compatible"] is True

    (recovery.repo / ".harness/orchestration.json").write_text(
        json.dumps({"concurrency_budget": "many"}), encoding="utf-8"
    )
    invalid, refused = recovery.recovery("reload")

    assert invalid.returncode == 2
    assert refused is not None and refused["outcome"] == "refused"
    assert {item["code"] for item in refused["diagnostics"]} == {"config-invalid"}


@seam
def test_recovery_runs_without_textual(recovery: Recovery) -> None:
    result, document = recovery.recovery(
        "status", python="import sys\nsys.modules['textual'] = None\n"
    )

    assert result.returncode == 0, result.stderr
    assert document is not None and document["resumable"] is True


@seam
def test_a_terminal_gate_without_a_terminal_refuses_instead_of_waiting(
    reported: Recovery,
) -> None:
    """Under human_approval_gate tty the approval needs a terminal; without one the command
    refuses at once and never reads standard input."""
    _decide(reported, "block")
    (reported.repo / ".harness/orchestration.json").write_text(
        json.dumps({"human_approval_gate": "tty"}), encoding="utf-8"
    )

    result, document = reported.recovery("resume", *APPROVED, "--note", "checked")

    assert result.returncode == 2, result.stderr
    assert document is not None and document["outcome"] == "refused"
    [refusal] = document["refusals"]
    assert "human_approval_gate is 'tty'" in refusal["message"]


@seam
def test_a_partial_approval_is_refused(recovery: Recovery) -> None:
    result, document = recovery.recovery(
        "resume", "--approved-by", APPROVAL["approved_by"]
    )

    assert result.returncode == 2
    assert document is None
    assert "together" in result.stderr
    assert result.stderr.splitlines()[0].startswith("ERROR: ")


def test_the_cli_parser_names_the_recovery_commands() -> None:
    parser = coordinator.parser()
    for name in ("status", "reload", "resume", "fix", "abort"):
        args = parser.parse_args(["recovery", name, "--batch", "batch-1"])
        assert args.installed_runtime_only is True
        assert args.handler is getattr(coordinator, f"recovery_{name}")
        with pytest.raises(SystemExit):
            parser.parse_args(["recovery", name])
    status = parser.parse_args(
        ["recovery", "status", "--batch", "b", "--skip", "x", "--skip", "y", "--json"]
    )
    assert status.skip == ["x", "y"]
    assert parser.parse_args(["recovery", "fix", "--batch", "b", "--apply"]).apply is True


@seam
def test_the_file_ledger_epoch_refuses_with_the_migrate_remedy(tmp_path: Path) -> None:
    from tests.orchestration.runtime_store_support import (
        ServingRuntime,
        write_coordinator_state,
    )

    repo, ids = write_coordinator_state(tmp_path, report=False)
    runtime = ServingRuntime(repo)
    try:
        result = headless(repo, runtime, "recovery", "resume", "--batch", ids["batch_id"])
    finally:
        runtime.stop()

    assert result.returncode == 2
    assert result.stdout == ""
    assert "harness-runtime migrate" in result.stderr


# -- idempotency and concurrency -----------------------------------------------------------------


@seam
def test_a_repeated_resume_changes_nothing_more(recovery: Recovery) -> None:
    recovery.hold()
    first, _ = recovery.recovery("resume")
    assert first.returncode == 0, first.stderr

    again, document = recovery.recovery("resume")

    assert again.returncode == 0, again.stderr
    assert document is not None and document["steps"] == []
    assert recovery.revisions(recovery.hold_path)[recovery.hold_path] == 2


@seam
def test_concurrent_resumes_release_the_hold_once(recovery: Recovery) -> None:
    recovery.hold()
    processes = [
        coordinator_popen(
            recovery.repo, recovery.runtime, "recovery", "resume", "--batch", recovery.batch
        )
        for _ in range(3)
    ]
    finished = [(process, *process.communicate(timeout=120)) for process in processes]

    codes = [process.returncode for process, _, _ in finished]
    assert set(codes) <= {0, 2}, finished
    assert 0 in codes
    for process, _, stderr in finished:
        if process.returncode == 2 and not stderr.startswith("ERROR"):
            continue
        if process.returncode == 2:
            assert any(
                code in stderr for code in ("[revision_conflict]", "[store_busy]", "busy")
            ), stderr
    hold = _hold_record(recovery)
    assert hold is not None and hold["state"] == "released"
    # One create, one release: the hold was released exactly once.
    assert recovery.revisions(recovery.hold_path)[recovery.hold_path] == 2


@seam
def test_an_interrupted_resume_keeps_its_intent_and_completes_on_a_rerun(
    recovery: Recovery,
) -> None:
    recovery.hold()
    interrupt = (
        "from harness.orchestration import operational_hold\n"
        "def _interrupted(*_args, **_kwargs):\n"
        "    raise KeyboardInterrupt\n"
        "operational_hold.release = _interrupted\n"
    )

    result, document = recovery.recovery("resume", python=interrupt)

    assert result.returncode == 2
    assert document is None
    assert "interrupted" in result.stderr
    assert _intent(recovery)["status"] == "interrupted"
    hold = _hold_record(recovery)
    assert hold is not None and hold["state"] == "held"

    rerun, completed = recovery.recovery("resume")

    assert rerun.returncode == 0, rerun.stderr
    assert completed is not None
    assert [step["step"] for step in completed["steps"]] == ["release"]
    assert (_intent(recovery)["status"], _intent(recovery)["action"]) == ("done", "resume")


# -- the checks resume repeats -------------------------------------------------------------------


@seam
def test_a_stale_approval_is_refused_and_not_reused(recovery: Recovery) -> None:
    (recovery.repo / ".harness/orchestration.json").write_text(
        json.dumps({"approval_ttl_seconds": 60}), encoding="utf-8"
    )

    result, document = recovery.recovery("resume")

    assert result.returncode == 2, result.stderr
    assert document is not None
    assert "approval-stale" in {item["code"] for item in document["refusals"]}
    status = recovery.records(f"dispatch-status/{recovery.dispatch}.json")
    assert status[f"dispatch-status/{recovery.dispatch}.json"]["state"] == "approved"


@seam
def test_a_missing_snapshot_is_refused_and_the_brief_can_still_be_cancelled(
    recovery: Recovery,
) -> None:
    missing = (
        "from harness.orchestration import pinned_configuration\n"
        "from harness.orchestration.core.utils import CoordinatorError\n"
        "def _missing(dispatch):\n"
        "    raise CoordinatorError('the brief pins no configuration snapshot',\n"
        "                           remedy='restore the approved brief')\n"
        "pinned_configuration.read_snapshot = _missing\n"
    )

    result, document = recovery.recovery("resume", python=missing)

    assert result.returncode == 2
    assert document is not None
    [refusal] = [item for item in document["refusals"] if item["check"] == "snapshot"]
    assert refusal["pointer"] == (
        f"/dispatches/{recovery.dispatch}/orchestration_policy/configuration"
    )
    cancelled = recovery.cli(
        "dispatch", "cancel", "--dispatch", recovery.dispatch, *APPROVED,
        "--reason", "the snapshot is gone",
    )
    assert cancelled.returncode == 0, cancelled.stderr


@seam
def test_an_unreadable_batch_is_held_through_the_store_alone(recovery: Recovery) -> None:
    batch = recovery.records(recovery.batch_path)[recovery.batch_path]
    incomplete = {key: value for key, value in batch.items() if key != "approval_policy"}
    assert incomplete != batch
    recovery.replace(recovery.batch_path, incomplete)
    revision = recovery.revisions(recovery.batch_path)[recovery.batch_path]

    result, document = recovery.recovery("resume")

    assert result.returncode == 2
    assert document is not None
    assert [step["step"] for step in document["steps"]] == ["hold"]
    assert document["steps"][0]["hold"]["reason"] == "missing-value"
    hold = _hold_record(recovery)
    assert hold is not None and hold["state"] == "held"
    # The batch record itself is never written for it.
    assert recovery.revisions(recovery.batch_path)[recovery.batch_path] == revision


@seam
def test_a_recorded_stop_continues_only_through_the_audited_resume_stop(
    reported: Recovery,
) -> None:
    _decide(reported, "block")
    before = reported.records(reported.batch_path)[reported.batch_path]

    without, refused = reported.recovery("resume")

    assert without.returncode == 2
    assert refused is not None
    assert {item["code"] for item in refused["refusals"]} == {"approval-required"}
    lifecycle = next(c for c in refused["status"]["checks"] if c["check"] == "lifecycle")
    assert lifecycle["evidence"]["route"] in {"resume-stop", "rewind"}
    approved, document = reported.recovery("resume", *APPROVED, "--note", "checked")
    assert approved.returncode == 2
    assert document is not None and document["outcome"] == "refused"
    assert reported.records(reported.batch_path)[reported.batch_path] == before


@seam
def test_a_terminal_batch_keeps_its_refusal(recovery: Recovery) -> None:
    abandoned = recovery.cli(
        "batch", "abandon", "--batch", recovery.batch, *APPROVED, "--reason", "not wanted"
    )
    assert abandoned.returncode == 0, abandoned.stderr
    before = recovery.records(recovery.batch_path)[recovery.batch_path]

    result, document = recovery.recovery("resume")

    assert result.returncode == 2
    assert document is not None
    assert {item["code"] for item in document["refusals"]} == {"terminal-refusal"}
    assert recovery.records(recovery.batch_path)[recovery.batch_path] == before
    assert _hold_record(recovery) is None


# -- a supervised worker -------------------------------------------------------------------------


def _resume_supervised(repo: Supervised) -> tuple[int, Document]:
    result = headless(repo.repo, repo.runtime, "recovery", "resume", "--batch", repo.batch)
    return result.returncode, json.loads(result.stdout) if result.stdout else {}


@linux
def test_a_live_worker_is_reconciled_and_no_second_one_is_started(
    supervised: Supervised,
) -> None:
    sent = supervised.send()
    assert sent.returncode == 0, sent.stderr
    supervised.wait_execution("running")

    code, document = _resume_supervised(supervised)

    assert code == 0, document
    assert document["steps"] == []
    assert document["next"]["command"] == f"dispatch wait --dispatch {supervised.dispatch}"
    assert len(supervised.launches()) == 1


@linux
def test_an_ambiguous_hand_off_is_refused_and_never_started_again(
    supervised: Supervised,
) -> None:
    sent = supervised.send()
    assert sent.returncode == 0, sent.stderr
    supervised.wait_launches(1)
    supervised.restart_runtime({"HARNESS_RUNTIME_TEST_BOOT_ID": "elsewhere"})
    supervised.wait_execution("unknown")

    code, document = _resume_supervised(supervised)

    assert code == 2
    assert {item["code"] for item in document["refusals"]} == {"ambiguous-effect"}
    assert len(supervised.launches()) == 1


@linux
def test_an_exited_worker_gets_its_reaction(supervised: Supervised) -> None:
    sent = supervised.send()
    assert sent.returncode == 0, sent.stderr
    supervised.wait_execution("running")
    supervised.release(0)
    supervised.wait_execution("exited")

    code, document = _resume_supervised(supervised)

    assert code == 0, document
    assert [step["step"] for step in document["steps"]] == ["reconcile"]
    assert supervised.status()["state"] == "blocked"
    assert len(supervised.launches()) == 1
