"""CI evidence port (issue #535): a normalized observation, a pure verdict and a GitHub adapter.

A ``CiSource`` turns one pull request of a hosting service into a ``CiObservation`` -- plain
facts, no decisions.  ``evaluate`` is the only place that decides: CI is accepted for a
candidate/target pair only when the check runs ran on the *combined result* of that exact pair
(the pull request merge commit whose parents are the candidate and the target), in the right
repository and pull request, and every required check passed.  Anything else is a ``fallback``
(local QA is still needed); only a completed failure on the verified pair is ``failed``.

GitHub combined-result rule (documented behavior, checked against the GitHub docs): for a
``pull_request`` workflow ``GITHUB_SHA`` is the last merge commit of ``refs/pull/<n>/merge`` and,
before the merge, the pull request's ``merge_commit_sha`` holds the test merge commit.  A check
run is therefore a combined-result check only when its ``head_sha`` equals ``merge_commit_sha``
and that commit's parents are exactly ``{candidate, target}``.  A check run on the head SHA alone
is ``head_only``; a missing, stale or unreadable merge commit is ``unknown_checkout``.

The adapter is read-only (``gh api`` GET calls through an injectable runner), never opens or
merges a pull request and never changes branch protection.  Nothing it returns carries a token,
a response body or a stderr dump.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from harness.orchestration.core.utils import JsonObject

Checkout = Literal["combined", "unknown"]
Outcome = Literal["accepted", "failed", "fallback"]

# Reasons of a ``fallback`` or ``failed`` verdict; ``accepted`` carries ``verified``.
REASONS = (
    "not_configured",
    "unavailable",
    "wrong_repository",
    "wrong_pull_request",
    "wrong_base",
    "stale_candidate",
    "stale_target",
    "unknown_checkout",
    "head_only",
    "missing_check",
    "pending_check",
    "inconclusive_check",
    "failed_check",
)

_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_SAFE_REASON = re.compile(r"[^A-Za-z0-9 ._:/#()-]")
_SAFE_URL = re.compile(r"https://[A-Za-z0-9.:-]+/[A-Za-z0-9_./-]+")
_MAX_CHECK_RUNS = 100


@dataclass(frozen=True)
class CheckRun:
    """One check run, normalized: which commit it ran on and how it ended."""

    name: str
    head_sha: str
    status: str  # queued | in_progress | completed
    conclusion: str | None
    run_id: str
    url: str | None


@dataclass(frozen=True)
class CiObservation:
    """What the CI service reported for one pull request; never a decision."""

    repository: str
    pull_request: int
    base_ref: str
    candidate_sha: str  # the pull request head
    checkout: Checkout
    merge_commit_sha: str | None
    merge_parents: tuple[str, ...]
    checks: tuple[CheckRun, ...]
    source: str = "github"
    error: str | None = None  # set -> the service could not answer; all else is empty


@dataclass(frozen=True)
class CiVerdict:
    outcome: Outcome
    reason: str | None
    detail: str
    verified: JsonObject | None = None


class CiSource(Protocol):
    def observe(self, repository: str, pull_request: int) -> CiObservation: ...


Runner = Callable[[Sequence[str]], str]


def _fallback(reason: str, detail: str) -> CiVerdict:
    return CiVerdict("fallback", reason, detail)


def _latest(runs: list[CheckRun]) -> CheckRun:
    return max(runs, key=lambda run: (len(run.run_id), run.run_id))


def evaluate(
    observation: CiObservation,
    *,
    repository: str,
    pull_request: int,
    candidate_sha: str,
    target_sha: str,
    required_checks: Sequence[str],
    base_ref: str | None = None,
) -> CiVerdict:
    """Decide whether ``observation`` is CI evidence for exactly this candidate/target pair."""
    if not required_checks:
        return _fallback(
            "not_configured",
            "project.json ci_required_checks is empty: CI evidence is not configured",
        )
    if observation.error is not None:
        return _fallback("unavailable", observation.error)
    if observation.repository.lower() != repository.lower():
        return _fallback("wrong_repository", "the observation is of another repository")
    if observation.pull_request != pull_request:
        return _fallback(
            "wrong_pull_request", "the observation is of another pull request"
        )
    if base_ref is not None and observation.base_ref != base_ref:
        return _fallback("wrong_base", "the pull request targets another branch")
    if observation.candidate_sha != candidate_sha:
        return _fallback(
            "stale_candidate", "the pull request head is not the linked candidate"
        )
    merge = observation.merge_commit_sha
    if observation.checkout != "combined" or not merge or not observation.merge_parents:
        return _fallback(
            "unknown_checkout",
            "the combined-result checkout of the pull request cannot be confirmed",
        )
    parents = set(observation.merge_parents)
    if candidate_sha not in parents:
        return _fallback(
            "stale_candidate", "the merge result does not contain the linked candidate"
        )
    if parents != {candidate_sha, target_sha} or len(observation.merge_parents) != 2:
        return _fallback(
            "stale_target", "the merge result was not built on the linked target"
        )
    selected: list[CheckRun] = []
    for name in required_checks:
        on_merge = [
            run
            for run in observation.checks
            if run.name == name and run.head_sha == merge
        ]
        if not on_merge:
            on_head = any(
                run.name == name and run.head_sha == candidate_sha
                for run in observation.checks
            )
            if on_head:
                return _fallback(
                    "head_only",
                    f"required check {name!r} ran on the head commit, not the combined result",
                )
            return _fallback("missing_check", f"required check {name!r} has no run")
        selected.append(_latest(on_merge))
    for run in selected:
        if run.status != "completed":
            return _fallback(
                "pending_check", f"required check {run.name!r} is not finished"
            )
    for run in selected:
        if run.conclusion == "failure":
            return CiVerdict(
                "failed",
                "failed_check",
                f"required check {run.name!r} failed on the combined result",
                _verified(observation, selected),
            )
    for run in selected:
        if run.conclusion != "success":
            return _fallback(
                "inconclusive_check",
                f"required check {run.name!r} ended as {run.conclusion or 'unknown'}, "
                "which is not a verdict on the code",
            )
    return CiVerdict(
        "accepted", None, "all required checks passed", _verified(observation, selected)
    )


def _verified(observation: CiObservation, selected: list[CheckRun]) -> JsonObject:
    return {
        "source": observation.source,
        "repository": observation.repository,
        "pull_request": observation.pull_request,
        "candidate_sha": observation.candidate_sha,
        "target_sha": next(
            p for p in observation.merge_parents if p != observation.candidate_sha
        ),
        "merge_commit_sha": observation.merge_commit_sha,
        "checks": [
            {
                "name": run.name,
                "run_id": run.run_id,
                "conclusion": run.conclusion,
                "url": run.url,
            }
            for run in selected
        ],
    }


def default_runner(arguments: Sequence[str]) -> str:
    """Run ``gh`` read-only; the output of a failing call is never kept."""
    result = subprocess.run(
        ["gh", *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh exited with {result.returncode}")
    return result.stdout


class GitHubCiSource:
    """Read-only GitHub adapter: three GET calls through an injectable ``runner``."""

    def __init__(
        self, runner: Runner = default_runner, host: str = "github.com"
    ) -> None:
        self._runner = runner
        self._host = host

    def _get(self, path: str) -> JsonObject:
        text = self._runner(["api", "--hostname", self._host, "-X", "GET", path])
        return cast(JsonObject, json.loads(text))

    def _unavailable(
        self, repository: str, pull_request: int, reason: str
    ) -> CiObservation:
        return CiObservation(
            repository=repository,
            pull_request=pull_request,
            base_ref="",
            candidate_sha="",
            checkout="unknown",
            merge_commit_sha=None,
            merge_parents=(),
            checks=(),
            error=_SAFE_REASON.sub("?", reason)[:200],
        )

    def observe(self, repository: str, pull_request: int) -> CiObservation:
        try:
            return self._observe(repository, pull_request)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return self._unavailable(
                repository, pull_request, "the CI service did not answer"
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            return self._unavailable(
                repository, pull_request, "the CI service answer is not usable"
            )

    def _observe(self, repository: str, pull_request: int) -> CiObservation:
        pr = self._get(f"repos/{repository}/pulls/{pull_request}")
        actual_repo = pr["base"]["repo"]["full_name"]
        head = pr["head"]["sha"]
        merge = pr.get("merge_commit_sha")
        mergeable = pr.get("mergeable")
        base = dict(
            repository=actual_repo,
            pull_request=int(pr["number"]),
            base_ref=pr["base"]["ref"],
            candidate_sha=head,
        )
        if not (
            pr.get("state") == "open"
            and mergeable is True
            and isinstance(merge, str)
            and _SHA.fullmatch(merge)
        ):
            return CiObservation(
                **base,
                checkout="unknown",
                merge_commit_sha=None,
                merge_parents=(),
                checks=(),
            )
        commit = self._get(f"repos/{actual_repo}/commits/{merge}")
        if commit.get("sha") != merge:
            return CiObservation(
                **base,
                checkout="unknown",
                merge_commit_sha=None,
                merge_parents=(),
                checks=(),
            )
        parents = tuple(item["sha"] for item in commit["parents"])
        runs = self._get(
            f"repos/{actual_repo}/commits/{merge}/check-runs?per_page={_MAX_CHECK_RUNS}"
        )
        head_runs = self._get(
            f"repos/{actual_repo}/commits/{head}/check-runs?per_page={_MAX_CHECK_RUNS}"
        )
        for page in (runs, head_runs):
            if int(page["total_count"]) > len(page["check_runs"]):
                raise ValueError("incomplete check-run listing")
        checks = tuple(
            self._check(item)
            for item in (*runs["check_runs"], *head_runs["check_runs"])
        )
        return CiObservation(
            **base,
            checkout="combined",
            merge_commit_sha=merge,
            merge_parents=parents,
            checks=checks,
        )

    @staticmethod
    def _check(item: JsonObject) -> CheckRun:
        url = item.get("html_url")
        return CheckRun(
            name=str(item["name"]),
            head_sha=str(item["head_sha"]),
            status=str(item["status"]),
            conclusion=item.get("conclusion"),
            run_id=str(item["id"]),
            url=url if isinstance(url, str) and _SAFE_URL.fullmatch(url) else None,
        )
