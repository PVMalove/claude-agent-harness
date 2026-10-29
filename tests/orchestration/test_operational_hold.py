"""Operational holds: a batch held by its recovery record in the runtime store (ADR 0026)."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.orchestration import operational_hold
from harness.orchestration.core.system_fields import load_registry
from harness.orchestration.core.utils import CoordinatorError, JsonObject

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
