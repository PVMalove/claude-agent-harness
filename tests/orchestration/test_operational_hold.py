"""Operational holds: a batch suspended in the runtime state store, never in its lifecycle."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.orchestration import operational_hold
from harness.orchestration.core.runtime_state_contract import QUARANTINE_STEP
from harness.orchestration.core.system_fields import load_registry
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.ledger.lifecycle import LifecycleLedger
from harness.orchestration.runtime_intent import first_record, next_revision, write_next
from harness.orchestration.runtime_reconcile import settle
from harness.orchestration.runtime_state_store import (
    RuntimeStateError,
    RuntimeStateStore,
)
from tests.orchestration.test_coordinator import _git

BATCH = "batch-0123456789abcdef"


@pytest.fixture
def store(tmp_path: Path) -> RuntimeStateStore:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    return RuntimeStateStore(repo)


def _record(store: RuntimeStateStore) -> JsonObject:
    record = store.read(BATCH)
    assert record is not None
    return record


def test_the_reasons_are_the_registry_values() -> None:
    assert (
        operational_hold.HOLD_REASONS
        == load_registry().fields["operational_hold.reason"].allowed
    )


def test_a_hold_suspends_the_store_record_and_names_its_reason(
    store: RuntimeStateStore,
) -> None:
    held = operational_hold.hold(
        store, BATCH, "runtime-compatibility", "adapter declares systemFields.v2"
    )

    record = _record(store)
    assert record["state"] == "suspended"
    assert record["failure"] == {
        "code": "runtime-compatibility",
        "step": "hold:runtime-compatibility",
        "message": "adapter declares systemFields.v2",
    }
    assert held == operational_hold.current_hold(store, BATCH)
    assert held is not None and held.releasable
    assert held.to_dict()["reason"] == "runtime-compatibility"


def test_a_repeated_hold_adds_no_revision(store: RuntimeStateStore) -> None:
    operational_hold.hold(store, BATCH, "unknown-value", "x")
    revision = _record(store)["revision"]

    operational_hold.hold(store, BATCH, "unknown-value", "x")

    assert _record(store)["revision"] == revision


def test_a_quarantine_is_never_overwritten_or_released(
    store: RuntimeStateStore,
) -> None:
    record = first_record(store, BATCH)
    failure: JsonObject = {
        "code": "integrity-failure",
        "step": QUARANTINE_STEP,
        "message": "quarantined",
    }
    write_next(store, record, next_revision(record, state="suspended", failure=failure))

    held = operational_hold.hold(store, BATCH, "runtime-compatibility", "x")

    assert held is not None
    assert (held.reason, held.releasable) == ("legacy-quarantine", False)
    assert _record(store)["failure"] == failure
    with pytest.raises(CoordinatorError, match="not released"):
        operational_hold.release(store, BATCH)


def test_release_returns_an_idle_record_to_ready(store: RuntimeStateStore) -> None:
    operational_hold.hold(store, BATCH, "runtime-compatibility", "x")

    operational_hold.release(store, BATCH)

    record = _record(store)
    assert (record["state"], record["failure"]) == ("ready", None)
    assert operational_hold.current_hold(store, BATCH) is None


def test_an_unreadable_store_record_is_a_hold_that_blocks_actions(
    store: RuntimeStateStore,
) -> None:
    first_record(store, BATCH)
    store.path(BATCH).write_text("{", encoding="utf-8")

    held = operational_hold.current_hold(store, BATCH)

    assert held is not None
    assert (held.reason, held.revision, held.releasable) == (
        "unreadable-record",
        None,
        False,
    )
    with pytest.raises(RuntimeStateError):
        operational_hold.require_no_hold(store, BATCH)


def test_no_hold_without_a_record_or_after_a_plain_suspension(
    store: RuntimeStateStore,
) -> None:
    assert operational_hold.current_hold(store, BATCH) is None
    record = first_record(store, BATCH)
    running = next_revision(record, state="running")
    write_next(store, record, running)
    write_next(
        store,
        running,
        next_revision(
            running,
            state="suspended",
            failure={"code": "execution-failure", "step": "intent", "message": "x"},
        ),
    )

    assert operational_hold.current_hold(store, BATCH) is None
    operational_hold.require_no_hold(store, BATCH)


def test_settle_stops_on_a_hold_instead_of_releasing_it(
    store: RuntimeStateStore, tmp_path: Path
) -> None:
    operational_hold.hold(store, BATCH, "runtime-compatibility", "adapter is v2")
    before = _record(store)
    root = store.location.state_root
    ledger = LifecycleLedger(root)

    with pytest.raises(RuntimeStateError) as raised:
        settle(store.location.repo_root, root, ledger, BATCH)

    assert raised.value.code == "runtime-compatibility"
    assert "recovery resume" in raised.value.remedy
    assert _record(store) == before


def test_a_completed_record_is_never_held(store: RuntimeStateStore) -> None:
    record = first_record(store, BATCH)
    running = next_revision(record, state="running")
    write_next(store, record, running)
    write_next(store, running, next_revision(running, state="completed"))

    assert operational_hold.hold(store, BATCH, "unknown-value", "x") is None
    assert _record(store)["state"] == "completed"
