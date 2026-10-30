"""The shared golden fixture of how QA evidence links itself (``qa-evidence-reference.v1``).

The runtime operation ``qa.evidence.verify`` (``qa_evidence_verify`` of ADR 0021, section 7) follows
the chain the Python coordinator writes: the QA report names its log by ``(sha256:<hex>)`` in its
``output``, and the last line of the log names the stage-evidence document. The Rust tests parse the
fixture; these tests prove the Python writers still produce exactly its texts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.gate_runner.gate_runner import (
    format_evidence_trailer,
    parse_evidence_trailer,
)
from harness.orchestration import qa_lane
from harness.orchestration.core.utils import JsonObject

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "native/harness-runtime/crates/contracts/tests/fixtures/qa-evidence-reference.v1.json"
)


def _fixture() -> JsonObject:
    loaded: JsonObject = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return loaded


def _written_outputs() -> list[JsonObject]:
    return [case for case in _fixture()["report_outputs"] if "artifact" in case]


def test_the_fixture_names_its_contract() -> None:
    fixture = _fixture()

    assert fixture["contract"] == "qa-evidence-reference.v1"
    assert _written_outputs()
    assert any(case["trailer"] for case in fixture["logs"])


@pytest.mark.parametrize("case", _written_outputs(), ids=lambda case: case["case"])
def test_the_qa_report_links_its_log_exactly_as_the_fixture_says(
    case: JsonObject,
) -> None:
    dispatch = {
        "dispatch_id": "dispatch-1",
        "ticket": "#1",
        "verification_commands": ["make lint"],
    }
    checks = [{"command": "make lint", "result": case["check_result"], "evidence": "e"}]

    report = qa_lane._qa_report(
        dispatch, checks, Path(case["artifact"]), case["checksum"]
    )

    assert report["output"] == case["output"]
    assert case["log_sha256"] == case["checksum"]


@pytest.mark.parametrize("case", _fixture()["logs"], ids=lambda case: case["case"])
def test_the_log_trailer_is_written_and_read_as_the_fixture_says(
    case: JsonObject,
) -> None:
    trailer = case["trailer"]
    lines = case["log"].splitlines()

    if trailer is None:
        assert parse_evidence_trailer(lines) is None
        return
    assert case["log"].endswith(
        format_evidence_trailer(trailer["path"], trailer["sha256"])
    )
    assert parse_evidence_trailer(lines) == (trailer["path"], trailer["sha256"])
