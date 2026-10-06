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
import json
from pathlib import Path

from harness.errors import INTERNAL_INVARIANT_REMEDY
from harness.health.project_tracker import resolve_project_tracker
from harness.orchestration.core import ci_source
from harness.orchestration.core import utils
from harness.orchestration.core.config import _reject_sensitive
from harness.orchestration.core.constants import (
    INTEGRATION_CI_COLLECTED,
    INTEGRATION_CI_COLLECTED_FAILURE,
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
from harness.orchestration.workflow import history, pr_refresh

INTEGRATION_RECORD_CONTRACT = 1
_ACCEPTED_DECISIONS = {"accept", "override-warning"}
# A refreshed candidate is confirmed only by a passed CI or local-QA check of its own pair: resolver
# evidence records a conflict resolution, never the verification of the result.
_INTEGRATION_VERIFICATION_KINDS = ("ci", "local-qa")


def _satisfies(link: JsonObject, pair: JsonObject) -> bool:
    """A passed check of exactly this pair.  CI counts only when 'integration collect-ci' accepted
    it (issue #535): a CI result linked by hand stays unverified evidence."""
    if (
        link["result"] != "passed"
        or link["candidate_sha"] != pair["candidate_sha"]
        or link["target_sha"] != pair["target_sha"]
    ):
        return False
    if link["kind"] == "ci":
        return link.get("verification") == INTEGRATION_CI_COLLECTED
    return bool(link["kind"] == "local-qa")


def _qa_replacement(
    links: list[JsonObject], pair: JsonObject, state: str
) -> JsonObject:
    """Whether collector-accepted CI stands in for a repeat of full local QA of the current pair.
    The original QA reports are never touched; this block only reports the replacement."""
    collected = [
        link
        for link in links
        if link["kind"] == "ci" and link.get("verification") == INTEGRATION_CI_COLLECTED
    ]
    current = [link for link in collected if _satisfies(link, pair)]
    if current and state == "current":
        verified = current[-1]["collector"]
        return {
            "applies": True,
            "reason": None,
            "re_refresh_required": False,
            "evidence_id": current[-1]["evidence_id"],
            **{
                key: verified[key]
                for key in (
                    "source",
                    "repository",
                    "pull_request",
                    "candidate_sha",
                    "target_sha",
                    "merge_commit_sha",
                    "checks",
                )
            },
        }
    if current:
        reason = "integration_moved" if state == "stale" else "integration_unreadable"
        verified = current[-1]["collector"]
        return {
            "applies": False,
            "reason": reason,
            "re_refresh_required": True,
            "evidence_id": current[-1]["evidence_id"],
            "candidate_sha": verified["candidate_sha"],
            "target_sha": verified["target_sha"],
            "merge_commit_sha": verified["merge_commit_sha"],
        }
    return {
        "applies": False,
        "reason": "no_collected_ci" if not collected else "pair_changed",
        "re_refresh_required": False,
    }


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
        return _store_evidence(root, ledger, members, "unverified", {})


def _store_evidence(
    root: Path,
    ledger: LifecycleLedger,
    members: JsonObject,
    verification: str,
    extra: JsonObject,
) -> JsonObject:
    """Write one immutable pair-check record (the caller holds the ledger lock); the same members
    find the existing record instead of writing another."""
    evidence_id = IntegrationEvidenceRecord.derive_id(members)
    path = (
        _records_root(root)
        / IntegrationEvidenceRecord.directory
        / f"{evidence_id}.json"
    )
    if path.is_file():
        return {
            "evidence_id": evidence_id,
            "integration_record_id": members["integration_record_id"],
            "linked": False,
            "evidence": _read_object(path, "integration evidence record"),
        }
    document: JsonObject = {
        "evidence_id": evidence_id,
        **members,
        "scope": "pair-check",
        "recorded_at": utils._now(),
        "verification": verification,
        **extra,
    }
    _reject_sensitive(document, "integration evidence")
    _write_record(ledger, IntegrationEvidenceRecord.from_dict(document))
    return {
        "evidence_id": evidence_id,
        "integration_record_id": members["integration_record_id"],
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


def _notice(
    state: str,
    record: JsonObject,
    observed: JsonObject,
    pair: JsonObject,
    verified: bool,
) -> str:
    target = pair["target_sha"]
    if (
        state == "current"
        and pair["candidate_sha"] != record["identity"]["candidate_sha"]
    ):
        if verified:
            return (
                f"The branch was refreshed onto {target}; a passed integration check covers the "
                f"new candidate {pair['candidate_sha']}. No re-review is required."
            )
        return (
            f"The branch was refreshed onto {target} as candidate {pair['candidate_sha']}. The old "
            "QA is historical evidence and does not confirm it: CI collected by 'integration collect-ci' or local "
            "integration QA (register it with 'integration link-evidence') of the new pair is required. No re-review is "
            "required because of the refresh alone."
        )
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
        pair = pr_refresh.current_pair(root, record)
        refreshes = pr_refresh.refresh_records(root, record["integration_record_id"])
    identity = record["identity"]
    observed = _observe(repo, {**identity, "target_sha": pair["target_sha"]})
    state = observed["state"]
    tip = observed["integration_tip"]
    from harness.orchestration.workflow.local_qa import verified_evidence

    local_pair_current = False
    if any(link["kind"] == "local-qa" for link in links) and state == "current":
        try:
            local_pair_current = (
                _remote_branch_tip(repo, identity["remote"], identity["branch"])
                == pair["candidate_sha"]
            )
        except CoordinatorError:
            pass
    verified = not refreshes or any(
        _satisfies(link, pair)
        and (
            link["kind"] != "local-qa"
            or local_pair_current
            and verified_evidence(root, link)
        )
        for link in links
    )
    return {
        "integration_record_id": record["integration_record_id"],
        "ticket": identity["ticket"],
        "branch": identity["branch"],
        "source_batch_id": identity["source_batch_id"],
        "candidate_sha": pair["candidate_sha"],
        "original_candidate_sha": identity["candidate_sha"],
        "integration_ref": identity["integration_ref"],
        "target_sha": pair["target_sha"],
        "original_target_sha": identity["target_sha"],
        "state": state,
        "integration_tip": tip,
        "refresh_required": state != "current",
        "source_evidence": {
            "pair": record["source"]["pair"],
            "applies_to_current_pair": state == "current" and not refreshes,
            "qa": record["source"]["qa"],
            "publish": record["source"]["publish"],
        },
        "refreshes": [
            {
                key: item[key]
                for key in (
                    "refresh_id",
                    "previous_candidate_sha",
                    "new_candidate_sha",
                    "target_sha",
                )
            }
            for item in refreshes
        ],
        "verification": {
            "required": bool(refreshes),
            "satisfied": verified,
            "accepted_kinds": list(_INTEGRATION_VERIFICATION_KINDS),
            "re_review_required": False,
        },
        "qa_replacement": _qa_replacement(links, pair, state),
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
                and link["target_sha"] == tip
                and (
                    link["kind"] != "local-qa"
                    or local_pair_current
                    and link["candidate_sha"] == pair["candidate_sha"]
                ),
                "verification": "verified"
                if link["kind"] == "local-qa" and verified_evidence(root, link)
                else "unverified"
                if link["kind"] == "local-qa"
                else link["verification"],
            }
            for link in links
        ],
        "notice": _notice(state, record, observed, pair, verified),
    }


