"""Integration accounting after a terminal publish (issue #532).

A completed batch is history: it is never reopened and its reports are never rewritten.  This
module records, next to that history, the link a later integration step needs -- ticket, issue
branch, source batch, published candidate SHA and the integration (target) SHA the candidate was
checked against -- as an immutable ``IntegrationRecord``.  Every operation here only reads the
batch, plan, dispatch and report records and writes records under ``reports/integration*``, so
repeating one neither loses nor duplicates registered work.

Whether the recorded pair is still the current one is an observation, never a decision: the
integration ref moving makes the record ``stale``, which asks for a new check of the new pair and
never lets old QA stand in for it.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from harness.errors import INTERNAL_INVARIANT_REMEDY
from harness.orchestration.core import utils
from harness.orchestration.core.config import _reject_sensitive
from harness.orchestration.core.constants import (
    INTEGRATION_EVIDENCE_KINDS,
    INTEGRATION_EVIDENCE_RESULTS,
    INTEGRATION_RECORD_ID_PATTERN,
    INTEGRATION_REFERENCE_MAX_CHARS,
    INTEGRATION_SHA_PATTERN,
)
from harness.orchestration.core.git_utils import (
    _candidate_commit,
    _git,
    _remote_branch_tip,
)
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _canonical,
    _non_empty,
    _read_object,
    _repo,
    _safe_id,
)
from harness.orchestration.core.workspace import _integration_ref
from harness.orchestration.ledger.ledger_ops import (
    _ledger_lock,
    _load_batch,
    _load_dispatch,
    _records_root,
    _state_root,
    _write_record,
)
from harness.orchestration.ledger.lifecycle import (
    IntegrationEvidenceRecord,
    IntegrationRecord,
    LifecycleLedger,
)
from harness.orchestration.workflow import history

INTEGRATION_RECORD_CONTRACT = 1
_ACCEPTED_DECISIONS = {"accept", "override-warning"}


def _sha256(value: JsonObject) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(args: argparse.Namespace, name: str) -> str:
    value = getattr(args, name, None)
    return value.strip() if _non_empty(value) else ""


def _verified_dispatch(root: Path, entry: JsonObject) -> JsonObject:
    """The dispatch brief of a batch entry, checked against the digest the batch recorded."""
    dispatch = _load_dispatch(root, entry["dispatch_id"])
    if entry.get("brief_sha256") != _sha256(dispatch):
        raise CoordinatorError(
            "dispatch record failed immutable brief integrity check",
            remedy="the dispatch record was modified after its brief integrity hash was recorded -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    return dispatch


def _accepted_publish(root: Path, batch: JsonObject) -> JsonObject | None:
    """The latest accepted publish of ``batch``: its entry, report and candidate, or ``None`` when
    the batch has never published."""
    for entry in reversed(batch.get("dispatches", [])):
        if entry.get("role") != "developer" or entry.get("state") != "reported":
            continue
        if (entry.get("decision") or {}).get("decision") not in _ACCEPTED_DECISIONS:
            continue
        dispatch = _verified_dispatch(root, entry)
        if dispatch.get("purpose") != "publish":
            continue
        report = history._pending_report(root, batch, entry)
        if report.get("outcome") != "completed":
            continue
        candidate = dispatch.get("candidate_commit")
        if report.get("commit_sha") != candidate:
            raise CoordinatorError(
                "the accepted publish report does not name the candidate of its brief",
                remedy="the publish evidence is inconsistent -- "
                + INTERNAL_INVARIANT_REMEDY,
            )
        return {"entry": entry, "candidate": candidate}
    return None


def _select_source(
    root: Path, ticket: str, branch: str, requested: object, candidate: str | None
) -> tuple[JsonObject, JsonObject]:
    """The one terminal batch that published this ticket branch -- never a substitute for it."""
    batches = history._batches_for_ticket_branch(root, ticket, branch)
    if requested is not None:
        batch_id = _safe_id(requested, "batch")
        batches = [batch for batch in batches if batch.get("batch_id") == batch_id]
        if not batches:
            raise CoordinatorError(
                "requested batch does not match the ticket and issue branch",
                remedy="pass a --batch that belongs to this --ticket and --branch (see 'batch list')",
            )
    published = []
    for batch in batches:
        proof = (
            _accepted_publish(root, batch)
            if batch.get("state") == "completed"
            else None
        )
        if proof is not None:
            published.append((batch, proof))
    if not published:
        raise CoordinatorError(
            "no completed batch has an accepted publish for this ticket and issue branch",
            remedy="finish the publish dispatch and accept its report so the batch completes; "
            "integration prepare publishes nothing itself",
        )
    if candidate is not None:
        narrowed = [item for item in published if item[1]["candidate"] == candidate]
        if not narrowed:
            known = ", ".join(sorted({item[1]["candidate"] for item in published}))
            raise CoordinatorError(
                f"candidate {candidate} is not the published commit of this ticket branch ({known})",
                remedy="pass the candidate SHA from the accepted publish report, or omit --candidate-commit",
            )
        published = narrowed
    if len(published) > 1:
        ids = ", ".join(str(item[0]["batch_id"]) for item in published)
        raise CoordinatorError(
            f"several batches published this ticket branch ({ids}); no batch is chosen for you",
            remedy="pass --batch with the batch whose publish you are integrating",
        )
    return published[0]


def _evidence_reference(root: Path, entry: JsonObject) -> JsonObject:
    return {
        "dispatch_id": entry["dispatch_id"],
        "brief_sha256": entry["brief_sha256"],
        "report_path": entry["report"],
        "report_sha256": entry["report_sha256"],
    }


def _source_facts(
    root: Path, batch: JsonObject, publish: JsonObject, target_sha: str
) -> JsonObject:
    """The deterministic ledger facts an integration record links: no timestamps, only references
    to immutable records by digest, so recomputing them later detects any rewriting of history."""
    candidate = publish["candidate"]
    try:
        qa_report = history._accepted_qa_for_candidate(root, batch, candidate)
    except CoordinatorError as exc:
        raise CoordinatorError(
            "integration prepare requires accepted green QA evidence for the published candidate",
            remedy="accept green QA evidence for the candidate commit; prepare does not run QA",
        ) from exc
    qa_entry = next(
        (
            item
            for item in batch["dispatches"]
            if item.get("dispatch_id") == qa_report.get("dispatch_id")
        ),
        None,
    )
    if qa_entry is None:
        raise CoordinatorError(
            "accepted QA report is not linked to a dispatch of its batch",
            remedy="the QA evidence is inconsistent -- " + INTERNAL_INVARIANT_REMEDY,
        )
    _verified_dispatch(root, qa_entry)
    checks = [
        {"command": check.get("command"), "result": check.get("result")}
        for check in qa_report.get("checks_run", [])
        if isinstance(check, dict)
    ]
    return {
        "batch_state": batch["state"],
        "publish": _evidence_reference(root, publish["entry"]),
        "qa": {
            **_evidence_reference(root, qa_entry),
            "candidate_commit": candidate,
            "outcome": qa_report.get("outcome"),
            "checks_run": checks,
        },
        "pair": {"candidate_sha": candidate, "target_sha": target_sha},
    }


def _record_path(root: Path, record_id: str) -> Path:
    return _records_root(root) / IntegrationRecord.directory / f"{record_id}.json"


def _observe(repo: Path, identity: JsonObject) -> JsonObject:
    """Where the integration ref points now, compared with the recorded target.  A remote that
    cannot answer is ``unavailable``, which -- like ``stale`` -- asks for a refresh."""
    try:
        tip = _remote_branch_tip(repo, identity["remote"], identity["integration_ref"])
    except CoordinatorError as exc:
        return {"state": "unavailable", "integration_tip": None, "reason": exc.message}
    if tip is None:
        return {
            "state": "unavailable",
            "integration_tip": None,
            "reason": f"{identity['integration_ref']!r} is not a branch of remote {identity['remote']!r}",
        }
    state = "current" if tip == identity["target_sha"] else "stale"
    return {"state": state, "integration_tip": tip}


def _require_remote(repo: Path, remote: str) -> None:
    if remote not in _git(repo, "remote").splitlines():
        raise CoordinatorError(
            "integration remote is not configured for this repository",
            remedy="pass --remote naming a configured remote, or add it with 'git remote add'",
        )


def _verify_unmoved(repo: Path, identity: JsonObject) -> JsonObject:
    """Both independent proofs the pair is real: the published branch is the candidate on the
    remote, and the integration ref has not moved from the recorded target."""
    remote, branch = identity["remote"], identity["branch"]
    published = _remote_branch_tip(repo, remote, branch)
    if published != identity["candidate_sha"]:
        raise CoordinatorError(
            f"remote branch {branch!r} is {published or 'missing'}, not the published candidate "
            f"{identity['candidate_sha']}",
            remedy="run prepare before the branch is merged or deleted; re-publish the accepted "
            "candidate through a publish dispatch if the remote branch was changed",
        )
    tip = _remote_branch_tip(repo, remote, identity["integration_ref"])
    if tip != identity["target_sha"]:
        raise CoordinatorError(
            f"integration ref {identity['integration_ref']!r} is {tip or 'missing'}, not the "
            f"recorded target {identity['target_sha']}; no record exists for the new pair",
            remedy="run a new developer rebase dispatch and a new QA for the new pair through the "
            "normal route; prepare changes nothing",
        )
    return {"remote_branch_sha": published, "integration_tip": tip}


def _evidence_links(root: Path, record_id: str) -> list[JsonObject]:
    directory = _records_root(root) / "reports" / "integration-evidence"
    links = []
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        link = _read_object(path, "integration evidence record")
        if link.get("integration_record_id") == record_id:
            links.append(link)
    return links


def _prepared(
    record: JsonObject, links: list[JsonObject], observed: JsonObject, *, created: bool
) -> JsonObject:
    identity = record["identity"]
    return {
        "integration_record_id": record["integration_record_id"],
        "created": created,
        "ticket": identity["ticket"],
        "branch": identity["branch"],
        "source_batch_id": identity["source_batch_id"],
        "candidate_sha": identity["candidate_sha"],
        "published_sha": identity["published_sha"],
        "target_sha": identity["target_sha"],
        "integration_ref": identity["integration_ref"],
        "source_evidence": record["source"],
        "evidence_links": [link["evidence_id"] for link in links],
        "status": observed,
    }


def integration_prepare(args: argparse.Namespace) -> JsonObject:
    """Record the integration link of one published ticket branch; idempotent."""
    repo = _repo(args)
    root = _state_root(args, repo)
    ticket, branch = _text(args, "ticket"), _text(args, "branch")
    if not ticket or not branch:
        raise CoordinatorError(
            "integration prepare requires non-empty ticket and branch",
            remedy="pass non-empty --ticket and --branch",
        )
    remote = _text(args, "remote") or "origin"
    requested = _text(args, "candidate_commit")
    candidate = _candidate_commit(repo, requested) if requested else None
    _require_remote(repo, remote)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        batch, publish = _select_source(
            root, ticket, branch, getattr(args, "batch", None), candidate
        )
        target = batch.get("integration_base_commit")
        if not _non_empty(target):
            raise CoordinatorError(
                "the source batch recorded no integration base commit",
                remedy="this batch predates integration-base tracking; re-plan it to record one",
            )
        facts = _source_facts(root, batch, publish, target)
        identity: JsonObject = {
            "ticket": ticket,
            "branch": branch,
            "source_batch_id": batch["batch_id"],
            "candidate_sha": publish["candidate"],
            "published_sha": publish["candidate"],
            "integration_ref": _integration_ref(repo, batch),
            "target_sha": target,
            "remote": remote,
        }
        record_id = IntegrationRecord.derive_id(identity)
        path = _record_path(root, record_id)
        if path.is_file():
            record = _read_object(path, "integration record")
            if record.get("source_sha256") != _sha256(facts):
                raise CoordinatorError(
                    "the recorded integration evidence no longer matches the batch history",
                    remedy="the ledger was changed after the record was written -- "
                    + INTERNAL_INVARIANT_REMEDY,
                )
            links = _evidence_links(root, record_id)
            created = False
        else:
            observed = _verify_unmoved(repo, identity)
            record = {
                "integration_record_id": record_id,
                "contract": INTEGRATION_RECORD_CONTRACT,
                "created_at": utils._now(),
                "identity": identity,
                "source": facts,
                "source_sha256": _sha256(facts),
                "observed_at_prepare": {**observed, "observed_at": utils._now()},
            }
            _write_record(ledger, IntegrationRecord.from_dict(record))
            links, created = [], True
    state = (
        {"state": "current", "integration_tip": record["identity"]["target_sha"]}
        if created
        else _observe(repo, record["identity"])
    )
    return _prepared(record, links, state, created=created)


def _load_record(root: Path, record_id: object) -> JsonObject:
    """An integration record that is still exactly what was written: its id derives from its
    identity and its source facts match their digest."""
    if (
        not isinstance(record_id, str)
        or INTEGRATION_RECORD_ID_PATTERN.fullmatch(record_id) is None
    ):
        raise CoordinatorError(
            "integration record id is not valid",
            remedy="pass the integration_record_id that 'integration prepare' returned",
        )
    path = _record_path(root, record_id)
    if not path.is_file():
        raise CoordinatorError(
            f"no integration record {record_id}",
            remedy="run 'integration prepare' for the ticket branch to create the record",
        )
    record = _read_object(path, "integration record")
    if (
        record.get("integration_record_id") != record_id
        or record_id != (IntegrationRecord.derive_id(record.get("identity") or {}))
        or record.get("source_sha256") != _sha256(record.get("source") or {})
    ):
        raise CoordinatorError(
            "the integration record failed its integrity check",
            remedy="the record was modified after it was written -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    return record


def _choice(args: argparse.Namespace, name: str, allowed: tuple[str, ...]) -> str:
    value = _text(args, name)
    if value not in allowed:
        raise CoordinatorError(
            f"{name} must be one of {', '.join(allowed)}",
            remedy=f"pass --{name.replace('_', '-')} as one of: {', '.join(allowed)}",
        )
    return value


def _sha(args: argparse.Namespace, name: str, *, required: bool = True) -> str | None:
    value = _text(args, name)
    if not value and not required:
        return None
    if INTEGRATION_SHA_PATTERN.fullmatch(value) is None:
        raise CoordinatorError(
            f"{name} must be a full lowercase hexadecimal SHA (40 or 64 digits)",
            remedy=f"pass --{name.replace('_', '-')} as the full SHA of the checked commit",
        )
    return value


def integration_link_evidence(args: argparse.Namespace) -> JsonObject:
    """Register a new check of a candidate/target pair against an integration record.

    This is the one public route by which future CI, local-QA and resolver evidence is linked.  It
    records the link as an immutable event next to the record, never inside it: the record's own
    initial (source) evidence stays tied to its original pair, and a registered check applies to
    the pair it names and to no other.  ``verification`` stays ``unverified`` until a later route
    verifies the artifact; registering is not accepting.
    """
    repo = _repo(args)
    root = _state_root(args, repo)
    kind = _choice(args, "kind", INTEGRATION_EVIDENCE_KINDS)
    result = _choice(args, "result", INTEGRATION_EVIDENCE_RESULTS)
    candidate = _sha(args, "candidate_commit")
    target = _sha(args, "target_commit")
    artifact = _sha(args, "artifact_sha256", required=False)
    reference = _text(args, "reference")
    if not reference or len(reference) > INTEGRATION_REFERENCE_MAX_CHARS:
        raise CoordinatorError(
            f"reference must be a non-empty string of at most {INTEGRATION_REFERENCE_MAX_CHARS} characters",
            remedy="pass --reference naming where the check result can be inspected (a run URL or an artifact path)",
        )
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        record = _load_record(root, getattr(args, "record", None))
        members: JsonObject = {
            "integration_record_id": record["integration_record_id"],
            "kind": kind,
            "candidate_sha": candidate,
            "target_sha": target,
            "result": result,
            "reference": reference,
            "artifact_sha256": artifact,
        }
        evidence_id = IntegrationEvidenceRecord.derive_id(members)
        path = (
            _records_root(root)
            / IntegrationEvidenceRecord.directory
            / f"{evidence_id}.json"
        )
        if path.is_file():
            return {
                "evidence_id": evidence_id,
                "integration_record_id": record["integration_record_id"],
                "linked": False,
                "evidence": _read_object(path, "integration evidence record"),
            }
        document: JsonObject = {
            "evidence_id": evidence_id,
            **members,
            "scope": "pair-check",
            "recorded_at": utils._now(),
            "verification": "unverified",
        }
        _reject_sensitive(document, "integration evidence")
        _write_record(ledger, IntegrationEvidenceRecord.from_dict(document))
    return {
        "evidence_id": evidence_id,
        "integration_record_id": record["integration_record_id"],
        "linked": True,
        "evidence": document,
    }


def _resolve_record(root: Path, args: argparse.Namespace) -> JsonObject:
    """The record named by ``--record``, or the only one of ``--ticket`` and ``--branch``."""
    if getattr(args, "record", None) is not None:
        return _load_record(root, args.record)
    ticket, branch = _text(args, "ticket"), _text(args, "branch")
    if not ticket or not branch:
        raise CoordinatorError(
            "integration status needs --record, or both --ticket and --branch",
            remedy="pass --record with the id 'integration prepare' returned, or --ticket and --branch",
        )
    directory = _records_root(root) / IntegrationRecord.directory
    requested = getattr(args, "batch", None)
    matches = []
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        identity = _read_object(path, "integration record").get("identity") or {}
        if (
            identity.get("ticket") == ticket
            and identity.get("branch") == branch
            and (requested is None or identity.get("source_batch_id") == requested)
        ):
            matches.append(path.stem)
    if not matches:
        raise CoordinatorError(
            "no integration record exists for the ticket branch",
            remedy="run 'integration prepare' for the published ticket branch first",
        )
    if len(matches) > 1:
        raise CoordinatorError(
            f"several integration records match the ticket branch ({', '.join(matches)})",
            remedy="pass --record (or --batch) to name the one to observe",
        )
    return _load_record(root, matches[0])


def _check_history_unchanged(root: Path, record: JsonObject) -> None:
    """The batch history the record links is still what it was when the record was written."""
    batch = _load_batch(root, record["identity"]["source_batch_id"])
    for key in ("publish", "qa"):
        recorded = record["source"][key]
        entry = next(
            (
                item
                for item in batch.get("dispatches", [])
                if item.get("dispatch_id") == recorded["dispatch_id"]
            ),
            None,
        )
        if entry is None or any(
            entry.get(field) != recorded[field]
            for field in ("brief_sha256", "report_sha256")
        ):
            raise CoordinatorError(
                f"the {key} evidence of the integration record no longer matches the batch history",
                remedy="the ledger was changed after the record was written -- "
                + INTERNAL_INVARIANT_REMEDY,
            )
        _verified_dispatch(root, entry)
        history._pending_report(root, batch, entry)


def _notice(state: str, record: JsonObject, observed: JsonObject) -> str:
    target = record["identity"]["target_sha"]
    if state == "current":
        return f"The integration ref still points at the recorded target {target}."
    if state == "stale":
        return (
            f"The integration ref moved from {target} to {observed['integration_tip']}. The initial "
            "evidence covers only the recorded pair; a new check of the new candidate/target pair "
            "is needed. Nothing was dispatched or changed."
        )
    return (
        f"The integration ref could not be read ({observed['reason']}), so the recorded pair "
        "cannot be confirmed current. Nothing was dispatched or changed."
    )


def integration_status(args: argparse.Namespace) -> JsonObject:
    """Observe whether an integration record's pair is still current; strictly read-only.

    A moved integration ref is reported as ``stale`` and asks for a refresh.  Old evidence is never
    carried over to a new pair: each piece of evidence says which pair it covers and whether that
    is the pair the integration ref is at now.  Observing creates no dispatch and writes nothing,
    so repeating it is always safe.
    """
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        record = _resolve_record(root, args)
        _check_history_unchanged(root, record)
        links = _evidence_links(root, record["integration_record_id"])
    identity = record["identity"]
    observed = _observe(repo, identity)
    state = observed["state"]
    tip = observed["integration_tip"]
    return {
        "integration_record_id": record["integration_record_id"],
        "ticket": identity["ticket"],
        "branch": identity["branch"],
        "source_batch_id": identity["source_batch_id"],
        "candidate_sha": identity["candidate_sha"],
        "integration_ref": identity["integration_ref"],
        "target_sha": identity["target_sha"],
        "state": state,
        "integration_tip": tip,
        "refresh_required": state != "current",
        "source_evidence": {
            "pair": record["source"]["pair"],
            "applies_to_current_pair": state == "current",
            "qa": record["source"]["qa"],
            "publish": record["source"]["publish"],
        },
        "pair_checks": [
            {
                "evidence_id": link["evidence_id"],
                "kind": link["kind"],
                "result": link["result"],
                "pair": {
                    "candidate_sha": link["candidate_sha"],
                    "target_sha": link["target_sha"],
                },
                "applies_to_current_pair": tip is not None
                and link["target_sha"] == tip,
                "verification": link["verification"],
            }
            for link in links
        ],
        "notice": _notice(state, record, observed),
    }
