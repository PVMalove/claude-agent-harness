"""Carried items: obligations a decision hands on to a later stage's immutable brief (issue #499).

A defect the coordinator finds in a clean developer report does not cost a developer retry before
review. The report is accepted with a structured coordinator finding, which the batch records
append-only, and every later code-review brief carries each open finding in its ``carried_items``
section until a review that carried it is accepted. The section is one shared channel keyed by the
kind of source that raised an item, so a later kind joins it without changing its shape.

An item's status is derived, never stored: it stays open until a code-review dispatch whose brief
carried it is accepted or warning-overridden; a retried review leaves it open.

A read-only role that leaves brief items undone lists them as ``incomplete_items`` (issue #501).
They are never stored on the batch: an accept with ``--carry-incomplete`` records their ids in its
routing record, and each target role's work brief reads them back from the immutable report until
an accepted dispatch of that role carried them.

This module reads the ledger through ``history`` and ``ledger_ops`` only; the decision, dispatch,
report and risk modules call into it, never the other way round.
"""

from __future__ import annotations

import argparse
import hashlib
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import cast

from harness.errors import INTERNAL_INVARIANT_REMEDY
from harness.orchestration import operational_guards
from harness.orchestration.core import config as core_config
from harness.orchestration.core import utils
from harness.orchestration.core.config import _reject_sensitive
from harness.orchestration.core.constants import (
    CARRIED_ITEM_ACCOUNTING_FIELDS,
    CARRIED_ITEM_CLOSED_FIELDS,
    CARRIED_ITEM_FIELDS,
    CARRIED_ITEM_NOT_CLOSED_FIELDS,
    CARRIED_ITEM_SOURCES,
    CARRIED_ITEM_STATUSES,
    TOOLING_REASON_CATEGORY,
)
from harness.orchestration.core.git_utils import _candidate_commit
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _canonical,
    _non_empty,
    _read_object,
    _repo,
    _safe_id,
)
from harness.orchestration.core.workspace import (
    _agent_authored_file,
    _reject_non_english,
)
from harness.orchestration.ledger.ledger_ops import (
    _ledger_lock,
    _load_batch,
    _load_dispatch,
    _replace_record,
    _state_root,
)
from harness.orchestration.ledger.lifecycle import BatchRecord, LifecycleLedger
from harness.orchestration.workflow import approval as approvals
from harness.orchestration.workflow import commit_plan as plan_rules
from harness.orchestration.workflow.history import (
    _pending_report,
    _require_route,
    _validate_batch_integrity,
)

COORDINATOR_FINDING = "coordinator-finding"
REVIEW_FINDING = "review-finding"
INCOMPLETE_ITEM = "incomplete-item"
# The routes whose routing record hands a read-only report's incomplete items to later briefs: an
# accept carries them over, and a narrowed retry (``tooling-retry`` when a tool blocked an item)
# hands them to the same stage again.
INCOMPLETE_ITEM_ROUTES = ("carry-over", "narrowed-retry", "tooling-retry")
# The next role and action ``batch decide --decision accept`` moves a batch to after each read-only
# stage; risk assessment runs before any role.
ACCEPT_NEXT_STEP: dict[str, tuple[str | None, str]] = {
    "architect": ("developer", "developer"),
    "verification": (None, "risk-assessment"),
    "code-review": ("qa", "qa"),
    "qa": ("developer", "publish"),
}
# ``batch carry-over`` starts no dispatch and moves no candidate, so the coordinator runs it under
# this policy name, the way ``batch resume`` records ``policy:operational-recovery``.
CARRY_OVER_POLICY = "carry-over"
FINDING_FIELDS = frozenset({"summary", "files", "expected_evidence"})
FINDINGS_FILE_REMEDY = (
    'write the findings file as {"findings": [{"summary": ..., "files": [...], '
    '"expected_evidence": ...}, ...]} in English and pass it again with --findings-file'
)
# How a developer-retry report accounts for the items its brief carried (issue #503).
CLOSURE_FIELD = "carried_item_closure"
CLOSURE_REMEDY = (
    "set carried_item_closure to one record per item of the brief's carried_items: "
    '{"item_id": <id>, "commits": [<sha>, ...]} or {"item_id": <id>, "not_closed": "<reason>"}'
)


def _findings_error(message: str) -> CoordinatorError:
    return CoordinatorError(
        f"coordinator findings file is invalid: {message}", remedy=FINDINGS_FILE_REMEDY
    )


