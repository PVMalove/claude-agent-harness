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
