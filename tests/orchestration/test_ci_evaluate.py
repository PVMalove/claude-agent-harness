"""Pure CI verdict of issue #535: only the combined result of the exact pair is accepted."""

from __future__ import annotations

import dataclasses
import unittest

from harness.orchestration.core import ci_source
from harness.orchestration.core.ci_source import CheckRun, CiObservation, evaluate

CANDIDATE = "a" * 40
TARGET = "b" * 40
MERGE = "c" * 40
REPO = "owner/repo"


def run(
    name: str = "lint",
    sha: str = MERGE,
    status: str = "completed",
    conclusion: str | None = "success",
    run_id: str = "10",
) -> CheckRun:
    return CheckRun(
        name, sha, status, conclusion, run_id, f"https://github.com/{REPO}/runs/{run_id}"
    )


def observation(**changes: object) -> CiObservation:
    base = CiObservation(
        repository=REPO,
        pull_request=7,
        base_ref="integration/x",
        candidate_sha=CANDIDATE,
        checkout="combined",
        merge_commit_sha=MERGE,
        merge_parents=(CANDIDATE, TARGET),
        checks=(run("lint"), run("tests", run_id="11")),
    )
    return dataclasses.replace(base, **changes)  # type: ignore[arg-type]


def verdict(obs: CiObservation, **changes: object) -> ci_source.CiVerdict:
    args: dict[str, object] = dict(
        repository=REPO,
        pull_request=7,
        candidate_sha=CANDIDATE,
        target_sha=TARGET,
        required_checks=["lint", "tests"],
        base_ref="integration/x",
    )
    args.update(changes)
    return evaluate(obs, **args)  # type: ignore[arg-type]


class EvaluateTests(unittest.TestCase):
    def assert_fallback(self, result: ci_source.CiVerdict, reason: str) -> None:
        self.assertEqual((result.outcome, result.reason), ("fallback", reason))

    def test_accepts_the_full_set_on_the_combined_result(self) -> None:
        result = verdict(observation())
        self.assertEqual(result.outcome, "accepted")
        assert result.verified is not None
        self.assertEqual(result.verified["merge_commit_sha"], MERGE)
        self.assertEqual(result.verified["target_sha"], TARGET)
        self.assertEqual(
            [c["name"] for c in result.verified["checks"]], ["lint", "tests"]
        )

    def test_not_configured(self) -> None:
        self.assert_fallback(
            verdict(observation(), required_checks=[]), "not_configured"
        )

    def test_unavailable_is_fallback_never_a_failure(self) -> None:
        self.assert_fallback(verdict(observation(error="down")), "unavailable")

    def test_other_repository_or_pull_request_or_base(self) -> None:
        self.assert_fallback(verdict(observation(repository="x/y")), "wrong_repository")
        self.assert_fallback(
            verdict(observation(pull_request=8)), "wrong_pull_request"
        )
        self.assert_fallback(verdict(observation(base_ref="master")), "wrong_base")

    def test_stale_candidate_and_target(self) -> None:
        self.assert_fallback(
            verdict(observation(), candidate_sha="d" * 40), "stale_candidate"
        )
        self.assert_fallback(
            verdict(observation(), target_sha="d" * 40), "stale_target"
        )
        self.assert_fallback(
            verdict(observation(merge_parents=("d" * 40, TARGET))), "stale_candidate"
        )
        self.assert_fallback(
            verdict(observation(merge_parents=(CANDIDATE, TARGET, "d" * 40))),
            "stale_target",
        )

    def test_unknown_checkout(self) -> None:
        self.assert_fallback(
            verdict(observation(checkout="unknown")), "unknown_checkout"
        )
        self.assert_fallback(
            verdict(observation(merge_commit_sha=None)), "unknown_checkout"
        )
        self.assert_fallback(verdict(observation(merge_parents=())), "unknown_checkout")

    def test_head_only_pipeline(self) -> None:
        obs = observation(checks=(run("lint", sha=CANDIDATE), run("tests")))
        self.assert_fallback(verdict(obs), "head_only")

    def test_missing_pending_inconclusive_checks(self) -> None:
        self.assert_fallback(
            verdict(observation(checks=(run("lint"),))), "missing_check"
        )
        pending = observation(
            checks=(run("lint"), run("tests", status="in_progress", conclusion=None))
        )
        self.assert_fallback(verdict(pending), "pending_check")
        for conclusion in (
            "cancelled",
            "timed_out",
            "skipped",
            "neutral",
            "startup_failure",
            None,
        ):
            obs = observation(checks=(run("lint"), run("tests", conclusion=conclusion)))
            self.assert_fallback(verdict(obs), "inconclusive_check")

    def test_failed_check_on_the_verified_pair_is_failed(self) -> None:
        obs = observation(checks=(run("lint"), run("tests", conclusion="failure")))
        result = verdict(obs)
        self.assertEqual((result.outcome, result.reason), ("failed", "failed_check"))

    def test_latest_run_of_a_check_wins(self) -> None:
        obs = observation(
            checks=(
                run("lint", conclusion="failure", run_id="9"),
                run("lint", run_id="12"),
                run("tests", run_id="11"),
            )
        )
        self.assertEqual(verdict(obs).outcome, "accepted")

    def test_failure_on_a_wrong_pair_is_never_a_finding(self) -> None:
        obs = observation(checks=(run("lint"), run("tests", conclusion="failure")))
        self.assert_fallback(verdict(obs, target_sha="d" * 40), "stale_target")


if __name__ == "__main__":
    unittest.main()
