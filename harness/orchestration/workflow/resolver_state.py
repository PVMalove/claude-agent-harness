"""Events, budget and report contract of the conflict-resolver route (issue #534).

The route (``resolver``) creates batches and briefs; this module holds what the report and decision
side also needs -- the append-only events the cycle budget is derived from (so a lost session or a
resume can never reset it), the ``resolver`` report block a conflict-resolver must return, and the
record of an accepted resolution.  It never creates a batch or dispatch, so reports and decisions
may import it without a cycle.
"""

from __future__ import annotations

from pathlib import Path

from harness.orchestration.core import config as core_config
from harness.orchestration.core import utils
from harness.orchestration.core.git_utils import (
    _commits_between,
    _git_is_ancestor,
)
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _non_empty,
    _read_object,
)
from harness.orchestration.ledger.ledger_ops import (
    _records_root,
    _write_record,
)
from harness.orchestration.ledger.lifecycle import (
    LifecycleLedger,
    ResolverEventRecord,
)
from harness.orchestration.workflow import integration, pr_refresh

RESOLVER_ROLE = "conflict-resolver"
RESOLVER_NEXT_ACTION = "resolve-conflict"
RESOLVER_BATCH_KIND = "resolver"
# Two target SHAs are resolved automatically; a third needs a human decision that extends the budget.
AUTOMATIC_CYCLES = 2
EVENT_KINDS = (
    "cycle-spent",
    "same-target-fix",
    "human-decision",
    "scope-change",
    "exhausted",
)
RESOLVER_CAUSES = ("resolved", "integration-incompatibility", "task-defect")
COMMIT_PLAN_ENTRY_ID = "resolution"
PROHIBITIONS = (
    "Add no behaviour outside the requirements of the two sides",
    "Never abort the rebase and never force-push",
    "Never write to an integration or protected branch",
    "Never widen the scope of this brief",
)
REPORT_RESOLVER_FIELDS = frozenset(
    {
        "preserved_requirements",
        "human_decisions",
        "target_sha",
        "resolved_candidate_sha",
        "cause",
        "changed_files",
        "commits",
    }
)


# -- events and the budget derived from them -------------------------------------------------------


def events(root: Path, record_id: str) -> list[JsonObject]:
    """The route's events of an integration record, oldest first."""
    directory = _records_root(root) / ResolverEventRecord.directory
    found = [
        _read_object(path, "resolver event")
        for path in sorted(directory.glob("*.json") if directory.is_dir() else [])
    ]
    found = [item for item in found if item.get("integration_record_id") == record_id]
    return sorted(found, key=lambda item: (item["recorded_at"], item["event_id"]))


def write_event(
    ledger: LifecycleLedger,
    root: Path,
    kind: str,
    members: JsonObject,
    **fields: object,
) -> JsonObject:
    """Write one immutable event; repeating the same event returns the recorded one."""
    members = {
        "discriminator": None,
        "dispatch_id": None,
        "target_sha": None,
        **members,
    }
    event_id = ResolverEventRecord.derive_id({**members, "kind": kind})
    path = _records_root(root) / ResolverEventRecord.directory / f"{event_id}.json"
    if path.is_file():
        return _read_object(path, "resolver event")
    document: JsonObject = {
        "event_id": event_id,
        "kind": kind,
        **members,
        **fields,
        "recorded_at": utils._now(),
    }
    _write_record(ledger, ResolverEventRecord.from_dict(document))
    return document


def budget(root: Path, config: JsonObject, record_id: str) -> JsonObject:
    """The cycle budget of a record.  A cycle is one target SHA with a recorded resolver report;
    a clean rebase, a human answer and a fix on the same target spend none.  Everything is derived
    from the events, so no session can reset it."""
    recorded = events(root, record_id)
    spent = sorted(
        {item["target_sha"] for item in recorded if item["kind"] == "cycle-spent"}
    )
    extended = sum(
        1
        for item in recorded
        if item["kind"] == "human-decision" and item.get("extends_budget")
    )
    total = AUTOMATIC_CYCLES + extended
    return {
        "automatic_cycles": AUTOMATIC_CYCLES,
        "human_extensions": extended,
        "cycles_total": total,
        "cycles_spent": len(spent),
        "remaining": max(0, total - len(spent)),
        "spent_targets": spent,
        "internal_fix_budget": core_config._retry_policy(config)[
            "max_developer_retries"
        ],
    }


