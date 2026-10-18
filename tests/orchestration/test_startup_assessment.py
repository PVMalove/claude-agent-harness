"""A stopped startup is explained from recorded facts; only a known worker outcome allows a restart."""

from __future__ import annotations

import pytest

from harness.orchestration.workflow.startup import assess_startup, worker_process_state

DISPATCH = {
    "role": "developer",
    "resolved_model": "sonnet",
    "worktree": "/tmp/worker",
    "worker_attestation_required": True,
}
MATCHED = {
    "model_self_report": {"reported_model": "sonnet", "match": True},
    "worktree_attestation": {"match": True},
}


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ({"state": "working", **MATCHED}, "live"),
        ({"state": "dispatched"}, "live"),
        (
            {
                "state": "blocked",
                "runtime_failure": {
                    "kind": "runtime-unavailable",
                    "worker_started": False,
                },
            },
            "absent",
        ),
        ({"state": "reported", **MATCHED}, "reported"),
        (
            {"state": "blocked", "model_self_report": {"match": False}},
            "rejected-at-startup",
        ),
        ({"state": "rate_limited"}, "unknown"),
    ],
)
def test_worker_process_state_is_the_strongest_recorded_fact(
    status: dict[str, object], state: str
) -> None:
    assert worker_process_state(status) == state


def test_the_operator_stop_proof_never_overrides_a_live_dispatch() -> None:
    assert worker_process_state({"state": "working"}, runtime_stopped=True) == "live"
    assert (
        worker_process_state(
            {"state": "blocked", "worktree_attestation": {"match": False}},
            runtime_stopped=True,
        )
        == "stopped"
    )


def test_a_refused_self_report_keeps_the_existing_human_approved_restart() -> None:
    status = {"state": "blocked", "model_self_report": {"match": False}}

    refused = assess_startup(DISPATCH, status)
    stopped = assess_startup(DISPATCH, status, runtime_stopped=True)

    assert refused["classification"] == "model-mismatch"
    assert refused["worker_state"] == "rejected-at-startup"
    assert refused["restart_allowed"] is True
    assert (
        "worker termination is not proven (rejected-at-startup)" in refused["unknowns"]
    )
    assert stopped["worker_state"] == "stopped"
    assert stopped["unknowns"] == []


def test_a_missing_self_report_stays_fail_closed_while_the_worker_may_run() -> None:
    assessment = assess_startup(DISPATCH, {"state": "dispatched"})

    assert assessment["classification"] == "model-self-report-missing"
    assert assessment["worker_state"] == "live"
    assert assessment["restart_allowed"] is False
    assert "the active model identity is not known" in assessment["unknowns"]


def test_a_launch_failure_names_its_infrastructure_cause_before_the_missing_report() -> (
    None
):
    assessment = assess_startup(
        DISPATCH,
        {
            "state": "blocked",
            "runtime_failure": {
                "kind": "runtime-unavailable",
                "worker_started": False,
                "error": "provider is at capacity",
            },
        },
    )

    assert assessment["classification"] == "runtime-unavailable"
    assert assessment["worker_state"] == "absent"
    assert assessment["restart_allowed"] is True


def test_capacity_while_the_worker_is_live_is_not_restartable() -> None:
    assessment = assess_startup(
        DISPATCH,
        {"state": "working", **MATCHED, "runtime_failure": {"kind": "capacity"}},
    )

    assert assessment["classification"] == "capacity"
    assert assessment["restart_allowed"] is False
