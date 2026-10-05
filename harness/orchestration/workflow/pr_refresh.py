"""PR-preparation refresh of an integration record (issue #533).

When the integration ref moved after a ticket branch was published, the branch is brought up to
the exact new target SHA by a clean rebase of the batch's own issue branch -- no developer restart,
no resolver cycle.  The rewrite is recorded as an immutable ``IntegrationRefreshRecord`` next to the
integration record; the QA of the old candidate stays historical evidence and never confirms the
new candidate, which needs a new CI or local-QA check of the new pair.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from harness.orchestration.core import utils
from harness.orchestration.core.git_utils import (
    _commits_between,
    _git,
    _remote_branch_tip,
)
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _read_object,
    _repo,
    _sanitise,
)
from harness.orchestration.core.workspace import _validate_branch
from harness.orchestration.ledger.ledger_ops import (
    _ledger_lock,
    _load_batch,
    _records_root,
    _state_root,
    _write_record,
)
from harness.orchestration.ledger.lifecycle import (
    IntegrationRefreshRecord,
    LifecycleLedger,
)
from harness.orchestration.workflow import integration

REFRESH_RECORD_CONTRACT = 1


def refresh_records(root: Path, record_id: str) -> list[JsonObject]:
    """The refresh records of an integration record in chain order: each one rebases the candidate
    and target the previous one produced, starting from the record's own pair."""
    directory = _records_root(root) / IntegrationRefreshRecord.directory
    found = [
        _read_object(path, "integration refresh record")
        for path in sorted(directory.glob("*.json") if directory.is_dir() else [])
    ]
    found = [item for item in found if item.get("integration_record_id") == record_id]
    identity = integration._load_record(root, record_id)["identity"]
    pair = (identity["candidate_sha"], identity["target_sha"])
    chain: list[JsonObject] = []
    while True:
        step = next(
            (
                item
                for item in found
                if (item["previous_candidate_sha"], item["previous_target_sha"]) == pair
            ),
            None,
        )
        if step is None:
            return chain
        chain.append(step)
        pair = (step["new_candidate_sha"], step["target_sha"])


def current_pair(root: Path, record: JsonObject) -> JsonObject:
    """The candidate/target pair the branch is at after every recorded refresh."""
    chain = refresh_records(root, record["integration_record_id"])
    if chain:
        return {
            "candidate_sha": chain[-1]["new_candidate_sha"],
            "target_sha": chain[-1]["target_sha"],
        }
    identity = record["identity"]
    return {
        "candidate_sha": identity["candidate_sha"],
        "target_sha": identity["target_sha"],
    }


