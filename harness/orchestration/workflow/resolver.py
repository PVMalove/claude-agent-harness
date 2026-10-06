"""The conflict-resolver route (issue #534).

A textual conflict between a published ticket branch (the candidate) and a moved integration tip
(the target) is handed to one ``conflict-resolver`` writer in a new batch of its own; the finished
batch of the original ticket stays history.  This module creates that batch and the immutable
``resolver`` section of its brief and records the human's events; the events, the budget derived
from them and the report contract live in ``resolver_state``.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from harness.orchestration.core import config as core_config
from harness.orchestration.core import utils
from harness.orchestration.core.git_utils import (
    _changed_files_between,
    _git,
    _remote_branch_tip,
)
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _non_empty,
    _read_object,
    _repo,
)
from harness.orchestration.core.workspace import _validate_branch
from harness.orchestration.ledger.ledger_ops import (
    _ledger_lock,
    _load_batch,
    _load_dispatch,
    _records_root,
    _replace_record,
    _state_root,
    _write_record,
)
from harness.orchestration.ledger.lifecycle import (
    BatchRecord,
    LifecycleLedger,
    ResolverRecord,
)
from harness.orchestration.workflow import integration, pr_refresh
from harness.orchestration.workflow.batch import create_batch
from harness.orchestration.workflow.history import _validate_batch_integrity
from harness.orchestration.workflow.resolver_state import (
    COMMIT_PLAN_ENTRY_ID,
    PROHIBITIONS,
    RESOLVER_BATCH_KIND,
    RESOLVER_NEXT_ACTION,
    budget,
    is_resolver_brief,
    last_cause,
    same_target_fixes,
    write_event,
)

_FINISHED_STATES = {"completed", "failed", "blocked", "not-required", "abandoned"}
_TICKET_IN_SUBJECT = re.compile(r"\(#(\d+)\)")


# -- the route -------------------------------------------------------------------------------------


def _resolver_batches(root: Path, record_id: str) -> list[JsonObject]:
    directory = _records_root(root) / "batches"
    found = [
        _read_object(path, "batch record")
        for path in sorted(directory.glob("batch-*.json") if directory.is_dir() else [])
    ]
    return [
        item
        for item in found
        if isinstance(item.get("resolver"), dict)
        and item["resolver"].get("integration_record_id") == record_id
    ]


def _open_batch(batches: list[JsonObject]) -> JsonObject | None:
    for item in batches:
        if item.get("state") not in _FINISHED_STATES:
            return item
    return None


def _git_lines(worktree: Path, *arguments: str) -> list[str]:
    return [line for line in _git(worktree, *arguments).splitlines() if line]


def _plan_requirements(root: Path, ticket: str) -> list[str] | None:
    """The definition of done of the newest plan recorded for ``ticket``, if any."""
    directory = _records_root(root) / "plans"
    plans = [
        _read_object(path, "batch plan")
        for path in sorted(directory.glob("*.json") if directory.is_dir() else [])
    ]
    plans = [item for item in plans if item.get("ticket") == ticket]
    if not plans:
        return None
    newest = max(plans, key=lambda item: str(item.get("created_at")))
    items = newest.get("definition_of_done")
    return [item for item in items if isinstance(item, str)] if items else None


def _target_side(root: Path, worktree: Path, base: str, tip: str) -> JsonObject:
    """What the commits that landed on the target since the merge base asked for: the plan record
    of the ticket named by ``(#N)`` in a subject, otherwise the commit's own subject and body."""
    tickets: list[str] = []
    requirements: list[str] = []
    for sha in _git_lines(worktree, "rev-list", "--reverse", f"{base}..{tip}", "--"):
        subject = _git(worktree, "show", "-s", "--format=%s", sha)
        body = _git(worktree, "show", "-s", "--format=%b", sha)
        named = [f"#{number}" for number in _TICKET_IN_SUBJECT.findall(subject)]
        planned = [(ticket, _plan_requirements(root, ticket)) for ticket in named]
        used = [(ticket, items) for ticket, items in planned if items]
        if used:
            for ticket, items in used:
                tickets.append(ticket)
                requirements.extend(items or [])
        else:
            requirements.append(f"{subject}\n\n{body}".strip())
        tickets.extend(ticket for ticket, items in planned if not items)
    return {
        "tickets": list(dict.fromkeys(tickets)),
        "requirements": list(dict.fromkeys(requirements)),
    }


def _conflict_scope(
    worktree: Path, base: str, candidate: str, tip: str, conflicting: list[str]
) -> list[str]:
    return sorted(
        {
            *conflicting,
            *_changed_files_between(worktree, base, candidate),
            *_changed_files_between(worktree, base, tip),
        }
    )


def _exhaust(
    ledger: LifecycleLedger,
    root: Path,
    record: JsonObject,
    target: str,
    reason: str,
    open_batch: JsonObject | None,
) -> CoordinatorError:
    """Stop only this task's resolver: the event and the branch stay as evidence, an open resolver
    batch is blocked, and no neighbouring batch is touched.  The cause decides the route."""
    cause = last_cause(root, record["integration_record_id"])
    route = "developer-retry" if cause == "task-defect" else "same-resolver"
    write_event(
        ledger,
        root,
        "exhausted",
        {
            "integration_record_id": record["integration_record_id"],
            "target_sha": target,
        },
        ticket=record["identity"]["ticket"],
        reason=reason,
        cause=cause,
        route=route,
    )
    if open_batch is not None:
        open_batch["state"] = "blocked"
        open_batch.pop("next_action", None)
        open_batch.pop("required_next_role", None)
        _replace_record(ledger, BatchRecord.from_dict(open_batch))
    if route == "developer-retry":
        remedy = (
            "the conflict comes from the ticket's own defect: return it to a regular developer "
            "dispatch (batch decide --decision retry on the ticket's batch); the branch and the "
            "resolver evidence are preserved"
        )
    else:
        remedy = (
            "the integration is incompatible with the ticket: a human records the decision "
            "('integration resolver-event --kind human-decision --extends-budget', which also grants "
            "one more round of same-target fixes) and the same resolver role continues ('integration "
            "resolve' or 'batch decide --decision retry'); the branch and the evidence are preserved"
        )
    return CoordinatorError(
        f"the conflict-resolver {reason} for target {target}; only this task stops",
        remedy=remedy,
    )


def integration_resolve(args: argparse.Namespace) -> JsonObject:
    """Hand a textual conflict to a conflict-resolver: a new batch with its brief section.

    Nothing is written to Git.  A clean rebase is ``integration refresh``'s job and is refused
    here; so is a third automatic target (a human decision extends the budget) and a same-target
    fix beyond the project retry budget.  Repeating the route returns the open resolver batch."""
    repo = _repo(args)
    root = _state_root(args, repo)
    config = core_config._config(repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        record = integration._resolve_record(root, args)
        integration._check_history_unchanged(root, record)
        identity = record["identity"]
        record_id = record["integration_record_id"]
        pair = pr_refresh.current_pair(root, record)
        remote, ref = identity["remote"], identity["integration_ref"]
        tip = _remote_branch_tip(repo, remote, ref)
        if tip is None:
            raise CoordinatorError(
                f"integration ref {ref!r} is not a branch of remote {remote!r}",
                remedy="check the integration ref and the remote, then retry",
            )
        existing = _resolver_batches(root, record_id)
        open_batch = _open_batch(existing)
        if open_batch is not None:
            if (
                open_batch["resolver"]["target_sha"] == tip
                and open_batch["resolver"]["candidate_sha"] == pair["candidate_sha"]
            ):
                return _result("existing", record, open_batch, root, config)
            raise CoordinatorError(
                f"resolver batch {open_batch['batch_id']} for target "
                f"{open_batch['resolver']['target_sha']} is still open",
                remedy="finish it (publish) or close it ('batch abandon') before resolving the new target",
            )
        if tip == pair["target_sha"]:
            raise CoordinatorError(
                "the branch already is at the integration tip: there is no conflict to resolve",
                remedy="run 'integration status' to see whether the pair needs a new check",
            )
        branch = identity["branch"]
        _validate_branch(repo, branch)
        source = _load_batch(root, identity["source_batch_id"])
        worktree = Path(source["worktree"])
        _git(worktree, "fetch", remote, "--", ref)
        pr_refresh._require_clean_own_branch(worktree, branch, pair["candidate_sha"])
        conflicting = pr_refresh.conflicting_files(worktree, branch, tip)
        if not conflicting:
            raise CoordinatorError(
                "the rebase onto the integration tip is clean: nothing for a resolver to do",
                remedy="run 'integration refresh'; a clean rebase spends no resolver cycle",
            )
        current = budget(root, config, record_id)
        fixes = same_target_fixes(root, record_id, tip)
        if tip in current["spent_targets"]:
            if fixes >= current["internal_fix_budget"]:
                raise _exhaust(
                    ledger, root, record, tip, "internal fix budget is spent", None
                )
        elif current["remaining"] == 0:
            raise _exhaust(
                ledger, root, record, tip, "automatic cycles are spent", None
            )
        merge_base = _git(worktree, "merge-base", pair["candidate_sha"], tip)
        scope = _conflict_scope(
            worktree, merge_base, pair["candidate_sha"], tip, conflicting
        )
        resolver: JsonObject = {
            "integration_record_id": record_id,
            "source_batch_id": identity["source_batch_id"],
            "ticket": identity["ticket"],
            "candidate_sha": pair["candidate_sha"],
            "target_sha": tip,
            "merge_base": merge_base,
            "conflicting_files": conflicting,
            "sides": {
                "candidate": {
                    "ticket": identity["ticket"],
                    "requirements": list(source["definition_of_done"]),
                },
                "target": _target_side(root, worktree, merge_base, tip),
            },
            "scope": scope,
            "prohibitions": list(PROHIBITIONS),
            "commit_plan": [
                {
                    "id": COMMIT_PLAN_ENTRY_ID,
                    "summary": "Resolve the conflict preserving both sides and commit the result",
                    "expected_paths": scope,
                    "covers": [1, 2, 3],
                }
            ],
        }
    created = create_batch(
        argparse.Namespace(
            repo=str(repo),
            state_dir=getattr(args, "state_dir", None),
            ticket=identity["ticket"],
            branch=branch,
            worktree=str(worktree),
            zone=None,
            integration_ref=ref,
            goal=f"Resolve the textual conflict of {identity['ticket']} with the integration tip {tip}",
            definition_of_done=[
                f"Rebase the issue branch onto the exact target {tip} and resolve every conflict",
                "Preserve the requirements of both sides and add no behaviour outside them",
                "Commit the resolution and pass the approved verification commands",
            ],
            prohibited_change=list(PROHIBITIONS),
            dependency=None,
            required_gate=None,
            allowed_path=scope,
            expected_file=conflicting,
            expected_service=["conflict-resolution"],
            expected_changed_lines=max(10, 20 * len(conflicting)),
            expected_context_tokens=None,
        )
    )
    with _ledger_lock(ledger):
        batch = _load_batch(root, created["batch_id"])
        if batch["base_commit"] != tip:
            raise CoordinatorError(
                "the integration ref moved while the resolver batch was planned",
                remedy="repeat 'integration resolve' for the new target",
            )
        batch.update(
            {
                "kind": RESOLVER_BATCH_KIND,
                "next_action": RESOLVER_NEXT_ACTION,
                "resolver": resolver,
            }
        )
        _replace_record(ledger, BatchRecord.from_dict(batch))
        record_path = _records_root(root) / ResolverRecord.directory
        resolver_id = ResolverRecord.derive_id(record_id)
        if not (record_path / f"{resolver_id}.json").is_file():
            _write_record(
                ledger,
                ResolverRecord.from_dict(
                    {
                        "resolver_id": resolver_id,
                        "contract": 1,
                        "integration_record_id": record_id,
                        "ticket": identity["ticket"],
                        "branch": branch,
                        "recorded_at": utils._now(),
                    }
                ),
            )
    return _result("created", record, batch, root, config)


def _result(
    state: str, record: JsonObject, batch: JsonObject, root: Path, config: JsonObject
) -> JsonObject:
    resolver = batch["resolver"]
    return {
        "state": state,
        "integration_record_id": record["integration_record_id"],
        "ticket": resolver["ticket"],
        "branch": batch["branch"],
        "batch_id": batch["batch_id"],
        "batch_state": batch["state"],
        "next_action": batch.get("next_action"),
        "candidate_sha": resolver["candidate_sha"],
        "target_sha": resolver["target_sha"],
        "conflicting_files": resolver["conflicting_files"],
        "allowed_paths": batch["allowed_paths"],
        "budget": budget(root, config, record["integration_record_id"]),
        "next": [
            "batch approve --batch <batch_id> --approved-by <name> --approved-at <time>",
            "dispatch create --batch <batch_id> --role conflict-resolver --purpose work --propose",
        ],
    }


# -- the brief ---------------------------------------------------------------------------------------


def brief_section(
    root: Path,
    config: JsonObject,
    batch: JsonObject,
    verification_commands: list[str],
    report_staging_path: str,
) -> JsonObject:
    """The immutable ``resolver`` section of a conflict-resolver brief."""
    resolver = batch.get("resolver")
    if not isinstance(resolver, dict):
        raise CoordinatorError(
            "a conflict-resolver dispatch needs a resolver batch",
            remedy="create the batch with 'integration resolve'; it is the only route to this role",
        )
    return {
        "ticket": resolver["ticket"],
        "sides": resolver["sides"],
        "candidate_sha": resolver["candidate_sha"],
        "target_sha": resolver["target_sha"],
        "conflicting_files": resolver["conflicting_files"],
        "scope": resolver["scope"],
        "prohibitions": resolver["prohibitions"],
        "commit_plan": resolver["commit_plan"],
        "checks": list(verification_commands),
        "budget": budget(root, config, resolver["integration_record_id"]),
        "report_staging_path": report_staging_path,
    }


# -- events recorded by a human ---------------------------------------------------------------------


def resolver_event(args: argparse.Namespace) -> JsonObject:
    """Record a human decision or a scope change of a resolver dispatch as its own audit event.

    A human decision answers the options a checkpointed resolver listed; it never spends a cycle
    and, with ``--extends-budget``, grants one more automatic target.  A scope change is only
    recorded: the new scope runs under a regular newly approved dispatch, the original brief is
    never rewritten."""
    repo = _repo(args)
    root = _state_root(args, repo)
    kind = getattr(args, "kind", None)
    if kind not in {"human-decision", "scope-change"}:
        raise CoordinatorError(
            "resolver event kind must be human-decision or scope-change",
            remedy="pass --kind human-decision or --kind scope-change",
        )
    decided_by = getattr(args, "decided_by", None)
    note = getattr(args, "note", None)
    if not _non_empty(decided_by) or not _non_empty(note):
        raise CoordinatorError(
            "a resolver event needs --decided-by and --note",
            remedy="name who decided and state the decision or the reason in --note",
        )
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        record = integration._resolve_record(root, args)
        record_id = record["integration_record_id"]
        dispatch = _load_dispatch(root, args.dispatch)
        batch = _load_batch(root, dispatch["batch_id"])
        _validate_batch_integrity(root, batch)
        resolver = batch.get("resolver")
        if (
            not is_resolver_brief(dispatch)
            or not isinstance(resolver, dict)
            or resolver.get("integration_record_id") != record_id
        ):
            raise CoordinatorError(
                "the dispatch is not a conflict-resolver dispatch of this integration record",
                remedy="pass the --dispatch of the resolver the record's conflict was handed to",
            )
        entry = next(
            item
            for item in batch["dispatches"]
            if item["dispatch_id"] == dispatch["dispatch_id"]
        )
        members: JsonObject = {
            "integration_record_id": record_id,
            "target_sha": resolver["target_sha"],
            "dispatch_id": dispatch["dispatch_id"],
        }
        fields: JsonObject = {
            "ticket": resolver["ticket"],
            "decided_by": decided_by.strip(),
            "note": note.strip(),
        }
        if kind == "human-decision":
            extends = bool(getattr(args, "extends_budget", False))
            checkpointed = entry.get("state") == "checkpointed"
            if not checkpointed and not extends:
                raise CoordinatorError(
                    "a human decision answers a checkpointed resolver",
                    remedy="the resolver writes a checkpoint listing the options first; record the decision after it "
                    "(only a decision that extends the budget after the automatic cycles are spent may stand alone)",
                )
            checkpoint_id = None
            if checkpointed:
                checkpoint_id = [
                    item
                    for item in batch.get("checkpoints", [])
                    if item["dispatch_id"] == dispatch["dispatch_id"]
                ][-1]["checkpoint_id"]
            fields.update(
                {
                    "option": getattr(args, "option", None),
                    "extends_budget": extends,
                    "checkpoint_id": checkpoint_id,
                }
            )
            members["discriminator"] = (
                checkpoint_id or f"extension:{utils._canonical(fields)}"
            )
        else:
            members["discriminator"] = f"scope:{utils._canonical(fields)}"
        return write_event(ledger, root, kind, members, **fields)
