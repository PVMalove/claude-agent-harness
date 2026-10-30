"""The Release Gate of a candidate (ADR 0026, section 4; ADR 0021, section 7).

The gate binds a candidate to the pair ``{candidate_sha, target_sha}`` its QA checked and to the
tree of that candidate. Python decides it from the Git facts it observes; the runtime only judges
the stored QA evidence of the pair (``qa_evidence_verify``). This module keeps the one function
that records a pair, so ``dispatch publish``, ``integration prepare`` and ``integration refresh``
write the same immutable record.
"""

from __future__ import annotations

from pathlib import Path

from harness.errors import INTERNAL_INVARIANT_REMEDY
from harness.orchestration.core import state_view
from harness.orchestration.core.constants import INTEGRATION_SHA_PATTERN
from harness.orchestration.core.git_utils import _git
from harness.orchestration.core.utils import CoordinatorError, JsonObject, _read_object
from harness.orchestration.ledger.ledger_ops import _write_record
from harness.orchestration.ledger.lifecycle import (
    IntegrationPairRecord,
    LifecycleLedger,
)

PAIR_RECORD_CONTRACT = 1


def pair_id(candidate: str, target: str) -> str:
    """The id of the pair record: it derives from the pair alone."""
    return IntegrationPairRecord.derive_id(
        {"candidate_sha": candidate, "target_sha": target}
    )


def _pair_path(ledger: LifecycleLedger, candidate: str, target: str) -> Path:
    return (
        ledger.records_root()
        / IntegrationPairRecord.directory
        / f"{pair_id(candidate, target)}.json"
    )


def candidate_tree(repo: Path, candidate: str) -> str:
    """``git rev-parse <candidate>^{tree}``: the tree a merge of exactly the candidate lands."""
    if INTEGRATION_SHA_PATTERN.fullmatch(candidate) is None:
        raise CoordinatorError(
            f"the candidate {candidate!r} is not a full lowercase commit SHA",
            remedy="pass the full SHA of the published candidate",
        )
    tree = _git(repo, "rev-parse", "--verify", "--quiet", f"{candidate}^{{tree}}")
    if INTEGRATION_SHA_PATTERN.fullmatch(tree) is None:
        raise CoordinatorError(
            f"git answered {tree!r} for the tree of {candidate}",
            remedy="fetch the candidate commit into this repository and repeat the command",
        )
    return tree


def record_pair(
    ledger: LifecycleLedger, repo: Path, candidate: str, target: str
) -> JsonObject:
    """Record the pair ``{candidate, target}`` with the tree of ``candidate``; idempotent.

    The caller holds the ledger lock (the store unit of its command). A pair that is recorded
    already is returned as it is; a stored pair with other content is refused.
    """
    try:
        tree = candidate_tree(repo, candidate)
    except CoordinatorError as exc:
        raise CoordinatorError(
            f"cannot record the Release Gate pair of {candidate}: {exc.message}",
            remedy=exc.remedy,
        ) from exc
    document: JsonObject = {
        "pair_id": pair_id(candidate, target),
        "contract": PAIR_RECORD_CONTRACT,
        "candidate_sha": candidate,
        "target_sha": target,
        "candidate_tree_sha": tree,
    }
    path = _pair_path(ledger, candidate, target)
    if state_view.is_file(path):
        stored = _read_object(path, "Release Gate pair record")
        if stored != document:
            raise CoordinatorError(
                f"the Release Gate pair record {path.name} names another candidate_tree_sha or "
                "fields than the candidate has",
                remedy="the pair record was changed after it was written -- "
                + INTERNAL_INVARIANT_REMEDY,
            )
        return stored
    _write_record(ledger, IntegrationPairRecord.from_dict(document))
    return document


def publish_pair(batch: JsonObject, dispatch: JsonObject) -> JsonObject:
    """The pair a publish pushes: its candidate and the integration base its batch checked."""
    return {
        "candidate_sha": dispatch["candidate_commit"],
        "target_sha": batch.get("integration_base_commit") or batch["base_commit"],
    }


def stored_pair(
    ledger: LifecycleLedger, candidate: str, target: str
) -> JsonObject | None:
    """The recorded pair ``{candidate, target}``, or ``None`` before it is recorded."""
    path = _pair_path(ledger, candidate, target)
    if not state_view.is_file(path):
        return None
    return _read_object(path, "Release Gate pair record")


def stored_tree(ledger: LifecycleLedger, pair: JsonObject) -> str | None:
    """The ``candidate_tree_sha`` of a recorded pair, or ``None`` before it is recorded."""
    stored = stored_pair(ledger, pair["candidate_sha"], pair["target_sha"])
    tree = None if stored is None else stored.get("candidate_tree_sha")
    return tree if isinstance(tree, str) else None