def _required_checks(repo: Path) -> list[str]:
    """``ci_required_checks`` of .harness/project.json; anything unusable means not configured."""
    try:
        data = json.loads((repo / ".harness/project.json").read_text(encoding="utf-8"))
        value = data.get("ci_required_checks") if isinstance(data, dict) else None
    except (OSError, ValueError):
        return []
    if (
        isinstance(value, list)
        and all(isinstance(item, str) and item.strip() for item in value)
        and len(set(value)) == len(value)
    ):
        return list(value)
    return []


# What a fallback of 'collect-ci' asks next, in one place: wait for a check that is still running,
# otherwise run local QA and name why combined-result CI cannot confirm the pair.
_CI_FALLBACK_NEXT: dict[str, tuple[str, str | None]] = {
    "pending_check": ("wait", None),
    "not_configured": ("local-qa", "absent"),
    "unsupported_tracker": ("local-qa", "absent"),
    "unavailable": ("local-qa", "unavailable"),
}


def ci_next(outcome: str, reason: str | None) -> JsonObject | None:
    """The deterministic next action of a collect-ci verdict; an accepted one needs none."""
    if outcome == "accepted":
        return None
    if outcome == "failed":
        return {"action": "route", "ci_condition": None}
    action, condition = _CI_FALLBACK_NEXT.get(str(reason), ("local-qa", "unusable"))
    return {"action": action, "ci_condition": condition}


