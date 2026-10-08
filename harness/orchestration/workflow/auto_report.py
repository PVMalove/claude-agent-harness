"""The final report of an ``approval_policy: auto`` batch, for the human before any PR (#643).

``build`` is a pure function of the batch and the facts read for it: every ``policy:auto``
decision with its route, reason and evidence, the accepted risks and findings, the retries and
spent budget, the definition-of-done coverage per commit plan, the review and QA results, and the
stop reason when there is one. The report is recorded once as ``batch.auto_report`` -- at the
accepted publish or at a stop -- and ``batch auto-report`` renders it. Opening a pull request
always needs an explicit human confirmation; auto-merge is forbidden.
"""

from __future__ import annotations

import argparse
from functools import partial
from pathlib import Path

from harness.orchestration import operational_guards
from harness.orchestration.core import config as core_config
from harness.orchestration.core.config import (
    _attention_policy,
    _continuation_policy,
    _retry_policy,
)
from harness.orchestration.core.constants import (
    AUTO_REPORT_FIELDS,
    OPERATIONAL_REASON_CATEGORIES,
)
from harness.orchestration.core.git_utils import _candidate_commit
from harness.orchestration.core.utils import CoordinatorError, JsonObject, _repo
from harness.orchestration.ledger.ledger_ops import (
    _ledger_lock,
    _load_batch,
    _load_dispatch,
    _state_root,
)
from harness.orchestration.ledger.lifecycle import LifecycleLedger
from harness.orchestration.workflow import approval as approvals
from harness.orchestration.workflow import carried_items
from harness.orchestration.workflow import commit_plan as plan_rules
from harness.orchestration.workflow import decisions
from harness.orchestration.workflow.history import (
    _latest_developer_candidate,
    _pending_report,
    _validate_batch_integrity,
)

SCHEMA_VERSION = 1
PR_RULE = (
    "open a pull request only after an explicit human confirmation (/to-pull-requests); "
    "auto-merge is forbidden"
)

# One reported dispatch: its batch entry, its brief and its report (None while unreported).
Row = tuple[JsonObject, JsonObject, JsonObject | None]


def _decided(entry: JsonObject) -> str | None:
    decision = entry.get("decision")
    return decision.get("decision") if isinstance(decision, dict) else None


def _accepted_risks(batch: JsonObject) -> list[JsonObject]:
    return [
        {"dispatch_id": record["dispatch_id"], **risk}
        for record in batch.get("auto_decisions", [])
        if record["kind"] == "decision"
        for risk in record["evidence"]["accepted_risks"]
    ]


def _findings(rows: list[Row], settled: set[str]) -> list[JsonObject]:
    """Every item a brief of the batch carried, once, with its source and state."""
    seen: dict[str, JsonObject] = {}
    for _, dispatch, _ in rows:
        section = dispatch.get("carried_items") or {}
        for source, items in section.items():
            for item in items:
                seen.setdefault(
                    item["item_id"],
                    {
                        "item_id": item["item_id"],
                        "source": source,
                        "summary": item.get("summary") or item.get("brief_item"),
                        "state": "settled" if item["item_id"] in settled else "open",
                    },
                )
    return list(seen.values())


def _retries(batch: JsonObject) -> list[JsonObject]:
    return [
        {
            "dispatch_id": decision["dispatch_id"],
            "route": decision["routing"].get("route"),
            "reason_category": decision["routing"].get("reason_category"),
            "previous_role": decision["routing"].get("previous_role"),
            "next_role": decision["routing"].get("next_role"),
            "approved_by": decision.get("approved_by"),
        }
        for decision in batch.get("coordinator_decisions", [])
        if decision.get("decision") == "retry"
        and isinstance(decision.get("routing"), dict)
    ]