def same_target_fixes(root: Path, record_id: str, target: str) -> int:
    return sum(
        1
        for item in events(root, record_id)
        if item["kind"] == "same-target-fix" and item["target_sha"] == target
    )


def last_cause(root: Path, record_id: str) -> str:
    causes = [
        item["cause"]
        for item in events(root, record_id)
        if item["kind"] in {"cycle-spent", "same-target-fix"} and item.get("cause")
    ]
    return causes[-1] if causes else "integration-incompatibility"


def budget_targets(root: Path, record_id: str) -> set[str]:
    return {
        item["target_sha"]
        for item in events(root, record_id)
        if item["kind"] == "cycle-spent"
    }


def human_decision_recorded(root: Path, batch: JsonObject, dispatch_id: str) -> bool:
    """Whether the newest checkpoint of the dispatch has its human-decision event."""
    resolver = batch.get("resolver")
    checkpoints = [
        item["checkpoint_id"]
        for item in batch.get("checkpoints", [])
        if item["dispatch_id"] == dispatch_id
    ]
    if not isinstance(resolver, dict) or not checkpoints:
        return False
    return any(
        item["kind"] == "human-decision"
        and item["dispatch_id"] == dispatch_id
        and item.get("checkpoint_id") == checkpoints[-1]
        for item in events(root, resolver["integration_record_id"])
    )


# -- the report --------------------------------------------------------------------------------------


