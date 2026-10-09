"""Hardening tests for the coordinator's leaf helpers (`core/utils.py`)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from harness.orchestration.core.utils import CoordinatorError, _moment, _read_object


def test_a_non_utf8_payload_is_a_coordinator_error(tmp_path: Path) -> None:
    payload = tmp_path / "report.json"
    # A role on a legacy Windows code page writes Cyrillic text as cp1251, not UTF-8.
    payload.write_bytes('{"summary": "отчёт"}'.encode("cp1251"))

    with pytest.raises(CoordinatorError) as caught:
        _read_object(payload, "--file payload")

    assert caught.value.message == "--file payload is not valid JSON"
    assert caught.value.remedy == "fix the JSON syntax in --file payload"


@pytest.mark.parametrize("value", [None, 1_700_000_000, ["2026-10-08T00:00:00+00:00"]])
def test_a_non_string_moment_is_not_a_readable_timestamp(value: object) -> None:
    with pytest.raises(CoordinatorError) as caught:
        _moment(value, "approved_at")

    assert caught.value.message == "approved_at is not a readable timestamp"
    assert caught.value.remedy == "pass approved_at as an ISO-8601 timestamp"


def test_a_moment_keeps_its_timezone() -> None:
    assert _moment("2026-10-08T01:02:03+00:00", "approved_at") == datetime(
        2026, 10, 8, 1, 2, 3, tzinfo=UTC
    )
