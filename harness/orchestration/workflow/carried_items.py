"""Carried items: obligations a decision hands on to a later stage's immutable brief (issue #499).

A defect the coordinator finds in a clean developer report does not cost a developer retry before
review. The report is accepted with a structured coordinator finding, which the batch records
append-only, and every later code-review brief carries each open finding in its ``carried_items``
section until a review that carried it is accepted. The section is one shared channel keyed by the
kind of source that raised an item, so a later kind joins it without changing its shape.

An item's status is derived, never stored: it stays open until a code-review dispatch whose brief
carried it is accepted or warning-overridden; a retried review leaves it open.

This module reads the ledger through ``history`` and ``ledger_ops`` only; the decision, dispatch,
report and risk modules call into it, never the other way round.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path, PurePosixPath
from typing import cast

from harness.orchestration import operational_guards
from harness.orchestration.core import utils
from harness.orchestration.core.config import _reject_sensitive
from harness.orchestration.core.constants import (
    CARRIED_ITEM_ACCOUNTING_FIELDS,
    CARRIED_ITEM_FIELDS,
    CARRIED_ITEM_STATUSES,
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
from harness.orchestration.workflow.history import (
    _pending_report,
    _require_route,
    _validate_batch_integrity,
)

COORDINATOR_FINDING = "coordinator-finding"
REVIEW_FINDING = "review-finding"
# ``batch carry-over`` starts no dispatch and moves no candidate, so the coordinator runs it under
# this policy name, the way ``batch resume`` records ``policy:operational-recovery``.
CARRY_OVER_POLICY = "carry-over"
FINDING_FIELDS = frozenset({"summary", "files", "expected_evidence"})
FINDINGS_FILE_REMEDY = (
    'write the findings file as {"findings": [{"summary": ..., "files": [...], '
    '"expected_evidence": ...}, ...]} in English and pass it again with --findings-file'
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


def _retried_review_findings(root: Path, batch: JsonObject) -> list[JsonObject]:
    """The Standards and Spec findings of the code-review a pending developer-retry answers.

    They reach only that developer brief: the next review judges the new candidate afresh.
    """
    previous = next(
        (
            item
            for item in reversed(batch.get("dispatches", []))
            if isinstance(item.get("decision"), dict)
        ),
        None,
    )
    routing = previous["decision"].get("routing") if previous else None
    if (
        previous is None
        or previous.get("role") != "code-review"
        or previous["decision"].get("decision") != "retry"
        or not isinstance(routing, dict)
        or routing.get("next_action") != "developer-retry"
    ):
        return []
    review = _pending_report(root, batch, previous)["review"]
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
                "dispatch_id": previous["dispatch_id"],
                "report_sha256": previous["report_sha256"],
                "axis": axis,
                "severity": finding["severity"],
            },
            "summary": finding["summary"],
            "files": [],
            "expected_evidence": finding["evidence"],
        }
        for number, (axis, finding) in enumerate(findings, start=1)
    ]


def brief_section(root: Path, batch: JsonObject, role: str, purpose: str) -> JsonObject:
    """The ``carried_items`` section of the brief about to be created; ``{}`` carries nothing.

    A code-review or developer work brief carries every open coordinator finding. A developer
    brief answering a retried code-review also carries that review's findings, so the one
    developer-retry closes both.
    """
    if purpose != "work" or role not in {"developer", "code-review"}:
        return {}
    section: JsonObject = {}
    findings = [
        _brief_item(record) for record in open_coordinator_findings(root, batch)
    ]
    if findings:
        section[COORDINATOR_FINDING] = findings
    review_findings = (
        _retried_review_findings(root, batch) if role == "developer" else []
    )
    if review_findings:
        section[REVIEW_FINDING] = review_findings
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


def packet_items(dispatch: JsonObject, report: JsonObject | None) -> list[JsonObject]:
    """The carried items a decision packet shows; for a code-review report, with the status the
    review gave each one (``omitted`` when it named none)."""
    reviewed = report is not None and report.get("role") == "code-review"
    review = report.get("review") if reviewed and report is not None else None
    given = {
        entry["item_id"]: entry
        for entry in (
            review.get("carried_items", []) if isinstance(review, dict) else []
        )
    }
    rows = []
    for item in _carried(dispatch):
        entry = given.get(item["item_id"])
        rows.append(
            {
                "item_id": item["item_id"],
                "source": item["source"],
                "summary": item["summary"],
                "status": (entry["status"] if entry else "omitted")
                if reviewed
                else None,
                "evidence": entry["evidence"] if entry else None,
            }
        )
    return rows


def carried_gap(report: JsonObject, dispatch: JsonObject) -> list[str]:
    """The carried items a code-review report did not close: omitted, unverified or open.

    A gap is never clean: no policy accepts it, and a plain ``accept`` is refused.
    """
    if report.get("role") != "code-review":
        return []
    return [
        row["item_id"]
        for row in packet_items(dispatch, report)
        if row["status"] != "closed"
    ]


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
    only adds review obligations, and a batch bound for qa goes to code-review instead.
    """
    repo = _repo(args)
    root = _state_root(args, repo)
    findings = read_findings(repo, args.findings_file)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        batch = _load_batch(root, args.batch)
        _validate_batch_integrity(root, batch)
        entry = _carry_over_target(root, batch)
        report = _pending_report(root, batch, entry)
        candidate = _candidate_commit(repo, report["commit_sha"])
        moment = utils._now()
        approved_by = f"policy:{CARRY_OVER_POLICY}"
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
                "approver": {"kind": "policy", "name": CARRY_OVER_POLICY},
                "approved_at": moment,
            },
        )
    return {
        "batch_id": batch["batch_id"],
        "dispatch_id": entry["dispatch_id"],
        "carried_item_ids": [record["item_id"] for record in records],
        "next_action": batch.get("next_action"),
    }