def _budget(config: JsonObject, batch: JsonObject) -> JsonObject:
    recorded = batch.get("coordinator_decisions", [])
    continuations = [
        item
        for item in recorded
        if item.get("decision") in {"continue", "continue-automatic"}
    ]
    continuation = _continuation_policy(config)
    return {
        "developer_retries": {
            "spent": decisions._developer_retry_count(batch),
            "max": _retry_policy(config)["max_developer_retries"],
        },
        "continuations": {
            "spent": len(continuations),
            "max_per_dispatch": continuation["max_continuations"],
        },
        "rate_limit_resumes": {
            "spent": sum(
                1 for item in continuations if item["decision"] == "continue-automatic"
            ),
            "max_per_dispatch": continuation["max_rate_limit_resumes"],
        },
        "infrastructure_retries": {
            "spent": sum(
                1
                for item in recorded
                if isinstance(item.get("routing"), dict)
                and item["routing"].get("reason_category")
                in OPERATIONAL_REASON_CATEGORIES
            ),
            "max_per_candidate": _attention_policy(config)[
                "max_infrastructure_retries"
            ],
        },
        "tooling_streak": {
            "spent": operational_guards.tooling_retry_streak(recorded),
            "max": operational_guards.MAX_CONSECUTIVE_TOOLING_RETRIES,
        },
    }


def _plan(batch: JsonObject) -> tuple[JsonObject, list[JsonObject]]:
    pinned = batch.get("commit_plan")
    if isinstance(pinned, list) and pinned:
        return {
            "source": "pinned",
            "sha256": plan_rules.accepted_plan_sha256(batch),
            "entries": [entry["id"] for entry in pinned],
        }, pinned
    plan = plan_rules.default_plan(
        batch["definition_of_done"], list(batch.get("allowed_paths") or [])
    )
    return {
        "source": "default",
        "sha256": None,
        "entries": [entry["id"] for entry in plan],
    }, plan


def _dod_coverage(
    batch: JsonObject, plan: list[JsonObject], coverage: dict[int, list[str]]
) -> list[JsonObject]:
    return [
        {
            "dod_item": position,
            "text": text,
            "plan_entries": [
                entry["id"] for entry in plan if position in entry.get("covers", [])
            ],
            "commits": coverage.get(position, []),
            "covered": bool(coverage.get(position)),
        }
        for position, text in enumerate(batch["definition_of_done"], start=1)
    ]


def _reviews(rows: list[Row]) -> list[JsonObject]:
    results = []
    for entry, dispatch, report in rows:
        review = report.get("review") if report is not None else None
        if entry.get("role") != "code-review" or not isinstance(review, dict):
            continue
        axes = {
            axis: review[axis]
            for axis in ("standards", "spec")
            if isinstance(review.get(axis), dict)
        }
        results.append(
            {
                "dispatch_id": entry["dispatch_id"],
                "candidate_commit": dispatch.get("candidate_commit"),
                "decision": _decided(entry),
                "severity": {
                    axis: value.get("severity") for axis, value in axes.items()
                },
                "findings": sum(
                    len(value.get("findings") or []) for value in axes.values()
                ),
            }
        )
    return results


def _qa(rows: list[Row]) -> list[JsonObject]:
    return [
        {
            "dispatch_id": entry["dispatch_id"],
            "candidate_commit": dispatch.get("candidate_commit"),
            "decision": _decided(entry),
            "outcome": report.get("outcome"),
            "checks": [
                {"command": check.get("command"), "result": check.get("result")}
                for check in report.get("checks_run", [])
                if isinstance(check, dict)
            ],
            "qa_stages": report.get("qa_stages"),
        }
        for entry, dispatch, report in rows
        if entry.get("role") == "qa" and report is not None
    ]


def build(
    batch: JsonObject,
    rows: list[Row],
    config: JsonObject,
    *,
    stop: JsonObject | None,
    candidate: str | None,
    settled: set[str],
    coverage: dict[int, list[str]],
    recorded_at: str | None,
) -> JsonObject:
    """The final auto report of ``batch`` (no I/O); ``recorded_at`` is None for a live view."""
    plan_summary, plan = _plan(batch)
    if stop is not None:
        outcome = "stopped"
        action = (
            f"resolve the stop ({stop['category']}: {stop['reason']}); every later step of "
            f"this batch needs --approved-by; {PR_RULE}"
        )
    elif batch.get("state") == "completed":
        outcome = "completed"
        action = f"review this report; {PR_RULE}"
    else:
        outcome = "in-progress"
        action = f"continue the automatic path with batch auto-decide; {PR_RULE}"
    report: JsonObject = {
        "schema_version": SCHEMA_VERSION,
        "batch_id": batch["batch_id"],
        "ticket": batch.get("ticket"),
        "branch": batch.get("branch"),
        "outcome": outcome,
        "stop": stop,
        "candidate_commit": candidate,
        "decisions": list(batch.get("auto_decisions", [])),
        "accepted_risks": _accepted_risks(batch),
        "findings": _findings(rows, settled),
        "retries": _retries(batch),
        "budget": _budget(config, batch),
        "commit_plan": plan_summary,
        "dod_coverage": _dod_coverage(batch, plan, coverage),
        "review": _reviews(rows),
        "qa": _qa(rows),
        "next_human_action": action,
        "recorded_at": recorded_at,
    }
    assert set(report) | {"record_sha256"} == AUTO_REPORT_FIELDS
    return report


