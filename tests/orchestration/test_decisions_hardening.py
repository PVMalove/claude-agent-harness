"""Hardening tests for the retry routing of a coordinator decision (``decisions``)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from harness.errors import INTERNAL_INVARIANT_REMEDY
from harness.orchestration.core.utils import CoordinatorError
from harness.orchestration.workflow import decisions


def test_retry_route_of_a_dispatch_outside_its_batch_is_an_invariant_error(
    tmp_path: Path,
) -> None:
    dispatch = {"dispatch_id": "dispatch-1", "role": "developer", "purpose": "work"}
    report = {"role": "developer", "outcome": "completed"}
    args = argparse.Namespace(retry_role=None, narrowed=False, reason_category="code")

    with pytest.raises(CoordinatorError) as caught:
        decisions._decide_retry_route(
            tmp_path, tmp_path, {"dispatches": []}, dispatch, report, args
        )

    assert "dispatch-1" in caught.value.message
    assert caught.value.remedy == INTERNAL_INVARIANT_REMEDY


def test_a_completed_report_never_holds_its_packet() -> None:
    preview = {"route": "developer-retry", "reason_category": "unknown"}

    assert decisions._route_hold({"outcome": "completed"}, preview) is None


@pytest.mark.parametrize(
    ("preview", "reason"),
    [
        ({"route": "architect-retry", "reason_category": "unknown"}, "unknown-value"),
        ({"route": None, "refused": "QA preparation failed"}, "missing-value"),
    ],
)
def test_an_unknown_or_missing_cause_holds_the_packet_without_an_abandon(
    preview: dict[str, object], reason: str
) -> None:
    held = decisions._route_hold({"outcome": "blocked"}, preview)

    assert held is not None
    assert (held["source"], held["reason"]) == ("retry-route", reason)
    packet: dict[str, object] = {
        "batch_id": "batch-1",
        "options": ["retry", "block", "abandon"],
        "recovery_route": {"options": ["retry", "block", "abandon"]},
    }
    decisions._hold_options(packet, held)
    assert packet["options"] == ["retry", "block"]
    assert packet["recovery_route"] == {"options": ["retry", "block"]}


def test_a_store_hold_offers_no_decision_at_all() -> None:
    packet: dict[str, object] = {
        "batch_id": "batch-1",
        "options": ["accept", "retry", "block", "abandon"],
    }

    decisions._hold_options(
        packet, {"source": "runtime-state", "reason": "unknown-value"}
    )

    assert packet["options"] == []
    route = packet["recovery_route"]
    assert isinstance(route, dict)
    assert route["commands"] == [
        "recovery status --batch batch-1",
        "recovery resume --batch batch-1",
    ]
