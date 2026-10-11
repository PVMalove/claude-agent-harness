"""Conditional acceptance leaves an open condition that only an independent gate closes."""

from __future__ import annotations

import pytest

from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.workflow import conditions


def _batch(*stages: str) -> JsonObject:
    batch: JsonObject = {
        "dispatches": [{"dispatch_id": f"dispatch-{i}"} for i, _ in enumerate(stages)]
    }
    for index, stage in enumerate(stages):
        conditions.record(
            batch,
            dispatch_id=f"dispatch-{index}",
            stage=stage,
            candidate=None,
            approver="Malove",
            moment="2026-10-11T00:00:00+00:00",
            note="runtime restored",
        )
    return batch


def test_publish_and_completion_refuse_while_a_condition_is_open() -> None:
    batch = _batch("developer")
    with pytest.raises(CoordinatorError, match="conditionally accepted"):
        conditions.require_closed(batch)


@pytest.mark.parametrize(
    ("stage", "closing", "closed"),
    [
        ("architect", "code-review", True),
        ("developer", "code-review", True),
        ("developer", "qa", True),
        ("code-review", "code-review", False),
        ("code-review", "qa", True),
        ("qa", "code-review", False),
        ("qa", "qa", True),
        ("publish", "qa", False),
    ],
)
def test_only_a_later_independent_gate_closes_a_condition(
    stage: str, closing: str, closed: bool
) -> None:
    batch = _batch(stage)
    batch["dispatches"].append({"dispatch_id": "dispatch-later"})
    conditions.close_covered(
        batch, closing, "dispatch-later", "2026-10-11T01:00:00+00:00"
    )
    assert (batch["conditions"][0]["status"] == "closed") is closed
    if closed:
        assert batch["conditions"][0]["closed_by"] == "dispatch-later"
        conditions.require_closed(batch)


def test_a_condition_never_closes_itself() -> None:
    batch = _batch("qa")
    conditions.close_covered(batch, "qa", "dispatch-0", "2026-10-11T01:00:00+00:00")
    assert conditions.open_conditions(batch)


def test_a_condition_closed_by_independent_evidence_is_not_open() -> None:
    batch: JsonObject = {"dispatches": [{"dispatch_id": "dispatch-0"}]}
    conditions.record(
        batch,
        dispatch_id="dispatch-0",
        stage="publish",
        candidate="abc",
        approver="Malove",
        moment="2026-10-11T00:00:00+00:00",
        note="pushed",
        closed_by="remote-branch-tip",
    )
    assert conditions.open_conditions(batch) == []
    conditions.require_closed(batch)