def _rows(root: Path, batch: JsonObject) -> list[Row]:
    return [
        (
            entry,
            _load_dispatch(root, entry["dispatch_id"]),
            _pending_report(root, batch, entry) if entry.get("report") else None,
        )
        for entry in batch.get("dispatches", [])
    ]


def _coverage(repo: Path, rows: list[Row]) -> dict[int, list[str]]:
    """Covering commits per definition-of-done item, from accepted developer work reports."""
    covered: dict[int, list[str]] = {}
    for entry, dispatch, report in rows:
        if (
            entry.get("role") != "developer"
            or dispatch.get("purpose") != "work"
            or report is None
            or _decided(entry) not in {"accept", "override-warning"}
        ):
            continue
        records, _ = plan_rules.coverage(
            report, dispatch, partial(_candidate_commit, repo)
        )
        for record in records or []:
            commits = covered.setdefault(record["dod_item"], [])
            commits.extend(
                sha for sha in record.get("commits", []) if sha not in commits
            )
    return covered


def live(
    repo: Path,
    root: Path,
    config: JsonObject,
    batch: JsonObject,
    recorded_at: str | None,
) -> JsonObject:
    """``build`` on the facts read for ``batch`` from the ledger and Git."""
    rows = _rows(root, batch)
    try:
        candidate: str | None = _latest_developer_candidate(repo, root, batch)
    except CoordinatorError:
        candidate = None
    return build(
        batch,
        rows,
        config,
        stop=batch.get("auto_stop"),
        candidate=candidate,
        settled=carried_items._settled_item_ids(root, batch),
        coverage=_coverage(repo, rows),
        recorded_at=recorded_at,
    )


def record(
    repo: Path, root: Path, config: JsonObject, batch: JsonObject, moment: str
) -> JsonObject:
    """Record the final report on the batch once; the caller writes the batch."""
    if "auto_report" not in batch:
        batch["auto_report"] = approvals.sealed(live(repo, root, config, batch, moment))
    return dict(batch["auto_report"])


def auto_report(args: argparse.Namespace) -> JsonObject:
    """``batch auto-report``: render the recorded final report, record a stop the ledger now
    shows, or render the live report of a batch still on the automatic path."""
    from harness.orchestration.workflow import auto_policy

    repo = _repo(args)
    root = _state_root(args, repo)
    config = core_config._config(repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        batch = _load_batch(root, args.batch)
        try:
            _validate_batch_integrity(root, batch)
        except CoordinatorError as exc:
            stop = auto_policy.ledger_stop(exc)
            return {
                "batch_id": args.batch,
                "recorded": False,
                "stop": {
                    "category": stop.category,
                    "reason": stop.reason,
                    "evidence": stop.evidence,
                },
                "report": None,
                "next_action": "a human inspects the ledger before any further step; "
                + PR_RULE,
            }
        if (
            "auto_report" not in batch
            and batch.get("approval_policy") != approvals.AUTO_POLICY
        ):
            raise CoordinatorError(
                "batch auto-report applies only to a batch planned under approval_policy auto",
                remedy="render this batch's evidence with batch decision-packet",
            )
        if "auto_report" not in batch and approvals.auto_active(config, batch):
            found = auto_policy.derive_stop(repo, root, config, batch)
            if found is not None:
                auto_policy.persist_stop(
                    ledger,
                    repo,
                    root,
                    config,
                    batch,
                    found,
                    detected_by="batch auto-report",
                )
        if "auto_report" in batch:
            return {
                "batch_id": batch["batch_id"],
                "recorded": True,
                "report": batch["auto_report"],
                "next_action": batch["auto_report"]["next_human_action"],
            }
        report = live(repo, root, config, batch, None)
    return {
        "batch_id": batch["batch_id"],
        "recorded": False,
        "report": report,
        "next_action": report["next_human_action"],
    }
