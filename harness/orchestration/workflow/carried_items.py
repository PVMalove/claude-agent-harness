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

import hashlib
from pathlib import Path, PurePosixPath

from harness.orchestration import operational_guards
from harness.orchestration.core.config import _reject_sensitive
from harness.orchestration.core.constants import CARRIED_ITEM_FIELDS
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _canonical,
    _non_empty,
    _read_object,
)
from harness.orchestration.core.workspace import (
    _agent_authored_file,
    _reject_non_english,
)
from harness.orchestration.ledger.ledger_ops import _load_dispatch

COORDINATOR_FINDING = "coordinator-finding"
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


def brief_section(root: Path, batch: JsonObject, role: str, purpose: str) -> JsonObject:
    """The ``carried_items`` section of the brief about to be created; ``{}`` carries nothing.

    A code-review or developer work brief carries every open coordinator finding.
    """
    if purpose != "work" or role not in {"developer", "code-review"}:
        return {}
    section: JsonObject = {}
    findings = [
        _brief_item(record) for record in open_coordinator_findings(root, batch)
    ]
    if findings:
        section[COORDINATOR_FINDING] = findings
    return section


def section_sha256(section: JsonObject) -> str | None:
    """The digest a transition binds for a non-empty section; ``None`` binds nothing."""
    return operational_guards.carried_items_digest(section) if section else None
