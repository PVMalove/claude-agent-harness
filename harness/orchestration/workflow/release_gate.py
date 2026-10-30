"""The Release Gate of a candidate (ADR 0026, section 4; ADR 0021, section 7).

The gate binds a candidate to the pair ``{candidate_sha, target_sha}`` its QA checked and to the
tree of that candidate. Python decides it from the Git facts it observes: the head of the issue
branch, the ancestry of the target and the tree a merge created. The runtime only judges the stored
QA evidence of the pair (``qa_evidence_verify``, on the wire ``qa.evidence.verify``), and only in the
runtime-store epoch: the file ledger has no runtime verdict and no Python stand-in for one. This
module keeps the one function that records a pair, so ``dispatch publish``, ``integration
prepare`` and ``integration refresh`` write the same immutable record.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from harness.errors import INTERNAL_INVARIANT_REMEDY
from harness.orchestration.core import state_view
from harness.orchestration.core.constants import (
    INTEGRATION_SHA_PATTERN,
    QaEvidenceVerdict,
    QaEvidenceViolation,
    ReleaseGateRefusal,
)
from harness.orchestration.core.git_utils import (
    _git,
    _git_is_ancestor,
    _remote_branch_tip,
    _run_git,
)
from harness.orchestration.core.utils import CoordinatorError, JsonObject, _read_object
from harness.orchestration.ledger.ledger_ops import _write_record
from harness.orchestration.ledger.lifecycle import (
    IntegrationPairRecord,
    LifecycleLedger,
    store_is_authority,
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


# How long the runtime may take to judge the evidence of a pair: one read snapshot.
VERIFY_TIMEOUT_SECONDS = 30.0


def qa_evidence_verify(repo: Path, pair: JsonObject) -> JsonObject:
    """``qa_evidence_verify`` (ADR 0021, section 7): the runtime's verdict on the stored QA
    evidence of ``pair``, ``{"verdict", "violations", "pair", "confirmed_by"}``.

    Read-only on both sides, so a repeat is safe. A runtime that cannot answer raises
    ``RuntimeClientError``; an answer outside the registry vocabularies is refused."""
    from harness.orchestration.runtime_client import QA_EVIDENCE_VERIFY, RuntimeClient

    reply = RuntimeClient(repo).call(
        QA_EVIDENCE_VERIFY,
        operation_id=f"{QA_EVIDENCE_VERIFY}-{uuid.uuid4().hex}",
        payload={
            "candidate_sha": pair["candidate_sha"],
            "target_sha": pair["target_sha"],
        },
        timeout_seconds=VERIFY_TIMEOUT_SECONDS,
    )
    answer = reply.result
    violations = answer.get("violations")
    known = (
        answer.get("verdict") in tuple(QaEvidenceVerdict)
        and isinstance(violations, list)
        and all(
            isinstance(item, dict) and item.get("code") in tuple(QaEvidenceViolation)
            for item in violations
        )
        and (answer["verdict"] == QaEvidenceVerdict.OK) == (not violations)
    )
    if not known:
        raise CoordinatorError(
            "qa.evidence.verify answered outside the qa_evidence vocabularies",
            remedy="update harness or harness-runtime so both speak the same protocol minor",
        )
    return answer


def violation_codes(answer: JsonObject) -> list[str]:
    """The violation codes of a verdict, in its order."""
    return [str(item["code"]) for item in answer["violations"]]


def require_evidence(repo: Path, root: Path, pair: JsonObject) -> JsonObject | None:
    """Refuse a push unless ``qa_evidence_verify`` answers ``ok`` for exactly ``pair``.

    Only the runtime-store epoch asks the runtime, as the operational hold does: there an
    unavailable or an older runtime refuses the push, and nothing stands in for its verdict. In the
    file-ledger epoch the runtime holds no evidence: ``None``, and the checks of the caller stay
    the gate."""
    if not store_is_authority(root):
        return None
    answer = qa_evidence_verify(repo, pair)
    if answer["verdict"] != QaEvidenceVerdict.OK:
        codes = violation_codes(answer)
        raise CoordinatorError(
            f"the Release Gate refuses {pair['candidate_sha']}: qa.evidence.verify found no "
            f"full QA run of the pair on {pair['target_sha']} ({', '.join(codes)})",
            remedy=f"[{','.join(codes)}] run a new full QA of exactly this candidate on this "
            "target and accept it, then repeat dispatch publish; the dispatch stays approved",
        )
    return answer


def git_refusals(repo: Path, remote: str, branch: str, pair: JsonObject) -> list[str]:
    """The Git facts that refuse the merge handoff of ``pair``: the remote issue branch is not
    the candidate, or the target is not in the history of the candidate."""
    refusals: list[str] = []
    if _remote_branch_tip(repo, remote, branch) != pair["candidate_sha"]:
        refusals.append(ReleaseGateRefusal.BRANCH_HEAD_MISMATCH)
    try:
        ancestor = _git_is_ancestor(repo, pair["target_sha"], pair["candidate_sha"])
    except CoordinatorError as exc:
        raise CoordinatorError(
            f"cannot check that {pair['target_sha']} is in the history of "
            f"{pair['candidate_sha']}: {exc.message}",
            remedy=f"git fetch {remote}, then repeat 'integration next'",
        ) from exc
    if not ancestor:
        refusals.append(ReleaseGateRefusal.TARGET_NOT_IN_CANDIDATE_HISTORY)
    return refusals


def handoff_gate(
    repo: Path, root: Path, remote: str, branch: str, pair: JsonObject
) -> tuple[list[str], JsonObject | None]:
    """The refusals of the merge handoff of ``pair`` and the runtime's verdict (``None`` in the
    file-ledger epoch). A runtime that cannot answer raises: there is no handoff without it."""
    refusals = git_refusals(repo, remote, branch, pair)
    if not store_is_authority(root):
        return refusals, None
    answer = qa_evidence_verify(repo, pair)
    if answer["verdict"] != QaEvidenceVerdict.OK:
        refusals.append(ReleaseGateRefusal.EVIDENCE_REFUSED)
    return refusals, answer


def merged_tree(repo: Path, remote: str, integration_ref: str, commit: str) -> str:
    """The tree of the commit a merge created; the commit must be in this repository."""
    if INTEGRATION_SHA_PATTERN.fullmatch(commit) is None:
        raise CoordinatorError(
            f"--merge-commit {commit!r} is not a full lowercase commit SHA",
            remedy="pass the full SHA of the commit the merge created on the target branch",
        )
    found = _run_git(repo, ("cat-file", "-e", f"{commit}^{{commit}}"))
    if found.returncode != 0:
        raise CoordinatorError(
            f"the merge commit {commit} is not in this repository",
            remedy=f"git fetch {remote} {integration_ref}, then repeat 'integration next'",
        )
    return candidate_tree(repo, commit)