def _collected(
    outcome: str, reason: str | None, detail: str, **extra: object
) -> JsonObject:
    hint = ci_next(outcome, reason)
    return {
        "outcome": outcome,
        "reason": reason,
        "detail": detail,
        "recorded": False,
        "local_qa_required": outcome != "accepted",
        **({"next": hint} if hint else {}),
        **extra,
    }


def integration_collect_ci(args: argparse.Namespace) -> JsonObject:
    """Collect CI evidence for the combined result of a pull request into the integration record.

    Read-only towards the tracker: it never opens or merges a pull request and never touches branch
    protection.  Only a verdict of ``accepted`` or ``failed`` is recorded; every ``fallback``
    (unsupported tracker, unavailable or unusable CI, unknown checkout, stale or foreign pair,
    missing or pending check) records nothing and says that local QA is still needed.
    """
    repo = _repo(args)
    root = _state_root(args, repo)
    number = _pull_request_number(args)
    if number is None:
        raise CoordinatorError(
            "collect-ci requires a positive pull request number",
            remedy="pass --pull-request with the number of the pull request",
        )
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        record = _resolve_record(root, args)
        pair = pr_refresh.current_pair(root, record)
    tracker = resolve_project_tracker(repo).effective
    if tracker.type != "github" or not tracker.host or not tracker.project:
        return _collected(
            "fallback",
            "unsupported_tracker",
            "CI evidence is collected only for a GitHub tracker; use local integration QA",
            integration_record_id=record["integration_record_id"],
        )
    required = _required_checks(repo)
    source = getattr(args, "ci_source", None) or ci_source.GitHubCiSource(
        host=tracker.host
    )
    observation = source.observe(tracker.project, number)
    verdict = ci_source.evaluate(
        observation,
        repository=tracker.project,
        pull_request=number,
        candidate_sha=pair["candidate_sha"],
        target_sha=pair["target_sha"],
        required_checks=required,
        base_ref=record["identity"]["integration_ref"],
    )
    result = _collected(
        verdict.outcome,
        verdict.reason,
        verdict.detail,
        integration_record_id=record["integration_record_id"],
        pair={"candidate_sha": pair["candidate_sha"], "target_sha": pair["target_sha"]},
    )
    verified = verdict.verified
    if verdict.outcome == "fallback" or verified is None:
        return result
    with _ledger_lock(ledger):
        record = _load_record(root, record["integration_record_id"])
        if pr_refresh.current_pair(root, record) != pair:
            return _collected(
                "fallback",
                "stale_candidate",
                "the integration pair moved while CI was collected",
                integration_record_id=record["integration_record_id"],
            )
        accepted = verdict.outcome == "accepted"
        members: JsonObject = {
            "integration_record_id": record["integration_record_id"],
            "kind": "ci",
            "candidate_sha": pair["candidate_sha"],
            "target_sha": pair["target_sha"],
            "result": "passed" if accepted else "failed",
            "reference": f"{verified['source']}:{verified['repository']}/pull/{number}"
            f"@{verified['merge_commit_sha']}",
            "artifact_sha256": _sha256(verified),
        }
        stored = _store_evidence(
            root,
            ledger,
            members,
            INTEGRATION_CI_COLLECTED if accepted else INTEGRATION_CI_COLLECTED_FAILURE,
            {"collector": verified},
        )
    return {
        **result,
        "recorded": True,
        "evidence_id": stored["evidence_id"],
        "linked": stored["linked"],
        "verified": verified,
    }


