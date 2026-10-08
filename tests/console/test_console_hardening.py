"""Hardening tests for the textual-free console seam: runtime invariants raise HarnessError with
INTERNAL_INVARIANT_REMEDY instead of a bare assert or KeyError, and the ledger snapshot is frozen."""

from __future__ import annotations

import dataclasses

import pytest

from harness.console import repo_map as console_repo_map
from harness.console.coordinator_catalog import subparser
from harness.console.reports import LedgerView
from harness.errors import INTERNAL_INVARIANT_REMEDY, HarnessError
from harness.orchestration import coordinator


def test_ledger_view_fields_cannot_be_rebound() -> None:
    view = LedgerView(unavailable="нет леджера")

    with pytest.raises(dataclasses.FrozenInstanceError):
        view.unavailable = None  # type: ignore[misc]


@pytest.mark.parametrize("payload", [[], "map", None])
def test_parse_map_raises_an_invariant_error_for_an_accepted_non_object(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    monkeypatch.setattr(console_repo_map, "validation_error", lambda _payload: None)

    with pytest.raises(HarnessError) as caught:
        console_repo_map.parse_map(payload, origin="кэш")

    assert caught.value.remedy == INTERNAL_INVARIANT_REMEDY


def test_unknown_coordinator_subcommand_is_an_invariant_error() -> None:
    with pytest.raises(HarnessError) as caught:
        subparser(coordinator.parser(), ("batch", "no-such-command"))

    assert "batch no-such-command" in caught.value.message
    assert caught.value.remedy == INTERNAL_INVARIANT_REMEDY