def _run_git(worktree: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(worktree), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _require_clean_own_branch(worktree: Path, branch: str, candidate: str) -> None:
    """The batch's own worktree is exactly where the record says: on the issue branch, at the
    candidate, with nothing uncommitted and no operation in progress.  Anything else is unfinished
    work this route must never overwrite, so it stops instead of stashing, resetting or forcing."""
    if _git(worktree, "rev-parse", "--abbrev-ref", "HEAD") != branch:
        raise CoordinatorError(
            f"the batch worktree is not on its issue branch {branch!r}",
            remedy="return the batch worktree to its issue branch; refresh never switches a foreign checkout",
        )
    if _git(worktree, "status", "--porcelain"):
        raise CoordinatorError(
            "the batch worktree has uncommitted changes; refresh does not touch unfinished work",
            remedy="commit or set aside the worktree changes yourself, then retry the refresh",
        )
    for marker in ("rebase-merge", "rebase-apply", "MERGE_HEAD"):
        if (worktree / _git(worktree, "rev-parse", "--git-path", marker)).exists():
            raise CoordinatorError(
                "a Git operation is already in progress in the batch worktree",
                remedy="finish or abort that operation yourself, then retry the refresh",
            )
    head = _git(worktree, "rev-parse", "HEAD")
    if head != candidate:
        raise CoordinatorError(
            f"the issue branch is at {head}, not at the recorded candidate {candidate}",
            remedy="the branch has work the record does not know; run a normal developer dispatch for it",
        )


def _require_remote_unchanged(
    repo: Path, remote: str, branch: str, candidate: str
) -> None:
    tip = _remote_branch_tip(repo, remote, branch)
    if tip != candidate:
        raise CoordinatorError(
            f"remote branch {branch!r} is {tip or 'missing'}, not the recorded candidate {candidate}; "
            "foreign commits would be lost by a rewrite",
            remedy="reconcile the remote branch yourself; refresh never overwrites commits it did not publish",
        )


def _conflict(worktree: Path, base: JsonObject, branch: str) -> JsonObject:
    """A textual conflict: report the resolver data and leave the worktree exactly as it was."""
    files = [
        line
        for line in _git(
            worktree, "diff", "--name-only", "--diff-filter=U"
        ).splitlines()
        if line
    ]
    _git(worktree, "rebase", "--abort")
    _git(worktree, "checkout", "-q", branch)
    return {
        **base,
        "state": "conflict",
        "rebased": False,
        "resolver": {
            "required": True,
            "cycles_spent": 0,
            "conflicting_files": files,
            "worktree": str(worktree),
            "candidate_sha": base["candidate_sha"],
            "target_sha": base["integration_tip"],
        },
    }


def _publish_rewrite(
    worktree: Path, remote: str, branch: str, old: str, new: str
) -> None:
    """Move the remote issue branch from ``old`` to ``new`` only while it still is ``old``, so a
    concurrent foreign commit makes the push fail instead of being lost."""
    result = _run_git(
        worktree,
        "push",
        f"--force-with-lease=refs/heads/{branch}:{old}",
        remote,
        f"{new}:refs/heads/{branch}",
    )
    if result.returncode != 0:
        detail = _sanitise((result.stderr or result.stdout).strip())
        raise CoordinatorError(
            f"the remote branch {branch!r} changed or refused the rewrite: {detail or 'unknown error'}",
            remedy="the local branch was left at the recorded candidate; inspect the remote change, then retry the refresh",
        )


def integration_refresh(args: argparse.Namespace) -> JsonObject:
    """Rebase the own issue branch onto the current integration SHA when it moved."""
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        record = integration._resolve_record(root, args)
        integration._check_history_unchanged(root, record)
        identity = record["identity"]
        pair = current_pair(root, record)
        remote, ref = identity["remote"], identity["integration_ref"]
        tip = _remote_branch_tip(repo, remote, ref)
        if tip is None:
            raise CoordinatorError(
                f"integration ref {ref!r} is not a branch of remote {remote!r}",
                remedy="check the integration ref and the remote, then retry the refresh",
            )
        base: JsonObject = {
            "integration_record_id": record["integration_record_id"],
            "branch": identity["branch"],
            "candidate_sha": pair["candidate_sha"],
            "target_sha": pair["target_sha"],
            "integration_tip": tip,
        }
        if tip == pair["target_sha"]:
            return {**base, "state": "unchanged", "rebased": False}
        branch = identity["branch"]
        _validate_branch(repo, branch)
        batch = _load_batch(root, identity["source_batch_id"])
        worktree = Path(batch["worktree"])
        _require_clean_own_branch(worktree, branch, pair["candidate_sha"])
        _require_remote_unchanged(repo, remote, branch, pair["candidate_sha"])
        _git(worktree, "fetch", remote, "--", ref)
        _git(worktree, "cat-file", "-e", f"{tip}^{{commit}}")
        # Rebase a detached HEAD so the issue branch only moves after the rewrite is published.
        _git(worktree, "checkout", "-q", "--detach")
        if _run_git(worktree, "rebase", tip).returncode != 0:
            return _conflict(worktree, base, branch)
        new_candidate = _git(worktree, "rev-parse", "HEAD")
        try:
            _publish_rewrite(
                worktree, remote, branch, pair["candidate_sha"], new_candidate
            )
        except CoordinatorError:
            _git(worktree, "checkout", "-q", branch)
            raise
        _git(worktree, "checkout", "-q", "-B", branch, new_candidate)
        members: JsonObject = {
            "integration_record_id": record["integration_record_id"],
            "previous_candidate_sha": pair["candidate_sha"],
            "previous_target_sha": pair["target_sha"],
            "new_candidate_sha": new_candidate,
            "target_sha": tip,
        }
        document: JsonObject = {
            "refresh_id": IntegrationRefreshRecord.derive_id(members),
            "contract": REFRESH_RECORD_CONTRACT,
            **members,
            "ticket": identity["ticket"],
            "branch": identity["branch"],
            "source_batch_id": identity["source_batch_id"],
            "remote": remote,
            "integration_ref": ref,
            "original_candidate_sha": identity["candidate_sha"],
            "history": {
                "previous_commits": _commits_between(
                    repo, pair["target_sha"], pair["candidate_sha"]
                ),
                "commits": _commits_between(repo, tip, new_candidate),
            },
            "resolver": {"invoked": False, "cycles_spent": 0},
            "evidence": {
                "historical_qa": record["source"]["qa"],
                "confirms_new_candidate": False,
                "verification_required": ["ci", "local-qa"],
            },
            "re_review_required": False,
            "recorded_at": utils._now(),
        }
        _write_record(ledger, IntegrationRefreshRecord.from_dict(document))
    return {
        **base,
        "state": "rebased",
        "rebased": True,
        "new_candidate_sha": new_candidate,
        "refresh_id": document["refresh_id"],
        "verification_required": True,
    }