# -- the next step of a PR continuation (issue #537) ------------------------------------------------


def _pull_request_number(args: argparse.Namespace) -> int | None:
    number = getattr(args, "pull_request", None)
    if number is None:
        return None
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise CoordinatorError(
            "the pull request number must be a positive integer",
            remedy="pass --pull-request with the number of the opened pull request",
        )
    return number


def _failed_pair_checks(links: list[JsonObject], pair: JsonObject) -> list[str]:
    """Code failures of exactly this pair: a collector-recorded CI failure or a failed generated
    local-QA gate.  An operational outcome (CI fallback, unavailable local QA) records no failed
    evidence at all, and a hand-linked result is never verified, so neither can appear here."""
    return [
        link["evidence_id"]
        for link in links
        if link["result"] == "failed"
        and link["candidate_sha"] == pair["candidate_sha"]
        and link["target_sha"] == pair["target_sha"]
        and (
            link["kind"] == "ci"
            and link.get("verification") == INTEGRATION_CI_COLLECTED_FAILURE
            or link["kind"] == "local-qa"
            and bool(link.get("local_qa_request_id"))
        )
    ]


def _verified_source(status: JsonObject) -> tuple[str, JsonObject]:
    """Where the verification of the current pair comes from: the original QA of an unrefreshed
    pair, else the accepted CI, else the generated and verified local QA."""
    if not status["refreshes"]:
        return "original-qa", status["source_evidence"]["qa"]
    replacement = status["qa_replacement"]
    if replacement["applies"]:
        return "ci", {
            key: replacement[key]
            for key in (
                "evidence_id",
                "source",
                "repository",
                "pull_request",
                "merge_commit_sha",
            )
        }
    local = [
        check
        for check in status["pair_checks"]
        if check["kind"] == "local-qa"
        and check["result"] == "passed"
        and check["applies_to_current_pair"]
        and check["verification"] == "verified"
    ]
    return "local-qa", {"evidence_id": local[-1]["evidence_id"]}


def _has_passed_check(links: list[JsonObject], status: JsonObject) -> bool:
    """A passed, verified check of the current pair, original or refreshed: it supersedes an
    earlier failed one.  ``verification.satisfied`` cannot say it, because it is true for any
    pair that was never refreshed."""
    usable = {
        check["evidence_id"]
        for check in status["pair_checks"]
        if check["applies_to_current_pair"] and check["verification"] != "unverified"
    }
    return any(
        link["evidence_id"] in usable and _satisfies(link, status) for link in links
    )


def _route_failure(
    repo: Path,
    root: Path,
    record_id: str,
    pair: JsonObject,
    failed: list[str],
    refreshed: bool,
) -> JsonObject:
    """Route a failed check of the current pair.  A refreshed or resolver-produced candidate failed
    because of its combination with the target: the same resolver continues, inside the budget
    that is derived from its append-only events (a human answer or a CI wait never resets it).  A
    failure of the original, never-refreshed pair is the task's own defect: the ordinary developer
    with review and QA takes it."""
    if not refreshed:
        return {
            "step": "route-failure",
            "route": "developer",
            "failed_evidence": failed,
            "next": [
                "batch decide --batch <source batch> --decision retry (a regular developer dispatch "
                "with its review and QA; the integration record and its evidence stay)"
            ],
        }
    from harness.orchestration.core import config as core_config
    from harness.orchestration.workflow import resolver_state

    current, fixes, reason = resolver_state.exhaustion(
        root, core_config._config(repo), record_id, pair["target_sha"]
    )
    exhausted = reason is not None
    result: JsonObject = {
        "step": "human-decision" if exhausted else "route-failure",
        "route": "resolver",
        "failed_evidence": failed,
        "budget": current,
        "fixes_on_target": fixes,
    }
    if exhausted:
        result["next"] = [
            "integration resolver-event --record <id> --kind human-decision --dispatch <resolver dispatch> "
            "--decided-by <name> --note <decision> --extends-budget"
        ]
    else:
        result["next"] = [
            "integration resolve --record <id>  (a failed verification of the refreshed pair; "
            "then batch approve and dispatch create --role conflict-resolver in the coordinator session)"
        ]
    return result


