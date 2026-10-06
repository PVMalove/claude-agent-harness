"""GitHub CI adapter of issue #535 over a controlled runner: no network, no secrets."""

from __future__ import annotations

import json
import unittest
from collections.abc import Sequence

from harness.orchestration.core.ci_source import GitHubCiSource

CANDIDATE, TARGET, MERGE = "a" * 40, "b" * 40, "c" * 40
REPO = "owner/repo"
PR_PATH = f"repos/{REPO}/pulls/7"
MERGE_RUNS = f"repos/{REPO}/commits/{MERGE}/check-runs?per_page=100"
HEAD_RUNS = f"repos/{REPO}/commits/{CANDIDATE}/check-runs?per_page=100"


def check(
    name: str, sha: str, rid: int, conclusion: str | None = "success"
) -> dict[str, object]:
    return {
        "name": name,
        "head_sha": sha,
        "status": "completed" if conclusion else "queued",
        "conclusion": conclusion,
        "id": rid,
        "html_url": f"https://github.com/{REPO}/runs/{rid}",
    }


class FakeGh:
    def __init__(self, **overrides: object) -> None:
        pr = {
            "number": 7,
            "state": "open",
            "mergeable": True,
            "merge_commit_sha": MERGE,
            "head": {"sha": CANDIDATE},
            "base": {"ref": "integration/x", "repo": {"full_name": REPO}},
        }
        self.responses: dict[str, object] = {
            PR_PATH: pr,
            f"repos/{REPO}/commits/{MERGE}": {
                "sha": MERGE,
                "parents": [{"sha": CANDIDATE}, {"sha": TARGET}],
            },
            MERGE_RUNS: {"total_count": 1, "check_runs": [check("lint", MERGE, 10)]},
            HEAD_RUNS: {"total_count": 1, "check_runs": [check("lint", CANDIDATE, 9)]},
        }
        self.responses.update({k.replace("__", "/"): v for k, v in overrides.items()})
        self.calls: list[Sequence[str]] = []

    def __call__(self, arguments: Sequence[str]) -> str:
        self.calls.append(arguments)
        value = self.responses[arguments[-1]]
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, str) else json.dumps(value)


def source(gh: FakeGh) -> GitHubCiSource:
    return GitHubCiSource(gh)


class GitHubCiSourceTests(unittest.TestCase):
    def test_normalizes_the_combined_result(self) -> None:
        gh = FakeGh()
        obs = source(gh).observe(REPO, 7)
        self.assertIsNone(obs.error)
        self.assertEqual(obs.checkout, "combined")
        self.assertEqual(obs.merge_parents, (CANDIDATE, TARGET))
        self.assertEqual(
            {(c.head_sha, c.run_id) for c in obs.checks},
            {(MERGE, "10"), (CANDIDATE, "9")},
        )
        self.assertTrue(all(call[0] == "api" and "GET" in call for call in gh.calls))

    def test_unconfirmed_merge_commit_is_unknown_checkout(self) -> None:
        for patch in (
            {"mergeable": None},
            {"mergeable": False},
            {"merge_commit_sha": None},
            {"state": "closed"},
            {"merge_commit_sha": "nonsense"},
        ):
            gh = FakeGh()
            pr = dict(gh.responses[PR_PATH])  # type: ignore[call-overload]
            pr.update(patch)
            gh.responses[PR_PATH] = pr
            obs = source(gh).observe(REPO, 7)
            self.assertEqual(obs.checkout, "unknown", patch)
            self.assertIsNone(obs.merge_commit_sha)

    def test_commit_sha_mismatch_is_unknown_checkout(self) -> None:
        gh = FakeGh()
        gh.responses[f"repos/{REPO}/commits/{MERGE}"] = {"sha": "d" * 40, "parents": []}
        self.assertEqual(source(gh).observe(REPO, 7).checkout, "unknown")

    def test_runner_failure_is_unavailable_without_leaking_detail(self) -> None:
        gh = FakeGh()
        gh.responses[PR_PATH] = RuntimeError("token ghp_SECRET")
        obs = source(gh).observe(REPO, 7)
        self.assertIsNotNone(obs.error)
        self.assertNotIn("SECRET", obs.error or "")

    def test_garbage_and_truncated_answers_are_unavailable(self) -> None:
        gh = FakeGh()
        gh.responses[PR_PATH] = "<html>"
        self.assertIsNotNone(source(gh).observe(REPO, 7).error)
        gh = FakeGh()
        gh.responses[MERGE_RUNS] = {
            "total_count": 150,
            "check_runs": [check("lint", MERGE, 10)],
        }
        self.assertIsNotNone(source(gh).observe(REPO, 7).error)

    def test_unsafe_run_url_is_dropped(self) -> None:
        bad = check("lint", MERGE, 10)
        bad["html_url"] = "https://user:token@evil.example/x?y"
        gh = FakeGh()
        gh.responses[MERGE_RUNS] = {"total_count": 1, "check_runs": [bad]}
        obs = source(gh).observe(REPO, 7)
        self.assertTrue(all(c.url is None for c in obs.checks if c.run_id == "10"))


if __name__ == "__main__":
    unittest.main()
