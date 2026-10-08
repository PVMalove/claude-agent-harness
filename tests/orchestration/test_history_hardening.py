"""Hardening tests for the batch-entry lookups of ``history``."""

from __future__ import annotations

import pytest

from harness.orchestration.core.utils import JsonObject
from harness.orchestration.workflow import history

ENTRIES: list[JsonObject] = [
    {"dispatch_id": "dispatch-1", "decision": {"decision": "accept"}},
    {"dispatch_id": "dispatch-2", "decision": {"decision": "retry"}},
    {"dispatch_id": "dispatch-3", "state": "dispatched"},
]


@pytest.mark.parametrize(
    ("dispatch_id", "expected"),
    [("dispatch-2", ENTRIES[1]), ("dispatch-3", ENTRIES[2]), ("dispatch-9", None)],
)
def test_dispatch_entry_finds_the_batch_entry_of_a_dispatch(
    dispatch_id: str, expected: JsonObject | None
) -> None:
    assert history._dispatch_entry({"dispatches": ENTRIES}, dispatch_id) is expected


@pytest.mark.parametrize(
    ("entries", "expected"),
    [(ENTRIES, ENTRIES[1]), (ENTRIES[2:], None), ([], None)],
)
def test_last_decided_entry_skips_undecided_dispatches(
    entries: list[JsonObject], expected: JsonObject | None
) -> None:
    assert history._last_decided_entry({"dispatches": entries}) is expected


def test_lookups_of_a_batch_without_dispatches_find_nothing() -> None:
    assert history._dispatch_entry({}, "dispatch-1") is None
    assert history._last_decided_entry({}) is None
