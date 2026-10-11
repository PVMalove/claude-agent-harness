"""The heuristic names an infrastructure cause only when no code-defect sign contradicts it."""

from __future__ import annotations

import pytest

from harness.orchestration.workflow import failure_classifier


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("HTTP 429 from the provider", "rate-limit"),
        ("worker session crashed: connection reset by peer", "network"),
        ("command timed out after 600s", "timeout"),
        ("runtime process died unexpectedly", "runtime-crash"),
        ("No space left on device", "resources"),
        ("docker daemon is not running", "service"),
    ],
)
def test_infrastructure_text_is_classified(text: str, category: str) -> None:
    result = failure_classifier.classify(text)
    assert result is not None
    assert result["method"] == "heuristic" and result["category"] == category


@pytest.mark.parametrize(
    "text",
    [
        "",
        None,
        "unit tests failed: assertion error in test_x, then connection reset",
        "mypy reports 3 errors; the runtime timed out afterwards",
        "merge conflict in services/x.py",
        "requirements are ambiguous",
        "something unexpected happened",
    ],
)
def test_unknown_or_code_text_is_not_classified(text: object) -> None:
    assert failure_classifier.classify(text) is None


def test_every_source_text_is_considered_but_only_strings() -> None:
    assert failure_classifier.classify(None, 5, "rate limit exceeded") is not None