def _files(position: int, value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        raise _findings_error(
            f"finding {position} files must be a non-empty list of repository paths"
        )
    for path in value:
        if (
            not _non_empty(path)
            or path.startswith("/")
            or "\\" in path
            or ".." in PurePosixPath(path).parts
        ):
            raise _findings_error(
                f"finding {position} files must hold repository-relative POSIX paths without "
                f"a leading '/', a backslash or a '..' segment (got {path!r})"
            )
    if len(set(value)) != len(value):
        raise _findings_error(f"finding {position} names a file more than once")
    return list(value)


def parse_findings(document: object) -> list[JsonObject]:
    """Validate a coordinator findings file; return its findings in file order (no I/O).

    Each finding is handed to a role as agent-to-agent protocol text, so it must be English.
    """
    if not isinstance(document, dict) or set(document) != {"findings"}:
        raise _findings_error("it must be a JSON object with exactly one key, findings")
    entries = document["findings"]
    if not isinstance(entries, list) or not entries:
        raise _findings_error("findings must be a non-empty list")
    findings: list[JsonObject] = []
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict) or set(entry) != FINDING_FIELDS:
            raise _findings_error(
                f"finding {position} must have exactly the fields "
                f"{', '.join(sorted(FINDING_FIELDS))}"
            )
        for field in ("summary", "expected_evidence"):
            if not _non_empty(entry[field]):
                raise _findings_error(
                    f"finding {position} {field} must be a non-empty string"
                )
        files = _files(position, entry["files"])
        try:
            _reject_non_english(
                [entry["summary"], entry["expected_evidence"], *files],
                f"finding {position}",
            )
        except CoordinatorError as exc:
            raise _findings_error(exc.message) from exc
        findings.append(
            {
                "summary": entry["summary"],
                "files": files,
                "expected_evidence": entry["expected_evidence"],
            }
        )
    return findings


def read_findings(repo: Path, path: str) -> list[JsonObject]:
    """Read and validate the findings file a coordinator wrote inside the project."""
    document = _read_object(
        _agent_authored_file(repo, path, "a coordinator findings file"),
        "coordinator findings file",
    )
    _reject_sensitive(document, "coordinator findings file")
    return parse_findings(document)


def coordinator_source(entry: JsonObject, candidate: str) -> JsonObject:
    """Where a coordinator finding came from: the accepted developer report and its candidate."""
    return {
        "kind": COORDINATOR_FINDING,
        "dispatch_id": entry["dispatch_id"],
        "report_sha256": entry["report_sha256"],
        "candidate_commit": candidate,
    }


def next_item_ids(batch: JsonObject, count: int) -> list[str]:
    """The ids the next ``count`` coordinator findings of this batch get, in attach order.

    They are ordinal, so a preview names exactly the ids the decision then records.
    """
    recorded = sum(
        1
        for record in batch.get("carried_items", [])
        if record["source"]["kind"] == COORDINATOR_FINDING
    )
    return [
        f"{COORDINATOR_FINDING}-{number}"
        for number in range(recorded + 1, recorded + count + 1)
    ]


def attach(
    batch: JsonObject,
    findings: list[JsonObject],
    *,
    source: JsonObject,
    attached_at: str,
    attached_by: str,
) -> list[JsonObject]:
    """Append one hash-checked record per finding to ``batch.carried_items``; return them."""
    records = []
    for item_id, finding in zip(
        next_item_ids(batch, len(findings)), findings, strict=True
    ):
        body: JsonObject = {
            "item_id": item_id,
            "source": dict(source),
            **finding,
            "attached_at": attached_at,
            "attached_by": attached_by,
        }
        body["record_sha256"] = hashlib.sha256(
            _canonical(body).encode("utf-8")
        ).hexdigest()
        records.append(body)
    batch.setdefault("carried_items", []).extend(records)
    return records


def _settled_item_ids(root: Path, batch: JsonObject) -> set[str]:
    """Items a code-review brief carried and whose review was accepted or warning-overridden."""
    settled: set[str] = set()
    for entry in batch.get("dispatches", []):
        decision = entry.get("decision")
        if (
            entry.get("role") != "code-review"
            or not isinstance(decision, dict)
            or decision.get("decision") not in {"accept", "override-warning"}
        ):
            continue
        section = _load_dispatch(root, entry["dispatch_id"]).get("carried_items") or {}
        settled.update(item["item_id"] for items in section.values() for item in items)
    return settled


def open_coordinator_findings(root: Path, batch: JsonObject) -> list[JsonObject]:
    """The batch's coordinator findings no accepted code-review has settled yet."""
    settled = _settled_item_ids(root, batch)
    return [
        record
        for record in batch.get("carried_items", [])
        if record["item_id"] not in settled
    ]


def _brief_item(record: JsonObject) -> JsonObject:
    return {field: record[field] for field in sorted(CARRIED_ITEM_FIELDS)}


def _review_findings(
    entry: JsonObject, report: JsonObject, first: int = 1
) -> list[JsonObject]:
    """The Standards and Spec findings of a retried code-review report as brief items (no I/O).

    They reach only the developer-retry that answers the review: the next review judges the new
    candidate afresh, or, as a delta-review after that fix-forward, accounts for their closure.
    They are numbered from ``first``, after the review findings the review's own brief carried.
    """
    review = report["review"]
    findings = [
        (axis, finding)
        for axis in ("standards", "spec")
        for finding in review[axis]["findings"]
    ]
    return [
        {
            "item_id": f"{REVIEW_FINDING}-{number}",
            "source": {
                "kind": REVIEW_FINDING,
                "dispatch_id": entry["dispatch_id"],
                "report_sha256": entry["report_sha256"],
                "axis": axis,
                "severity": finding["severity"],
            },
            "summary": finding["summary"],
            "files": [],
            "expected_evidence": finding["evidence"],
        }
        for number, (axis, finding) in enumerate(findings, start=first)
    ]


