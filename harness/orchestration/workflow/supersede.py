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

from harness.errors import INTERNAL_INVARIANT_REMEDY
from harness.orchestration.core import utils
from harness.orchestration.core.git_utils import _candidate_commit, _git_is_ancestor
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _non_empty,
    _safe_id,
)
from harness.orchestration.ledger.ledger_ops import _load_batch, _records_root
from harness.orchestration.workflow import commit_plan as plan_rules
from harness.orchestration.workflow.approval import _approval
from harness.orchestration.workflow.history import (
    _current_developer_candidate,
    _pending_report,
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


def _resume_record(source: JsonObject) -> JsonObject:
    """The abandoned batch's ``abandoned.last_accepted`` record that the superseding batch resumes
    from; refuse any other source."""
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
        link = source.get("supersedes")
        earlier = link.get("batch_id") if isinstance(link, dict) else None
        remedy = (
            "nothing of that batch was accepted: create an ordinary batch without "
            "--supersedes, which starts from the architect stage"
        )
        if isinstance(earlier, str):
            remedy = (
                f"nothing of that batch was accepted, but it superseded batch {earlier}: pass "
                f"--supersedes {earlier} to resume from that batch's last accepted record "
                "again, or create an ordinary batch without --supersedes, which starts from "
                "the architect stage"
            )
        raise CoordinatorError(
            f"abandoned batch {source_id} has no abandoned.last_accepted record to resume from",
            remedy=remedy,
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


def _architect_reference(root: Path, source: JsonObject) -> JsonObject | None:
    """The accepted architect of ``source`` by reference, or ``None`` when it has none.

    A source that carried its architect from a batch it superseded hands that reference on, so a
    chain of superseding batches keeps pointing at the one accepted architect report.
    """
    entry = next(
        plan_rules.decided_entries(source, "architect", {"accept", "override-warning"}),
        None,
    )
    if entry is None:
        link = source.get("supersedes")
        carried = link.get("architect") if isinstance(link, dict) else None
        return dict(carried) if isinstance(carried, dict) else None
    _pending_report(root, source, entry)  # the referenced report is intact
    return {
        "batch_id": source["batch_id"],
        "dispatch_id": entry["dispatch_id"],
        "report": entry["report"],
        "report_sha256": entry["report_sha256"],
        "commit_plan_sha256": plan_rules.accepted_plan_sha256(source),
    }


def _carried_plan(source: JsonObject, architect: JsonObject) -> list[JsonObject] | None:
    """The commit plan pinned on the carried architect accept (#478), copied as recorded."""
    digest = architect.get("commit_plan_sha256")
    if not isinstance(digest, str):
        return None
    plan = source.get("commit_plan")
    if not isinstance(plan, list) or plan_rules.plan_sha256(plan) != digest:
        raise CoordinatorError(
            f"batch {source.get('batch_id')} commit_plan does not match the plan pinned on its "
            "architect accept",
            remedy="the abandoned batch's commit_plan diverged from its architect accept -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    return [dict(entry) for entry in plan]


def _start_commit(repo: Path, source: JsonObject, last: JsonObject) -> str | None:
    """The last accepted candidate the first developer starts from; ``None`` when only the
    architect was accepted."""
    candidate = last.get("candidate_commit")
    if candidate is None:
        return None
    try:
        return _candidate_commit(repo, candidate)
    except CoordinatorError as exc:
        raise CoordinatorError(
            f"the last accepted candidate {candidate} of batch {source.get('batch_id')} does not "
            "resolve to a commit",
            remedy="restore that commit in the issue branch (for example from git reflog) or "
            "fetch it, then create the superseding batch again; or create an ordinary batch "
            "without --supersedes",
        ) from exc


def developer_next_action(batch: JsonObject) -> str:
    """The developer action that follows the settled architect stage of ``batch``.

    It is ``developer-retry`` while a superseding batch's start commit does not descend from its
    integration base and no developer report of the batch has been decided: that first developer
    rebases the start commit onto ``rebase_target_commit`` and fixes on top of it (#504). Every
    other batch starts with the initial ``developer``.
    """
    link = batch.get("supersedes")
    target = link.get("rebase_target_commit") if isinstance(link, dict) else None
    decided = any(
        item.get("role") == "developer" and isinstance(item.get("decision"), dict)
        for item in batch.get("dispatches", [])
    )
    return "developer-retry" if isinstance(target, str) and not decided else "developer"


def rebase_target(
    repo: Path,
    root: Path,
    batch: JsonObject,
    next_action: object,
    role: str,
    purpose: str,
    candidate: str | None,
) -> str | None:
    """The rebase target a superseding batch's developer-retry brief carries, or ``None``.

    It is the integration base pinned at ``batch create`` (``supersedes.rebase_target_commit``),
    bound while the developer-retry work brief continues a snapshot (``candidate``, else the
    batch's current developer candidate) that does not contain it yet; a retry of a report that
    already rebased onto it continues without a target.
    """
    link = batch.get("supersedes")
    target = link.get("rebase_target_commit") if isinstance(link, dict) else None
    if (
        not isinstance(target, str)
        or next_action != "developer-retry"
        or (role, purpose) != ("developer", "work")
    ):
        return None
    snapshot = candidate or _current_developer_candidate(repo, root, batch)
    if snapshot is None or _git_is_ancestor(repo, target, snapshot):
        return None
    return target


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

    With the same definition of done, the source's accepted architect is carried by reference and
    its pinned commit plan is copied, so the batch starts at the developer stage; with another
    definition of done nothing is carried and the architect stage runs again. The first developer
    starts at the last accepted candidate (``start_commit``); when that does not descend from the
    integration base pinned now, the base becomes its ``rebase_target_commit``.
    """
    source = _load_source(root, source_id)
    last = _resume_record(source)
    _require_same_work(source, record)
    same_done = source.get("definition_of_done") == record["definition_of_done"]
    architect = _architect_reference(root, source) if same_done else None
    start = _start_commit(repo, source, last)
    base = record["integration_base_commit"]
    target = (
        base if start is not None and not _git_is_ancestor(repo, base, start) else None
    )
    link: JsonObject = {
        "batch_id": source["batch_id"],
        "approved_by": approval["approved_by"],
        "approved_at": approval["approved_at"],
        "last_accepted": dict(last),
        "definition_of_done_matches": same_done,
        "architect": architect,
        "start_commit": start,
        "rebase_target_commit": target,
    }
    if architect is not None:
        plan = _carried_plan(source, architect)
        if plan is not None:
            record["commit_plan"] = plan
    record["supersedes"] = link
    if architect is not None:
        # The developer stage is the only next action: no architect dispatch is allowed.
        record["next_action"] = developer_next_action(record)
    moment = utils._now()
    routing: JsonObject = {
        "route": _require_route(ROUTE),
        "previous_role": last.get("role"),
        "reason_category": None,
        "next_role": "developer" if architect is not None else "architect",
        "next_action": record.get("next_action"),
        "candidate_commit": start,
        "rebase_target_commit": target,
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
            "architect": architect,
        },
        "approver": {"kind": "human", "name": approval["approved_by"]},
        "approved_at": approval["approved_at"],
    }