def integration_next(args: argparse.Namespace) -> JsonObject:
    """The next step of a PR continuation of one integration record; strictly read-only.

    It classifies what ``integration status`` and the recorded evidence already say, and never
    writes Git, the ledger, a dispatch or a pull request, and never asks the tracker.  The steps:
    ``unavailable`` (the integration ref cannot be read: an operational stop), ``resolver-open``
    (a resolver batch is open: wait, no cycle is spent), ``refresh``, ``route-failure`` /
    ``human-decision`` (a failed check of the current pair), ``confirm-pr`` (before a pull
    request exists), ``verify`` (a refreshed pair of an opened pull request still needs CI or
    local QA) and ``handoff`` (the pair is current and verified: the facts for a manual merge)."""
    from harness.orchestration.workflow import resolver

    repo = _repo(args)
    root = _state_root(args, repo)
    number = _pull_request_number(args)
    status = integration_status(args)
    record_id = status["integration_record_id"]
    refreshed = bool(status["refreshes"])
    result: JsonObject = {
        "integration_record_id": record_id,
        "ticket": status["ticket"],
        "branch": status["branch"],
        "candidate_sha": status["candidate_sha"],
        "target_sha": status["target_sha"],
        "integration_tip": status["integration_tip"],
        "state": status["state"],
        "refreshed": refreshed,
    }
    if status["state"] == "unavailable":
        return {
            **result,
            "step": "unavailable",
            "next": [
                "restore access to the integration ref, then repeat 'integration next'"
            ],
        }
    with _ledger_lock(LifecycleLedger(root)):
        open_batch = resolver.open_resolver_batch(root, record_id)
        links = _evidence_links(root, record_id)
    if open_batch is not None:
        return {
            **result,
            "step": "resolver-open",
            "batch_id": open_batch["batch_id"],
            "batch_state": open_batch["state"],
            "next_action": open_batch.get("next_action"),
            "next": [
                "the coordinator workflow continues the open resolver batch; wait"
            ],
        }
    if status["state"] != "current":
        return {
            **result,
            "step": "refresh",
            "next": [
                "integration refresh --record <id>; a 'conflict' result continues with 'integration resolve'"
            ],
        }
    satisfied = status["verification"]["satisfied"]
    failed = _failed_pair_checks(links, status)
    if failed and not _has_passed_check(links, status):
        return {
            **result,
            **_route_failure(repo, root, record_id, status, failed, refreshed),
        }
    if number is None:
        pending = refreshed and not satisfied
        return {
            **result,
            "step": "confirm-pr",
            "qa_source": "verification-pending-after-pr"
            if pending
            else _verified_source(status)[0],
            "next": [
                "ask for a separate explicit confirmation naming candidate_sha and target_sha, then "
                "open the pull request"
            ],
        }
    if not satisfied:
        return {
            **result,
            "step": "verify",
            "next": [
                "integration collect-ci --record <id> --pull-request <number>",
                "on next.action 'local-qa': integration local-qa --record <id> --ci-condition <ci_condition> "
                "--reason <reason>; then repeat 'integration next'",
            ],
        }
    source, reference = _verified_source(status)
    return {
        **result,
        "step": "handoff",
        "handoff": {
            "candidate_sha": status["candidate_sha"],
            "target_sha": status["target_sha"],
            "qa_source": source,
            "reference": reference,
            "refreshed": refreshed,
        },
        "next": ["repeat 'integration next' just before a manual merge"],
    }