def _review_handoff(
    dispatch: JsonObject, report: JsonObject
) -> tuple[list[JsonObject], list[JsonObject], int]:
    """What a retried code-review hands on from its own brief (issue #625), with no I/O.

    A delta-review after a fix-forward carries the review findings and the developer's incomplete
    items that fix-forward closed. Those its review did not mark ``closed`` go to the next
    developer-retry again. Returns them by kind and the highest review-finding number its brief
    carried. A brief from before issue #625 never carried either kind, so it hands on nothing.
    """
    section = dispatch.get("carried_items") or {}
    closed = {
        entry["item_id"]
        for entry in report["review"].get("carried_items", [])
        if entry["status"] == "closed"
    }
    findings = section.get(REVIEW_FINDING, [])
    incomplete = [
        item
        for item in section.get(INCOMPLETE_ITEM, [])
        if item["source"].get("target_role") == "developer"
        and item["item_id"] not in closed
    ]
    highest = max(
        (int(item["item_id"].rsplit("-", 1)[1]) for item in findings), default=0
    )
    return (
        [item for item in findings if item["item_id"] not in closed],
        incomplete,
        highest,
    )


def next_incomplete_item_ids(batch: JsonObject, count: int) -> list[str]:
    """The ids the next ``count`` incomplete items of this batch get (issue #501).

    They continue the numbering of every incomplete-item id a decision of the batch recorded, so
    a preview names exactly the ids the decision then records.
    """
    recorded = sum(
        1
        for decision in batch.get("coordinator_decisions", [])
        if isinstance(decision.get("routing"), dict)
        for item_id in decision["routing"].get("carried_item_ids", [])
        if str(item_id).startswith(f"{INCOMPLETE_ITEM}-")
    )
    return [
        f"{INCOMPLETE_ITEM}-{number}"
        for number in range(recorded + 1, recorded + count + 1)
    ]


def incomplete_carry_routing(
    stage: str, candidate: str | None, items: list[JsonObject], item_ids: list[str]
) -> JsonObject:
    """The carry-over routing record of an accept with ``--carry-incomplete`` (no I/O).

    Like a coordinator finding's carry-over, it names no reason, starts no dispatch and is never
    applied to ``next_action``: it names the step the accept itself takes. Each item goes to its
    own target role's brief. Its rationale holds structural facts only.
    """
    next_role, next_action = ACCEPT_NEXT_STEP[stage]
    targets = sorted({item["target_role"] for item in items})
    return {
        "route": _require_route("carry-over"),
        "previous_role": stage,
        "reason_category": None,
        "next_role": next_role,
        "next_action": next_action,
        "candidate_commit": candidate,
        "carried_item_ids": item_ids,
        "rationale": f"the accepted {stage} report carries incomplete items "
        f"{', '.join(item_ids)} into the {', '.join(targets)} briefs; each item stays open until "
        "a dispatch of its target role that carried it is accepted, and no retry is spent.",
    }


def _hands_incomplete_items(entry: JsonObject) -> bool:
    """Whether a decided dispatch's routing record hands its report's incomplete items on."""
    decision = entry.get("decision")
    routing = decision.get("routing") if isinstance(decision, dict) else None
    return (
        isinstance(routing, dict)
        and routing.get("route") in INCOMPLETE_ITEM_ROUTES
        and any(
            str(item_id).startswith(f"{INCOMPLETE_ITEM}-")
            for item_id in routing.get("carried_item_ids", [])
        )
    )


def _incomplete_brief_items(
    root: Path, batch: JsonObject, entry: JsonObject
) -> list[JsonObject]:
    """The brief items of the incomplete items a decided read-only report listed.

    They are read from the immutable, hash-checked report under the ids its routing record
    names. A carry-over hands each item to its own target role; a narrowed retry hands every
    item to the same stage again.
    """
    routing = entry["decision"]["routing"]
    report = _pending_report(root, batch, entry)
    stage = report["role"]
    rows = []
    for item_id, item in zip(
        routing["carried_item_ids"], report["incomplete_items"], strict=True
    ):
        target = item["target_role"] if routing["route"] == "carry-over" else stage
        rows.append(
            {
                "item_id": item_id,
                "source": {
                    "kind": INCOMPLETE_ITEM,
                    "dispatch_id": entry["dispatch_id"],
                    "report_sha256": entry["report_sha256"],
                    "role": stage,
                    "target_role": target,
                    "route": routing["route"],
                    "reason": item["reason"],
                    "reason_category": TOOLING_REASON_CATEGORY
                    if "tooling_blocker" in item
                    else None,
                },
                "summary": item["brief_item"],
                "files": [],
                "expected_evidence": f"The {target} completion report shows this brief "
                "item done.",
            }
        )
    return rows


