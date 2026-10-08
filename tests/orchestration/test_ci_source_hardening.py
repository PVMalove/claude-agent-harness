"""Hardening tests for the CI evidence verdict (`core/ci_source.py`)."""

from __future__ import annotations

import pytest

from harness.orchestration.core.ci_source import CheckRun, CiObservation, evaluate

CANDIDATE = "a" * 40
TARGET = "b" * 40
MERGE = "c" * 40
REPO = "owner/repo"


def _observation(candidate: str, parents: tuple[str, ...]) -> CiObservation:
    return CiObservation(
        repository=REPO,
        pull_request=7,
        base_ref="integration/x",
        candidate_sha=candidate,
        checkout="combined",
        merge_commit_sha=MERGE,
        merge_parents=parents,
        checks=(
            CheckRun("lint", MERGE, "completed", "success", "10", None),
            CheckRun("tests", MERGE, "completed", "failure", "11", None),
        ),
    )


@pytest.mark.parametrize(
    ("candidate", "target", "parents", "required", "outcome"),
    [
        pytest.param(
            CANDIDATE, TARGET, (CANDIDATE, TARGET), ["lint"], "accepted", id="pair"
        ),
        pytest.param(
            CANDIDATE, TARGET, (TARGET, CANDIDATE), ["tests"], "failed", id="reversed"
        ),
        pytest.param(
            CANDIDATE,
            CANDIDATE,
            (CANDIDATE, CANDIDATE),
            ["lint"],
            "accepted",
            id="same",
        ),
    ],
)
def test_the_verified_target_is_the_evaluated_target(
    candidate: str,
    target: str,
    parents: tuple[str, ...],
    required: list[str],
    outcome: str,
) -> None:
    verdict = evaluate(
        _observation(candidate, parents),
        repository=REPO,
        pull_request=7,
        candidate_sha=candidate,
        target_sha=target,
        required_checks=required,
    )

    assert verdict.outcome == outcome
    assert verdict.verified is not None
    assert verdict.verified["target_sha"] == target
    assert verdict.verified["candidate_sha"] == candidate
