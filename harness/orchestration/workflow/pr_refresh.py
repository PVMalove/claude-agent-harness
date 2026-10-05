"""PR-preparation refresh of an integration record (issue #533).

When the integration ref moved after a ticket branch was published, the branch is brought up to
the exact new target SHA by a clean rebase of the batch's own issue branch -- no developer restart,
no resolver cycle.  The rewrite is recorded as an immutable ``IntegrationRefreshRecord`` next to the
integration record; the QA of the old candidate stays historical evidence and never confirms the
new candidate, which needs a new CI or local-QA check of the new pair.
"""

from __future__ import annotations

import argparse
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
)
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
        batch = _load_batch(root, identity["source_batch_id"])
        worktree = Path(batch["worktree"])
        _git(worktree, "fetch", remote, "--", ref)
        _git(worktree, "cat-file", "-e", f"{tip}^{{commit}}")
        _git(worktree, "rebase", tip)
        new_candidate = _git(worktree, "rev-parse", "HEAD")
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