def _accepted(entry: JsonObject) -> bool:
    decision = entry.get("decision")
    return isinstance(decision, dict) and decision.get("decision") in {
        "accept",
        "override-warning",
    }


def open_incomplete_items(root: Path, batch: JsonObject, role: str) -> list[JsonObject]:
    """The carried-over incomplete items for ``role`` that no accepted ``role`` dispatch carried.

    An item stays open until a dispatch of its target role whose brief carried it is accepted or
    warning-overridden; a retried dispatch leaves it open, so the next brief of that role carries
    it again.
    """
    settled = {
        item["item_id"]
        for entry in batch.get("dispatches", [])
        if entry.get("role") == role and _accepted(entry)
        for item in (
            _load_dispatch(root, entry["dispatch_id"]).get("carried_items") or {}
        ).get(INCOMPLETE_ITEM, [])
    }
    return [
        item
        for entry in batch.get("dispatches", [])
        if _accepted(entry) and _hands_incomplete_items(entry)
        for item in _incomplete_brief_items(root, batch, entry)
        if item["source"]["target_role"] == role and item["item_id"] not in settled
    ]


def _narrowed_items(root: Path, batch: JsonObject, role: str) -> list[JsonObject]:
    """The incomplete items of the read-only report a pending narrowed retry of ``role`` answers.

    They reach only that retry's brief: any later dispatch of the stage runs its whole assignment.
    """
    previous = next(
        (
            item
            for item in reversed(batch.get("dispatches", []))
            if isinstance(item.get("decision"), dict)
        ),
        None,
    )
    if (
        previous is None
        or previous["decision"].get("decision") != "retry"
        or not _hands_incomplete_items(previous)
        or previous["decision"]["routing"].get("next_role") != role
    ):
        return []
    return _incomplete_brief_items(root, batch, previous)


def retry_section(root: Path, batch: JsonObject, entry: JsonObject) -> JsonObject:
    """The closed list of carried items a retry of ``entry`` hands to its developer-retry (#503).

    A retried developer work report accepted none of its brief's items, so the retry owes exactly
    that brief's section. Any other retried report hands on the open coordinator findings, a
    code-review's Standards and Spec findings, and the open incomplete items handed to the
    developer; a retried delta-review also hands on the review findings and developer items its
    brief carried and it did not close (issue #625). Nothing can change these lists between the
    retry decision and the brief, so the decision records their ids and the brief carries the same
    list.
    """
    if entry.get("role") == "developer":
        dispatch = _load_dispatch(root, entry["dispatch_id"])
        if dispatch.get("purpose", "work") == "work":
            carried = dispatch.get("carried_items") or {}
            return {
                kind: carried[kind]
                for kind in CARRIED_ITEM_SOURCES
                if carried.get(kind)
            }
    section: JsonObject = {}
    findings = [
        _brief_item(record) for record in open_coordinator_findings(root, batch)
    ]
    if findings:
        section[COORDINATOR_FINDING] = findings
    incomplete = open_incomplete_items(root, batch, "developer")
    if entry.get("role") == "code-review":
        report = _pending_report(root, batch, entry)
        handed, handed_incomplete, highest = _review_handoff(
            _load_dispatch(root, entry["dispatch_id"]), report
        )
        review_findings = [*handed, *_review_findings(entry, report, highest + 1)]
        if review_findings:
            section[REVIEW_FINDING] = review_findings
        known = {item["item_id"] for item in incomplete}
        incomplete += [
            item for item in handed_incomplete if item["item_id"] not in known
        ]
    if incomplete:
        section[INCOMPLETE_ITEM] = incomplete
    return section


def section_item_ids(section: JsonObject) -> list[str]:
    """The item ids of a carried-items section, in channel order."""
    return [
        item["item_id"]
        for kind in CARRIED_ITEM_SOURCES
        for item in section.get(kind, [])
    ]


