"""The ``supersede`` route (issue #506): a new batch that resumes an abandoned one.

After a forced abandon (``batch decide --decision abandon`` on a dead end), ``batch create
--supersedes <batch>`` plans a new batch for the same ticket and issue branch. A human approves it
on ``batch create``. The new batch and its immutable plan carry one ``supersedes`` link to the
abandoned batch and its ``abandoned.last_accepted`` record, and the new batch records the
``supersede`` route once in its own ``coordinator_decisions``.

Nothing else of the abandoned batch is copied: its risk assessments, reviews, QA, carried items and
operator decisions stay there and are reached through ``supersedes.batch_id`` only. The abandoned
batch is only read. ``history`` and ``commit_plan`` read ``batch["supersedes"]`` directly, so this
module imports them without an import cycle.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from harness.orchestration.core import utils
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _non_empty,
    _safe_id,
)
from harness.orchestration.ledger.ledger_ops import _load_batch, _records_root
from harness.orchestration.workflow.approval import _approval
from harness.orchestration.workflow.history import (
    _require_route,
    _validate_batch_integrity,
)

ROUTE = "supersede"
POLICY_APPROVER_PREFIX = "policy:"


def request(args: argparse.Namespace) -> tuple[str, JsonObject] | None:
    """The abandoned batch a ``batch create`` supersedes and its human approval, or ``None``.

    Read before the ledger lock, so the approval TTL and the terminal gate apply as on any other
    human decision. ``--approved-by`` and ``--approved-at`` belong only to ``--supersedes``: the
    planning approval of an ordinary batch is ``batch approve``.
    """
    source = getattr(args, "supersedes", None)
    approved_by = getattr(args, "approved_by", None)
    approved_at = getattr(args, "approved_at", None)
    if source is None:
        if approved_by is not None or approved_at is not None:
            raise CoordinatorError(
                "--approved-by and --approved-at on batch create approve only --supersedes",
                remedy="drop --approved-by and --approved-at (an ordinary batch is approved with "
                "'batch approve'), or pass --supersedes <abandoned batch id> to resume that batch",
            )
        return None
    if not _non_empty(source):
        raise CoordinatorError(
            "--supersedes must name the abandoned batch",
            remedy="pass --supersedes <batch id> of a batch abandoned with "
            "'batch decide --decision abandon'",
        )
    if not _non_empty(approved_by) or not _non_empty(approved_at):
        raise CoordinatorError(
            "a superseding batch requires a human approval",
            remedy="pass --approved-by and --approved-at naming the human who approved resuming "
            f"batch {source.strip()} in a new batch",
        )
    if approved_by.strip().startswith(POLICY_APPROVER_PREFIX):
        raise CoordinatorError(
            f"a superseding batch is approved by a human, never by a policy ({approved_by.strip()})",
            remedy="pass --approved-by naming the human who approved the superseding batch; no "
            "approval_policy approves it",
        )
    return source.strip(), _approval(args)


def _load_source(root: Path, source_id: str) -> JsonObject:
    path = _records_root(root) / "batches" / f"{_safe_id(source_id, 'batch')}.json"
    if not path.is_file():
        raise CoordinatorError(
            f"batch {source_id} to supersede does not exist in the current ledger generation",
            remedy="pass --supersedes the batch_id of an abandoned batch of this ticket "
            "('batch list --ticket <ticket> --state abandoned'); a batch of an earlier ledger "
            "generation cannot be superseded, so create an ordinary batch instead",
        )
    source = _load_batch(root, source_id)
    _validate_batch_integrity(root, source)
    return source


def _last_accepted(source: JsonObject) -> JsonObject:
    """The abandoned batch's ``abandoned.last_accepted`` record; refuse any other source."""
    source_id = source.get("batch_id")
    if source.get("state") != "abandoned":
        raise CoordinatorError(
            f"batch {source_id} is {source.get('state')!r}, not abandoned, so it cannot be "
            "superseded",
            remedy=f"a dead-end batch is superseded only after 'batch decide --batch {source_id} "
            "--decision abandon --reason <why>'; a batch closed with 'batch abandon' has no "
            "accepted stage to resume, so create an ordinary batch instead",
        )
    abandoned = source.get("abandoned")
    last = abandoned.get("last_accepted") if isinstance(abandoned, dict) else None
    if not isinstance(last, dict):
        raise CoordinatorError(
            f"abandoned batch {source_id} has no abandoned.last_accepted record to resume from",
            remedy="nothing of that batch was accepted: create an ordinary batch without "
            "--supersedes, which starts from the architect stage",
        )
    return last


def _require_same_work(source: JsonObject, record: JsonObject) -> None:
    for field in ("ticket", "branch"):
        if source.get(field) != record[field]:
            raise CoordinatorError(
                f"batch {source.get('batch_id')} belongs to {field} {source.get(field)!r}, not "
                f"{record[field]!r}",
                remedy=f"supersede only a batch of the same ticket and issue branch: pass "
                f"--{field} {source.get(field)}, or create an ordinary batch without --supersedes",
            )


def attach(
    repo: Path,
    root: Path,
    record: JsonObject,
    source_id: str,
    approval: JsonObject,
) -> JsonObject:
    """Link the batch about to be written to the abandoned batch ``source_id``.

    Refuses, with a remedy and before anything is written, a source that is missing, not
    ``abandoned``, without an ``abandoned.last_accepted`` object, or of another ticket or issue
    branch. Sets ``record["supersedes"]`` and the one ``supersede`` coordinator decision, and returns
    the ``decision`` detail of the batch transition audit record. Runs under the ledger lock.
    """
    source = _load_source(root, source_id)
    last = _last_accepted(source)
    _require_same_work(source, record)
    link: JsonObject = {
        "batch_id": source["batch_id"],
        "approved_by": approval["approved_by"],
        "approved_at": approval["approved_at"],
        "last_accepted": dict(last),
        "definition_of_done_matches": source.get("definition_of_done")
        == record["definition_of_done"],
    }
    record["supersedes"] = link
    moment = utils._now()
    routing: JsonObject = {
        "route": _require_route(ROUTE),
        "previous_role": last.get("role"),
        "reason_category": None,
        "next_role": "architect",
        "next_action": record.get("next_action"),
        "candidate_commit": None,
        "superseded_batch_id": source["batch_id"],
        "rationale": f"batch {source['batch_id']} was abandoned after its accepted "
        f"{last.get('role')} stage {last.get('dispatch_id')}; this batch resumes from that "
        "record, and its risk assessment, review and QA run again.",
        "decided_at": moment,
    }
    record["coordinator_decisions"] = [
        {
            "decision": ROUTE,
            "approved_by": approval["approved_by"],
            "approved_at": approval["approved_at"],
            "note": f"supersedes abandoned batch {source['batch_id']}",
            "routing": routing,
        }
    ]
    return {
        "dispatch_id": None,
        "decision": ROUTE,
        "route": routing["route"],
        "evidence": {
            "batch_id": source["batch_id"],
            "last_accepted": link["last_accepted"],
        },
        "approver": {"kind": "human", "name": approval["approved_by"]},
        "approved_at": approval["approved_at"],
    }
