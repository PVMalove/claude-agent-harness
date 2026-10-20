"""The heuristic names an infrastructure cause only when no code-defect sign contradicts it."""

from __future__ import annotations

from pathlib import Path

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


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("provider is at capacity", "capacity"),
        ("capacity exhausted for this model", "capacity"),
        ("service unavailable (HTTP 503)", "service-unavailable"),
        ("HTTP 429 from the provider", "rate-limit"),
    ],
)
def test_startup_infrastructure_categories_stay_separate(
    text: str, category: str
) -> None:
    result = failure_classifier.classify(text)
    assert result is not None
    assert result["category"] == category


@pytest.mark.parametrize(
    ("failure", "category"),
    [
        ({"kind": "capacity", "worker_started": False}, "capacity"),
        ({"kind": "rate_limit", "worker_started": False}, "rate-limit"),
        ({"status_code": 503, "worker_started": False}, "service-unavailable"),
        (
            {"kind": "runtime-unavailable", "worker_started": False},
            "runtime-unavailable",
        ),
    ],
)
def test_structured_runtime_categories_are_explicit(
    failure: dict[str, object], category: str
) -> None:
    result = failure_classifier.classify_runtime_failure(failure)
    assert result is not None
    assert result["method"] == "structured"
    assert result["category"] == category


@pytest.mark.parametrize(
    "text",
    [
        "the capacity error came after tests failed",
        "capacity of the batch is unclear: requirements are ambiguous",
    ],
)
def test_a_code_defect_sign_wins_over_a_capacity_sign(text: str) -> None:
    assert failure_classifier.classify(text) is None


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("HTTP/1.1 503 from the gateway", "service-unavailable"),
        ("status code: 503", "service-unavailable"),
        ("gateway answered 502 Bad Gateway", "service"),
    ],
)
def test_http_statuses_are_classified(text: str, category: str) -> None:
    result = failure_classifier.classify(text)
    assert result is not None
    assert result["category"] == category


REPO = Path("/work/repo")
READ_ONLY_TAIL = """\
FAILED tests/health/test_isolated_runner_session.py::test_run_success - OSError: [Errno 30] Read-only file system: '/tmp/harness-db-ab12'
FAILED tests/health/test_isolated_runner_session.py::test_run_fails_on_os_error - OSError: [Errno 30] Read-only file system: '/tmp/harness-db-cd34'
4 failed, 3553 passed in 210.52s
[verify] pytest: failed in 212.10s
make: *** [Makefile:41: verify] Error 1"""


@pytest.mark.parametrize(
    ("text", "category"),
    [
        (READ_ONLY_TAIL, "read-only-filesystem"),
        (
            "FAILED tests/x.py::t - PermissionError: [Errno 13] Permission denied: '/var/cache/x'",
            "permission-denied",
        ),
    ],
)
def test_an_environment_refusal_is_classified(text: str, category: str) -> None:
    result = failure_classifier.classify_environment(text, REPO)
    assert result is not None
    assert result["method"] == "heuristic" and result["category"] == category


@pytest.mark.parametrize(
    "text",
    [
        "",
        None,
        # Inside the repository the code itself may have made the path unwritable.
        "FAILED tests/x.py::t - PermissionError: [Errno 13] Permission denied: '/work/repo/out'",
        "FAILED tests/x.py::t - PermissionError: [Errno 13] Permission denied: 'out/data'",
        # A real failure next to the environment one keeps the developer retry.
        READ_ONLY_TAIL + "\nE   AssertionError: expected 2",
        READ_ONLY_TAIL.replace(
            "OSError: [Errno 30] Read-only file system: '/tmp/harness-db-cd34'",
            "ValueError: bad input",
        ),
        "1 failed, 10 passed in 2.00s",
    ],
)
def test_a_possible_code_failure_is_not_an_environment_one(text: object) -> None:
    assert failure_classifier.classify_environment(text, REPO) is None