def _developer_retry_section(root: Path, batch: JsonObject) -> JsonObject | None:
    """The section of the developer-retry brief a pending retry decision routed, or ``None``.

    A decision recorded since issue #503 names the ids it routed as ``retry_item_ids``; the brief
    must carry exactly them. An earlier decision names none, and its section is taken as computed.
    """
    if batch.get("next_action") != "developer-retry":
        return None
    previous = next(
        (
            item
            for item in reversed(batch.get("dispatches", []))
            if isinstance(item.get("decision"), dict)
        ),
        None,
    )
    if previous is None or previous["decision"].get("decision") != "retry":
        return None
    section = retry_section(root, batch, previous)
    routing = previous["decision"].get("routing")
    recorded = routing.get("retry_item_ids") if isinstance(routing, dict) else None
    if recorded is not None and recorded != section_item_ids(section):
        raise CoordinatorError(
            f"the developer-retry brief would carry {section_item_ids(section)}, but the retry "
            f"decision on {previous['dispatch_id']} recorded {recorded}",
            remedy="the carried items changed after the retry decision -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    return section


def brief_section(root: Path, batch: JsonObject, role: str, purpose: str) -> JsonObject:
    """The ``carried_items`` section of the brief about to be created; ``{}`` carries nothing.

    A code-review or developer work brief carries every open coordinator finding. A developer-retry
    brief carries the closed list its retry decision routed (``retry_section``), so the one retry
    closes the review's findings and the open coordinator findings together. Any work brief
    carries the open incomplete items a read-only report handed to its role, and a narrowed
    retry's brief the items it re-runs (issue #501).
    """
    if purpose != "work":
        return {}
    if role == "developer":
        retried = _developer_retry_section(root, batch)
        if retried is not None:
            return retried
    section: JsonObject = {}
    if role in {"developer", "code-review"}:
        findings = [
            _brief_item(record) for record in open_coordinator_findings(root, batch)
        ]
        if findings:
            section[COORDINATOR_FINDING] = findings
    incomplete = [
        *_narrowed_items(root, batch, role),
        *open_incomplete_items(root, batch, role),
    ]
    if incomplete:
        section[INCOMPLETE_ITEM] = incomplete
    return section


def _carried(dispatch: JsonObject) -> list[JsonObject]:
    """Every item a brief carried, in channel order; a brief before issue #499 carried none."""
    section = dispatch.get("carried_items") or {}
    return [item for items in section.values() for item in items]


def validate_review_accounting(review: JsonObject, dispatch: JsonObject) -> None:
    """A code-review report's optional ``review.carried_items`` names only items its brief carried.

    Each named item appears once as ``{item_id, status, evidence}``. Leaving an item out is
    structurally valid: it is a carried gap the decision has to face, never a refused submission.
    """
    accounting = review.get("carried_items", [])
    known = {item["item_id"] for item in _carried(dispatch)}
    remedy = (
        "account for each item of the brief's carried_items once, as {item_id, status: "
        f"{' | '.join(CARRIED_ITEM_STATUSES)}, evidence}}, and name no other item"
    )
    if not isinstance(accounting, list):
        raise CoordinatorError(
            "composite review carried_items must be a list", remedy=remedy
        )
    seen: set[str] = set()
    for position, entry in enumerate(accounting, start=1):
        if not isinstance(entry, dict) or set(entry) != CARRIED_ITEM_ACCOUNTING_FIELDS:
            raise CoordinatorError(
                f"composite review carried_items entry {position} has an invalid schema",
                remedy=remedy,
            )
        item_id = entry["item_id"]
        if item_id not in known or item_id in seen:
            raise CoordinatorError(
                f"composite review carried_items entry {position} names {item_id!r}, which "
                "the brief did not carry or the review already accounted for",
                remedy=remedy,
            )
        if entry["status"] not in CARRIED_ITEM_STATUSES or not _non_empty(
            entry["evidence"]
        ):
            raise CoordinatorError(
                f"composite review carried_items entry {position} needs a known status and "
                "non-empty evidence",
                remedy=remedy,
            )
        seen.add(item_id)


def _closure(report: JsonObject | None) -> dict[str, JsonObject]:
    """A developer report's carried_item_closure records by item id; ``{}`` without one."""
    records = (
        report.get(CLOSURE_FIELD)
        if report is not None and report.get("role") == "developer"
        else None
    )
    return {
        record["item_id"]: record
        for record in (records if isinstance(records, list) else [])
        if isinstance(record, dict)
    }


def _omits_closure(report: JsonObject | None, dispatch: JsonObject) -> bool:
    """Whether a completed report of a brief that owes the closure carries none: one recorded
    before issue #503, since ``report submit`` refuses such a report now (``require_closure``)."""
    return (
        report is not None
        and report.get("role") == "developer"
        and report.get("outcome") == "completed"
        and CLOSURE_FIELD not in report
        and _closure_owed(dispatch)
    )


def packet_items(dispatch: JsonObject, report: JsonObject | None) -> list[JsonObject]:
    """The carried items a decision packet shows; for a code-review report, with the status the
    review gave each one (``omitted`` when it named none); for a developer-retry report, ``closed``
    with its closing commits or ``open`` with the reason, from its carried_item_closure, and
    ``omitted`` for every item of a completed report recorded without one."""
    reviewed = report is not None and report.get("role") == "code-review"
    review = report.get("review") if reviewed and report is not None else None
    given = {
        entry["item_id"]: entry
        for entry in (
            review.get("carried_items", []) if isinstance(review, dict) else []
        )
    }
    closure = _closure(report)
    omitted = _omits_closure(report, dispatch)
    rows = []
    for item in _carried(dispatch):
        entry = given.get(item["item_id"])
        closed = closure.get(item["item_id"])
        if reviewed:
            status = entry["status"] if entry else "omitted"
            evidence = entry["evidence"] if entry else None
        elif closed is not None:
            status = "open" if "not_closed" in closed else "closed"
            evidence = closed.get("not_closed") or ", ".join(closed["commits"])
        elif omitted:
            status, evidence = "omitted", None
        else:
            status, evidence = None, None
        rows.append(
            {
                "item_id": item["item_id"],
                "source": item["source"],
                "summary": item["summary"],
                "status": status,
                "evidence": evidence,
            }
        )
    return rows


def carried_gap(report: JsonObject, dispatch: JsonObject) -> list[str]:
    """The carried items a report did not close: for a code-review report, omitted, unverified or
    open; for a developer-retry report, those its carried_item_closure records as not_closed, or
    every item when a completed report recorded before issue #503 has no carried_item_closure.

    A gap is never clean: no policy accepts it, and a plain ``accept`` is refused.
    """
    rows = packet_items(dispatch, report)
    if report.get("role") == "code-review":
        return [row["item_id"] for row in rows if row["status"] != "closed"]
    return [row["item_id"] for row in rows if row["status"] in {"open", "omitted"}]


def _closure_owed(dispatch: JsonObject) -> bool:
    """Whether a brief is a developer-retry that carried items, so its report maps their closure."""
    return (
        dispatch.get("role") == "developer"
        and plan_rules.is_developer_retry(dispatch)
        and bool(_carried(dispatch))
    )


def check_closure(report: JsonObject, dispatch: JsonObject) -> None:
    """A developer-retry report's ``carried_item_closure`` maps every carried item once (#503).

    Each record is ``{item_id, commits}`` (the commits that close the item) or
    ``{item_id, not_closed}`` (why it is not closed). Only a developer-retry report whose brief
    carried items may carry it; that a completed one must is checked at submit by
    ``require_closure``. That the commits belong to the retry chain is checked against Git by
    ``check_closure_commits``.
    """
    if CLOSURE_FIELD not in report:
        return
    if not _closure_owed(dispatch):
        raise CoordinatorError(
            "completion report carried_item_closure belongs only to a developer-retry report "
            "whose brief carried items",
            remedy="drop carried_item_closure: only a developer-retry whose brief carried items "
            "maps each item to the commits that close it",
        )
    known = [item["item_id"] for item in _carried(dispatch)]
    closure = report[CLOSURE_FIELD]
    if not isinstance(closure, list):
        raise CoordinatorError(
            "completion report carried_item_closure must be a list",
            remedy=CLOSURE_REMEDY,
        )
    seen: list[str] = []
    for position, record in enumerate(closure, start=1):
        label = f"completion report carried_item_closure entry {position}"
        if not isinstance(record, dict) or set(record) not in (
            CARRIED_ITEM_CLOSED_FIELDS,
            CARRIED_ITEM_NOT_CLOSED_FIELDS,
        ):
            raise CoordinatorError(
                f"{label} must be {{item_id, commits}} or {{item_id, not_closed}}",
                remedy=CLOSURE_REMEDY,
            )
        item_id = record["item_id"]
        if item_id not in known or item_id in seen:
            raise CoordinatorError(
                f"{label} names {item_id!r}, which the brief did not carry or the report "
                "already mapped",
                remedy=CLOSURE_REMEDY,
            )
        seen.append(item_id)
        if "not_closed" in record:
            if not _non_empty(record["not_closed"]):
                raise CoordinatorError(
                    f"carried item {item_id} is not_closed without a reason",
                    remedy=f"state in not_closed why {item_id} is not closed, or list the "
                    "commits that close it",
                )
            continue
        commits = record["commits"]
        if (
            not isinstance(commits, list)
            or not commits
            or not all(isinstance(sha, str) for sha in commits)
        ):
            raise CoordinatorError(
                f"carried item {item_id} commits must be a non-empty list of commit SHAs",
                remedy=CLOSURE_REMEDY,
            )
    missing = [item_id for item_id in known if item_id not in seen]
    if missing:
        raise CoordinatorError(
            f"completion report carried_item_closure has no record for carried items {missing}",
            remedy=f"add a record for {', '.join(missing)}: the commits that close it, or "
            "not_closed with the reason",
        )


def require_closure(report: JsonObject, dispatch: JsonObject) -> None:
    """``report submit`` refuses a completed developer-retry report without carried_item_closure.

    Only a new report is refused: a report recorded before issue #503 is still decided, with every
    carried item an ``omitted`` carried gap, so ``batch decide`` never re-raises this.
    """
    if _omits_closure(report, dispatch):
        raise CoordinatorError(
            f"completion report carried_item_closure is missing: the developer-retry brief "
            f"carried items {[item['item_id'] for item in _carried(dispatch)]}",
            remedy=CLOSURE_REMEDY,
        )


def _retried_attempt(
    root: Path, entries: list[JsonObject], brief: JsonObject
) -> JsonObject | None:
    """The developer work brief whose retry created ``brief`` with the same carried items, or
    ``None``. The retried entry is the last decided one before the brief's own entry."""
    position = next(
        (
            index
            for index, item in enumerate(entries)
            if item.get("dispatch_id") == brief.get("dispatch_id")
        ),
        0,
    )
    retried = next(
        (
            item
            for item in reversed(entries[:position])
            if isinstance(item.get("decision"), dict)
        ),
        None,
    )
    if (
        retried is None
        or retried.get("role") != "developer"
        or retried["decision"].get("decision") != "retry"
    ):
        return None
    previous = _load_dispatch(root, retried["dispatch_id"])
    if previous.get("purpose", "work") != "work" or previous.get(
        "carried_items"
    ) != brief.get("carried_items"):
        return None
    return previous


def closure_snapshots(root: Path, batch: JsonObject, dispatch: JsonObject) -> list[str]:
    """The snapshots of the retry chain whose commits a carried_item_closure may name (#503),
    oldest first and ending with the dispatch's own ``snapshot_commit``; ``[]`` when none is owed.

    A retried developer report, or a developer tooling-retry, hands the same closed list to the
    next attempt, whose ``snapshot_commit`` is the retried attempt's HEAD. An item an earlier
    attempt closed stays closed by that attempt's commit, so the chain reaches back through every
    retried developer work brief that carried the same list.
    """
    if not _closure_owed(dispatch):
        return []
    entries = batch.get("dispatches", [])
    chain: list[str] = []
    brief: JsonObject | None = dispatch
    while brief is not None and isinstance(brief.get("snapshot_commit"), str):
        chain.insert(0, brief["snapshot_commit"])
        brief = _retried_attempt(root, entries, brief)
    return chain


def check_closure_commits(
    report: JsonObject, created: list[str], resolve: Callable[[str], str]
) -> None:
    """Every commit a carried_item_closure names was created by its retry chain (issue #503).

    ``created`` is the ordered list of commits after the chain's base (``closure_snapshots``; the
    rebase target for a rebase) up to the reported HEAD; ``resolve`` turns a reported SHA into its
    full form.
    """
    for item_id, record in _closure(report).items():
        commits = record.get("commits") or []
        claimed = [
            plan_rules._resolve_reported(resolve, sha, "carried_item_closure[].commits")
            for sha in commits
        ]
        foreign = [sha for sha, full in zip(commits, claimed) if full not in created]
        if foreign:
            raise CoordinatorError(
                f"carried item {item_id} names commits that neither this dispatch nor an earlier "
                f"attempt of its retry chain created: {foreign}",
                remedy="list only the commits that close the item after snapshot_commit up to "
                "commit_sha, or after the snapshot of the first retry attempt that carried the "
                "same list: a fix-forward closes it with new commits on top of the candidate",
            )


def marks_open(report: JsonObject) -> bool:
    """Whether a review confirms a carried item is still open: structured evidence for a developer."""
    review = report.get("review")
    accounting = review.get("carried_items") if isinstance(review, dict) else None
    return isinstance(accounting, list) and any(
        isinstance(entry, dict) and entry.get("status") == "open"
        for entry in accounting
    )


def section_sha256(section: JsonObject) -> str | None:
    """The digest a transition binds for a non-empty section; ``None`` binds nothing."""
    return operational_guards.carried_items_digest(section) if section else None


def _accepted_developer_work(root: Path, entry: JsonObject) -> bool:
    decision = entry.get("decision")
    return (
        entry.get("role") == "developer"
        and entry.get("state") == "reported"
        and isinstance(decision, dict)
        and decision.get("decision") in {"accept", "override-warning"}
        and _load_dispatch(root, entry["dispatch_id"]).get("purpose") == "work"
    )


def _refuse_after(root: Path, entry: JsonObject) -> CoordinatorError:
    """The refusal once a dispatch was created after the accepted developer report."""
    candidate = _load_dispatch(root, entry["dispatch_id"]).get("candidate_commit")
    message = (
        f"{entry['role']} dispatch {entry['dispatch_id']} for candidate {candidate} was "
        "already created after the accepted developer report, so its brief cannot carry new "
        "coordinator findings"
    )
    if entry.get("state") == "approved":
        remedy = (
            f"cancel the unsent dispatch with dispatch cancel --dispatch {entry['dispatch_id']} "
            "--approved-by <name> --approved-at <ISO-8601> --reason <why>, then run batch "
            "carry-over again"
        )
    elif isinstance(entry.get("decision"), dict):
        remedy = (
            f"that dispatch's report is already decided "
            f"({entry['decision'].get('decision')}); attach the finding with batch decide "
            "--findings-file <path> when accepting the next developer report"
        )
    else:
        remedy = (
            "report the defect when deciding that dispatch's report: a retry routes a "
            "developer-retry whose brief carries the review findings and every open "
            "coordinator finding"
        )
    return CoordinatorError(message, remedy=remedy)


def _carry_over_target(root: Path, batch: JsonObject) -> JsonObject:
    """The accepted developer work report ``batch carry-over`` attaches findings to.

    It is the batch's last accepted developer work report, and nothing but a cancelled or abandoned
    dispatch may follow it (``batch resume`` abandons a stale or blocked one, which never reports): a created code-review dispatch (or a qa dispatch a policy chain created) already
    holds the brief the findings would have to be in.
    """
    entries = batch.get("dispatches", [])
    if any(
        item.get("state") == "reported" and "decision" not in item for item in entries
    ):
        raise CoordinatorError(
            "batch carry-over requires a batch with no completion report awaiting a decision",
            remedy="decide the pending report first; a developer report accepted with batch "
            "decide --findings-file <path> carries the findings in that same decision",
        )
    position = next(
        (
            index
            for index in range(len(entries) - 1, -1, -1)
            if _accepted_developer_work(root, entries[index])
        ),
        None,
    )
    if position is None:
        raise CoordinatorError(
            "batch carry-over requires an accepted developer work report in this batch",
            remedy="accept a developer work report first; carry-over attaches findings to it "
            "before its code-review dispatch is created",
        )
    following = [
        item
        for item in entries[position + 1 :]
        if item.get("state") not in {"abandoned", "cancelled"}
    ]
    if following:
        raise _refuse_after(root, following[0])
    if batch.get("state") != "awaiting-approval":
        raise CoordinatorError(
            f"batch carry-over requires a batch awaiting approval, not {batch.get('state')!r}",
            remedy="carry findings over only while the batch awaits its next dispatch",
        )
    return cast(JsonObject, entries[position])


def carry_over_routing(candidate: str, item_ids: list[str]) -> JsonObject:
    """The routing record of a carry-over (no I/O).

    It has a retry record's shape, but the decision carrying it is an ``accept`` or a
    ``carry-over``: it names no reason, starts no dispatch and spends no developer retry, so
    ``batch decide`` never applies it to ``next_action``. Its rationale holds structural facts only.
    """
    return {
        "route": _require_route("carry-over"),
        "previous_role": "developer",
        "reason_category": None,
        "next_role": "code-review",
        "next_action": "code-review",
        "candidate_commit": candidate,
        "carried_item_ids": item_ids,
        "rationale": f"the accepted developer report of candidate {candidate} carries "
        f"coordinator findings {', '.join(item_ids)} into code-review; the candidate is "
        "unchanged and no developer retry is spent.",
    }


def carry_over_preview(
    repo: Path, root: Path, batch: JsonObject, findings_file: str
) -> JsonObject:
    """The routing record ``batch carry-over`` would record now; it writes nothing."""
    findings = read_findings(repo, findings_file)
    entry = _carry_over_target(root, batch)
    report = _pending_report(root, batch, entry)
    return carry_over_routing(
        _candidate_commit(repo, report["commit_sha"]),
        next_item_ids(batch, len(findings)),
    )


def carry_over_findings(args: argparse.Namespace) -> JsonObject:
    """Attach coordinator findings to an already accepted developer report (``batch carry-over``).

    This is the route after a policy auto-accept, which never takes a findings file. It starts no
    dispatch and moves no candidate, so the coordinator records it under ``policy:carry-over``; it
    only adds review obligations, and a batch bound for qa goes to code-review instead. Under an
    active ``approval_policy: auto`` it is recorded as ``policy:auto`` (issue #643).
    """
    repo = _repo(args)
    root = _state_root(args, repo)
    findings = read_findings(repo, args.findings_file)
    try:
        config = core_config._config(repo)
    except CoordinatorError:
        config = {}
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        batch = _load_batch(root, args.batch)
        _validate_batch_integrity(root, batch)
        entry = _carry_over_target(root, batch)
        report = _pending_report(root, batch, entry)
        candidate = _candidate_commit(repo, report["commit_sha"])
        moment = utils._now()
        auto = approvals.auto_active(config, batch)
        policy = approvals.AUTO_POLICY if auto else CARRY_OVER_POLICY
        approved_by = f"policy:{policy}"
        records = attach(
            batch,
            findings,
            source=coordinator_source(entry, candidate),
            attached_at=moment,
            attached_by=approved_by,
        )
        routing = carry_over_routing(
            candidate, [record["item_id"] for record in records]
        )
        if batch.get("next_action") == "qa":
            batch["next_action"] = "code-review"
        decision: JsonObject = {
            "dispatch_id": entry["dispatch_id"],
            "decision": "carry-over",
            "approved_by": approved_by,
            "approved_at": moment,
            "note": f"{len(records)} coordinator finding(s) carried into code-review",
            "routing": routing,
        }
        batch.setdefault("coordinator_decisions", []).append(decision)
        if auto:
            approvals.record_auto(
                batch,
                kind="carry-over",
                dispatch_id=entry["dispatch_id"],
                rationale=decision["note"],
                evidence={
                    "report_sha256": entry["report_sha256"],
                    "carried_item_ids": routing["carried_item_ids"],
                },
                moment=moment,
            )
        _safe_id(batch["batch_id"], "batch")
        _replace_record(
            ledger,
            BatchRecord.from_dict(batch),
            decision={
                "dispatch_id": entry["dispatch_id"],
                "decision": "carry-over",
                "route": routing["route"],
                "evidence": {
                    "dispatch_id": entry["dispatch_id"],
                    "report": entry["report"],
                    "report_sha256": entry["report_sha256"],
                },
                "approver": {"kind": "policy", "name": policy},
                "approved_at": moment,
            },
        )
    return {
        "batch_id": batch["batch_id"],
        "dispatch_id": entry["dispatch_id"],
        "carried_item_ids": [record["item_id"] for record in records],
        "next_action": batch.get("next_action"),
    }