def _strings_of(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or any(not _non_empty(item) for item in value):
        raise CoordinatorError(
            f"resolver report {label} must be a list of non-empty strings",
            remedy=f"set resolver.{label} to a list of non-empty strings",
        )
    return list(value)


def require_fix_budget(root: Path, config: JsonObject, batch: JsonObject) -> None:
    """A retry on the same target is a fix inside the project retry budget; one beyond it stops
    this task, never the neighbouring batches."""
    resolver = batch["resolver"]
    allowed = budget(root, config, resolver["integration_record_id"])[
        "internal_fix_budget"
    ]
    if (
        same_target_fixes(
            root, resolver["integration_record_id"], resolver["target_sha"]
        )
        >= allowed
    ):
        raise CoordinatorError(
            f"the resolver fixes on target {resolver['target_sha']} reached "
            f"retry_policy.max_developer_retries ({allowed})",
            remedy="block or fail this resolver batch: the branch and the evidence stay, and only this task stops",
        )


def is_resolver_brief(dispatch: JsonObject) -> bool:
    return dispatch.get("role") == RESOLVER_ROLE


def validate_report(
    repo: Path, root: Path, batch: JsonObject, dispatch: JsonObject, report: JsonObject
) -> None:
    """The ``resolver`` block of a conflict-resolver report, checked against its brief, the
    recorded human decisions and Git.  Required on a completed report, forbidden on any other
    role's report."""
    block = report.get("resolver")
    if not is_resolver_brief(dispatch):
        if block is not None:
            raise CoordinatorError(
                "only a conflict-resolver report carries the resolver block",
                remedy="drop the resolver field from this report",
            )
        return
    if block is None:
        if report["outcome"] == "completed":
            raise CoordinatorError(
                "a completed conflict-resolver report must carry the resolver block",
                remedy="add resolver with preserved_requirements, human_decisions, target_sha, "
                "resolved_candidate_sha, cause, changed_files and commits",
            )
        return
    if not isinstance(block, dict) or set(block) != REPORT_RESOLVER_FIELDS:
        raise CoordinatorError(
            "resolver block must carry exactly: "
            + ", ".join(sorted(REPORT_RESOLVER_FIELDS)),
            remedy="set resolver to an object with exactly those fields",
        )
    brief = dispatch["resolver"]
    if block["cause"] not in RESOLVER_CAUSES:
        raise CoordinatorError(
            f"resolver cause must be one of {', '.join(RESOLVER_CAUSES)}",
            remedy="name why the resolver stopped or finished with one of those causes",
        )
    if block["target_sha"] != brief["target_sha"]:
        raise CoordinatorError(
            "resolver target_sha does not match the brief",
            remedy=f"report the target {brief['target_sha']} the resolution was made against",
        )
    if report["outcome"] != "completed":
        return
    if block["cause"] != "resolved":
        raise CoordinatorError(
            "a completed resolver report must have cause 'resolved'",
            remedy="a resolver that stopped on an incompatibility or the ticket's own defect reports blocked",
        )
    preserved = block["preserved_requirements"]
    required = {
        ("candidate", text) for text in brief["sides"]["candidate"]["requirements"]
    } | {("target", text) for text in brief["sides"]["target"]["requirements"]}
    if not isinstance(preserved, list) or any(
        not isinstance(item, dict)
        or set(item) != {"side", "requirement", "preserved_by"}
        or not _non_empty(item["preserved_by"])
        for item in preserved
    ):
        raise CoordinatorError(
            "preserved_requirements must list objects with side, requirement and preserved_by",
            remedy="describe, for every requirement of both sides, how the resolution preserves it",
        )
    named = {(item["side"], item["requirement"]) for item in preserved}
    if named != required:
        missing = sorted(required - named)
        raise CoordinatorError(
            f"preserved_requirements must account for exactly the requirements of both sides (missing or unknown: {len(required ^ named)}; first missing: {missing[:1]})",
            remedy="list every requirement of sides.candidate and sides.target from the brief verbatim, and no other",
        )
    recorded = sorted(
        item["event_id"]
        for item in events(root, batch["resolver"]["integration_record_id"])
        if item["kind"] == "human-decision"
        and item["dispatch_id"] == dispatch["dispatch_id"]
    )
    if sorted(_strings_of(block["human_decisions"], "human_decisions")) != recorded:
        raise CoordinatorError(
            "human_decisions must list exactly the human-decision events recorded for this dispatch",
            remedy=f"report the event ids {recorded} (an empty list when none was recorded)",
        )
    resolved = report["commit_sha"]
    if block["resolved_candidate_sha"] != resolved:
        raise CoordinatorError(
            "resolved_candidate_sha must be the reported commit_sha",
            remedy="report the final commit of the resolution as both commit_sha and resolved_candidate_sha",
        )
    if block["changed_files"] != report["changed_files"]:
        raise CoordinatorError(
            "resolver changed_files must equal the report changed_files",
            remedy="report the same exact changed files in the resolver block",
        )
    if not _git_is_ancestor(repo, brief["target_sha"], resolved):
        raise CoordinatorError(
            f"the resolution does not contain the target {brief['target_sha']}",
            remedy="finish the rebase onto the exact target SHA and report the rebased HEAD",
        )
    commits = _commits_between(repo, brief["target_sha"], resolved)
    mapped = block["commits"]
    entry_ids = {item["id"] for item in brief["commit_plan"]}
    if (
        not isinstance(mapped, list)
        or any(
            not isinstance(item, dict)
            or set(item) != {"commit_sha", "plan_entry_id"}
            or item["plan_entry_id"] not in entry_ids
            for item in mapped
        )
        or [item["commit_sha"] for item in mapped] != commits
    ):
        raise CoordinatorError(
            "resolver commits must map every commit after the target, in order, to a plan entry",
            remedy=f"list {commits} as {{commit_sha, plan_entry_id}} pairs with plan entry id {sorted(entry_ids)}",
        )


def record_report(
    ledger: LifecycleLedger,
    root: Path,
    batch: JsonObject,
    dispatch: JsonObject,
    report: JsonObject,
) -> None:
    """A recorded resolver report spends the cycle of its target SHA, or counts as a fix on it."""
    resolver = batch["resolver"]
    record_id = resolver["integration_record_id"]
    target = resolver["target_sha"]
    block = report.get("resolver") or {}
    first = target not in budget_targets(root, record_id)
    write_event(
        ledger,
        root,
        "cycle-spent" if first else "same-target-fix",
        {
            "integration_record_id": record_id,
            "target_sha": target,
            "dispatch_id": dispatch["dispatch_id"],
        },
        ticket=resolver["ticket"],
        outcome=report["outcome"],
        cause=block.get("cause") if isinstance(block, dict) else None,
    )


def record_resolution(
    ledger: LifecycleLedger,
    repo: Path,
    root: Path,
    batch: JsonObject,
    report: JsonObject,
    config: JsonObject,
) -> None:
    """An accepted resolution moves the record's pair to the resolved candidate, as a refresh
    does: the old QA stays historical, and CI or local QA of the new pair is still required."""
    resolver = batch["resolver"]
    record_id = resolver["integration_record_id"]
    record = integration._load_record(root, record_id)
    pr_refresh.write_refresh_record(
        ledger,
        repo,
        record,
        pr_refresh.current_pair(root, record),
        resolver["target_sha"],
        report["commit_sha"],
        {
            "invoked": True,
            "cycles_spent": budget(root, config, record_id)["cycles_spent"],
        },
    )
