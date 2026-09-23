"""Carrying an approved brief: hand-off to a transport, and following it to publication.

`dispatch.py` decides whether a brief may exist and writes it; from here on the brief is fixed.
These commands only move it: they check out the worktree a role will work in, hand the file to the
selected transport, watch the dispatch while it runs, and publish the accepted candidate.  None of
them may widen scope, re-approve a transition or edit the brief.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

from harness.errors import INTERNAL_INVARIANT_REMEDY
from harness.orchestration.core import config as core_config
from harness.orchestration.core import utils
from harness.orchestration.core.config import (
    _context_advisory,
)
from harness.orchestration.core.constants import (
    LIVE_DISPATCH_STATES,
    ROLE_TRANSPORTS,
)
from harness.orchestration.core.git_utils import (
    _changed_files_between,
    _commit_changed_files,
    _git,
)
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _non_empty,
    _read_object,
    _repo,
    _safe_id,
    _sanitise,
    _silent_seconds,
)
from harness.orchestration.core.workspace import (
    _agent_inbox,
)
from harness.orchestration.ledger.ledger_ops import (
    _ledger_lock,
    _load_batch,
    _load_dispatch,
    _load_dispatch_status,
    _records_root,
    _replace_record,
    _state_root,
)
from harness.orchestration.ledger.lifecycle import (
    BatchRecord,
    DispatchRecord,
    DispatchStatusRecord,
    LifecycleLedger,
)
from harness.orchestration.workflow.attention import (
    _flag_stale_dispatch,
)
from harness.orchestration.workflow.history import (
    _accepted_qa_for_candidate,
    _validate_batch_integrity,
    _validate_dispatch,
)
from harness.orchestration.workflow.reports import (
    _persist_report,
)


def _validate_checkout(
    checkout: Path, candidate: str, base: str | None, scope: list[str]
) -> None:
    if not checkout.is_dir():
        raise CoordinatorError(
            f"review checkout does not exist: {checkout}",
            remedy=f"pass --checkout pointing at an existing worktree, not {checkout}",
        )
    try:
        actual = _git(checkout, "rev-parse", "--verify", "HEAD^{commit}")
    except CoordinatorError as exc:
        raise CoordinatorError(
            "review checkout is not a git worktree",
            remedy="pass --checkout pointing at a real git worktree",
        ) from exc
    if actual != candidate:
        raise CoordinatorError(
            "review checkout HEAD does not match the pinned candidate commit",
            remedy="checkout the pinned candidate commit in the review worktree before sending this dispatch",
        )
    # Ignored virtual environments and tool caches do not alter the pinned candidate.  Treating
    # them as a dirty review checkout sends the coordinator into needless recovery/review loops.
    status = _git(checkout, "status", "--porcelain", "--untracked-files=normal")
    mutable_paths = []
    for line in status.splitlines():
        path = line[3:].split(" -> ", 1)[-1].replace("\\", "/")
        if not path.startswith(".harness/orchestration/state/"):
            mutable_paths.append(path)
    if mutable_paths:
        raise CoordinatorError(
            "review checkout must be clean; mutable files are outside the pinned scope",
            remedy="clean the review checkout (git status --porcelain must be empty) before sending this dispatch",
        )
    actual_files = (
        _changed_files_between(checkout, base, candidate)
        if base
        else _commit_changed_files(checkout, candidate)
    )
    if actual_files != scope:
        raise CoordinatorError(
            "review checkout changed files do not match the immutable review scope",
            remedy="the review checkout's changed files must exactly match the immutable review scope; re-checkout the pinned candidate",
        )


def send_dispatch(args: argparse.Namespace) -> JsonObject:
    repo = _repo(args)
    root = _state_root(args, repo)
    dispatch_record = _load_dispatch(root, args.dispatch)
    if dispatch_record.get("role") == "qa":
        raise CoordinatorError(
            "QA dispatches must run through the clean-room QA lane, never a runtime adapter",
            remedy="send a QA dispatch through the clean-room QA lane (qa run), not a runtime adapter",
        )
    if dispatch_record.get("purpose") == "publish":
        raise CoordinatorError(
            "publish-only dispatches must use the verified coordinator publish boundary",
            remedy="send a publish-only dispatch through the coordinator publish command, not a runtime adapter",
        )
    transport = dispatch_record.get("resolved_transport")
    if transport not in ROLE_TRANSPORTS:
        raise CoordinatorError(
            "dispatch record has an invalid transport",
            remedy="the dispatch record has an invalid transport -- "
            + INTERNAL_INVARIANT_REMEDY,
        )
    adapter: Path | None = None
    if transport == "orca":
        if not args.adapter:
            raise CoordinatorError(
                "an orca-transport dispatch requires an explicit runtime adapter",
                remedy="pass --adapter for an orca-transport dispatch",
            )
        adapter = Path(args.adapter).resolve()
        if not adapter.is_file():
            raise CoordinatorError(
                f"runtime adapter does not exist: {adapter}",
                remedy=f"install or configure the runtime adapter at {adapter}",
            )
    elif args.adapter or args.adapter_arg:
        raise CoordinatorError(
            "an in-process dispatch runs inside this session and takes no runtime adapter",
            remedy="omit --adapter for an in-process dispatch; it runs inside this session",
        )
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        dispatch = _load_dispatch(root, args.dispatch)
        batch = _load_batch(root, dispatch["batch_id"])
        _validate_batch_integrity(root, batch)
        config = core_config._config(repo)
        _validate_dispatch(repo, config, root, batch, dispatch)
        if dispatch["role"] == "code-review":
            checkout = Path(args.checkout).resolve() if args.checkout else None
            if checkout is None:
                raise CoordinatorError(
                    "code-review dispatch requires an explicit checkout: pass "
                    "--checkout <path-to-a-worktree-pinned-at-candidate_commit> "
                    f"(candidate_commit={dispatch['candidate_commit']}); "
                    "other roles omit --checkout entirely",
                    remedy="pass --checkout <path-to-a-worktree-pinned-at-candidate_commit> for a code-review dispatch; other roles omit --checkout",
                )
            _validate_checkout(
                checkout,
                dispatch["candidate_commit"],
                dispatch["review_base"],
                dispatch["review_scope"],
            )
        status = _load_dispatch_status(root, dispatch["dispatch_id"])
        if status.get("state") != "approved":
            raise CoordinatorError(
                "only an approved dispatch may be sent to a runtime adapter",
                remedy="only send an approved dispatch to a runtime adapter",
            )
        entry = next(
            (
                item
                for item in batch.get("dispatches", [])
                if item["dispatch_id"] == dispatch["dispatch_id"]
            ),
            None,
        )
        if not entry or entry.get("state") != "approved":
            raise CoordinatorError(
                "dispatch was already sent or is not registered in its batch",
                remedy="the dispatch was already sent or is not registered in its batch -- "
                + INTERNAL_INVARIANT_REMEDY,
            )
        brief_path = (
            _records_root(root)
            / DispatchRecord.directory
            / f"{_safe_id(dispatch['dispatch_id'], 'dispatch')}.json"
        )
        if adapter is not None:
            command = (
                [str(adapter)]
                if adapter.suffix.lower() != ".py"
                else [sys.executable, str(adapter)]
            )
            adapter_args = args.adapter_arg or []
            if any(
                argument in {"dispatch", "--repo", "--brief"}
                or argument.startswith(("--repo=", "--brief="))
                for argument in adapter_args
            ):
                raise CoordinatorError(
                    "adapter arguments cannot override dispatch, repo or brief",
                    remedy="pass adapter arguments that do not collide with --dispatch, --repo or --brief",
                )
            command.append("dispatch")
            command.extend(adapter_args)
            command.extend(["--repo", str(repo), "--brief", str(brief_path)])
            # Windows consoles default to a legacy ANSI codepage: without an explicit encoding a
            # UTF-8 adapter message is mojibaked before it ever reaches the coordinator error.
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()
                raise CoordinatorError(
                    f"runtime adapter rejected dispatch: {detail}",
                    remedy=f"inspect the runtime adapter's rejection above: {detail}",
                )
        for entry in batch["dispatches"]:
            if entry["dispatch_id"] == dispatch["dispatch_id"]:
                entry["state"] = "dispatched"
                break
        else:
            raise CoordinatorError(
                "dispatch is not registered in its batch",
                remedy="the dispatch is not registered in its batch -- "
                + INTERNAL_INVARIANT_REMEDY,
            )
        sent_at = utils._now()
        _safe_id(dispatch["dispatch_id"], "dispatch")
        _replace_record(
            ledger,
            DispatchStatusRecord.from_dict(
                {
                    "dispatch_id": dispatch["dispatch_id"],
                    "state": "dispatched",
                    "updated_at": sent_at,
                    "heartbeat_at": sent_at,
                }
            ),
        )
        _safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, BatchRecord.from_dict(batch))
    return {
        "dispatch_id": dispatch["dispatch_id"],
        "state": "dispatched",
        "transport": transport,
        "brief": str(brief_path),
        "expected_model": dispatch["resolved_model"],
        "report_staging_path": dispatch.get("report_staging_path")
        or str(_agent_inbox(repo) / f"{dispatch['dispatch_id']}.json"),
        "next_role_action": "dispatch self-report",
        "heartbeat": {
            "every_seconds": dispatch.get("liveness", {}).get(
                "heartbeat_every_seconds"
            ),
            "stale_after_seconds": dispatch.get("liveness", {}).get(
                "stale_after_seconds"
            ),
            "instruction": (
                "after self-report, send dispatch heartbeat now and at least once per "
                f"{dispatch.get('liveness', {}).get('heartbeat_every_seconds')} seconds while working"
            ),
        },
    }


def wait_dispatch(args: argparse.Namespace) -> JsonObject:
    """Wait locally for a significant event; heartbeat updates never reach the coordinator chat."""
    repo = _repo(args)
    root = _state_root(args, repo)
    timeout, interval, threshold = args.timeout, args.poll_interval, args.stale_after
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1
        for value in (timeout, interval, threshold)
    ):
        raise CoordinatorError(
            "timeout, poll-interval and stale-after must be positive integers",
            remedy="pass --timeout, --poll-interval and --stale-after as positive integers",
        )
    deadline = time.monotonic() + timeout
    ledger = LifecycleLedger(root)
    while True:
        with _ledger_lock(ledger):
            dispatch = _load_dispatch(root, args.dispatch)
            status = _load_dispatch_status(root, dispatch["dispatch_id"])
            state = status.get("state")
            if state == "reported":
                return {"dispatch_id": dispatch["dispatch_id"], "event": "reported"}
            if state == "rate_limited":
                return {
                    "dispatch_id": dispatch["dispatch_id"],
                    "event": "rate_limited",
                    "retry_not_before": status.get("retry_not_before"),
                }
            if (
                state == "blocked"
                and status.get("model_self_report", {}).get("match") is False
            ):
                return {
                    "dispatch_id": dispatch["dispatch_id"],
                    "event": "model_mismatch",
                }
            if (
                state == "blocked"
                and status.get("worktree_attestation", {}).get("match") is False
            ):
                return {
                    "dispatch_id": dispatch["dispatch_id"],
                    "event": "worktree_mismatch",
                }
            if state in {"failed", "abandoned", "cancelled"}:
                return {
                    "dispatch_id": dispatch["dispatch_id"],
                    "event": "failed",
                    "state": state,
                }
            if state in LIVE_DISPATCH_STATES and _silent_seconds(status) >= threshold:
                _flag_stale_dispatch(
                    ledger, repo, root, dispatch, _silent_seconds(status)
                )
                return {
                    "dispatch_id": dispatch["dispatch_id"],
                    "event": "stale",
                    "silent_seconds": _silent_seconds(status),
                }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"dispatch_id": args.dispatch, "event": "timeout"}
        time.sleep(min(interval, max(1, int(remaining))))


def dispatch_status(args: argparse.Namespace) -> JsonObject:
    """Liveness view the coordinator session polls; a stale entry is a blocker to surface, never a
    reason for the coordinator to change state on its own."""
    repo = _repo(args)
    root = _state_root(args, repo)
    threshold = args.stale_after
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
        raise CoordinatorError(
            "stale-after must be a positive number of seconds",
            remedy="pass --stale-after as a positive number of seconds",
        )
    config = core_config._config(repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        entries: list[JsonObject] = []
        for path in sorted(
            (_records_root(root) / DispatchStatusRecord.directory).glob(
                "dispatch-*.json"
            )
        ):
            status = _read_object(path, "dispatch status")
            if args.dispatch and status.get("dispatch_id") != args.dispatch:
                continue
            dispatch = _load_dispatch(root, cast(str, status.get("dispatch_id")))
            if args.batch and dispatch.get("batch_id") != args.batch:
                continue
            batch = _load_batch(root, dispatch["batch_id"])
            telemetry = [
                record
                for record in batch.get("telemetry", [])
                if record.get("dispatch_id") == dispatch["dispatch_id"]
            ]
            latest_telemetry = max(
                telemetry, key=lambda record: record["recorded_at"], default=None
            )
            pressure = [
                item
                for item in batch.get("context_pressure", [])
                if item.get("dispatch_id") == dispatch["dispatch_id"]
            ]
            live = status.get("state") in LIVE_DISPATCH_STATES
            silent = _silent_seconds(status) if live else 0
            entries.append(
                {
                    "dispatch_id": dispatch["dispatch_id"],
                    "batch_id": dispatch["batch_id"],
                    # The coordinator session needs to see whose work a leftover dispatch belongs to
                    # before it proposes a new batch for the same ticket.
                    "ticket": dispatch["ticket"],
                    "role": dispatch["role"],
                    "state": status.get("state"),
                    "transport": dispatch.get("resolved_transport"),
                    "resolved_model": dispatch["resolved_model"],
                    "model_self_report": status.get("model_self_report"),
                    "heartbeat_at": status.get("heartbeat_at")
                    or status.get("updated_at"),
                    "silent_seconds": silent,
                    "stale": live and silent >= threshold,
                    "telemetry": latest_telemetry,
                    "context_pressure": pressure[-1] if pressure else None,
                    "needs_attention": bool(batch.get("needs_attention", False)),
                    "context_advisory": _context_advisory(
                        config,
                        latest_telemetry["max_context_tokens"]
                        if latest_telemetry
                        else None,
                    ),
                }
            )
    return {
        "stale_after_seconds": threshold,
        "stale": [entry["dispatch_id"] for entry in entries if entry["stale"]],
        "dispatches": entries,
    }


def publish_dispatch(args: argparse.Namespace) -> JsonObject:
    """Push one QA-accepted candidate through an approved publish-only brief."""
    repo = _repo(args)
    root = _state_root(args, repo)
    remote = args.remote.strip() if _non_empty(args.remote) else ""
    if not remote:
        raise CoordinatorError(
            "publish remote must be a non-empty string",
            remedy="pass --remote as a non-empty string",
        )
    if remote not in _git(repo, "remote").splitlines():
        raise CoordinatorError(
            "publish remote is not configured for this repository",
            remedy="add the named remote to this repository (git remote add) before publishing",
        )
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        dispatch = _load_dispatch(root, args.dispatch)
        batch = _load_batch(root, dispatch["batch_id"])
        _validate_batch_integrity(root, batch)
        _validate_dispatch(repo, core_config._config(repo), root, batch, dispatch)
        if dispatch["purpose"] != "publish":
            raise CoordinatorError(
                "only a publish-only dispatch may push a candidate",
                remedy="only a publish-only dispatch may push a candidate",
            )
        status = _load_dispatch_status(root, dispatch["dispatch_id"])
        entry = next(
            (
                item
                for item in batch["dispatches"]
                if item["dispatch_id"] == dispatch["dispatch_id"]
            ),
            None,
        )
        if (
            not entry
            or entry.get("state") != "approved"
            or status.get("state") != "approved"
        ):
            raise CoordinatorError(
                "publish requires an approved, unsent publish-only dispatch",
                remedy="approve a publish-only dispatch, and send it, before publishing",
            )
        candidate = dispatch["candidate_commit"]
        _accepted_qa_for_candidate(root, batch, candidate)
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "push",
                remote,
                f"{candidate}:refs/heads/{dispatch['branch']}",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if result.returncode != 0:
            detail = _sanitise((result.stderr or result.stdout).strip())
            raise CoordinatorError(
                f"could not publish the accepted candidate: {detail or 'unknown error'}",
                remedy="inspect the git push error above and fix it before retrying publish",
            )
        published = _git(
            repo, "ls-remote", "--heads", remote, f"refs/heads/{dispatch['branch']}"
        )
        if not published or published.split()[0] != candidate:
            raise CoordinatorError(
                "remote branch does not resolve to the accepted QA candidate",
                remedy="push exactly the accepted QA candidate commit to the remote branch",
            )
        entry["state"] = "dispatched"
        _safe_id(dispatch["dispatch_id"], "dispatch")
        _replace_record(
            ledger,
            DispatchStatusRecord.from_dict(
                {
                    "dispatch_id": dispatch["dispatch_id"],
                    "state": "dispatched",
                    "updated_at": utils._now(),
                }
            ),
        )
        _safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, BatchRecord.from_dict(batch))
        changed = (
            _changed_files_between(repo, batch["base_commit"], candidate)
            if batch.get("base_commit")
            else _commit_changed_files(repo, candidate)
        )
        report = {
            "dispatch_id": dispatch["dispatch_id"],
            "ticket": dispatch["ticket"],
            "role": "developer",
            "outcome": "completed",
            "output": f"published accepted QA candidate {candidate} to {remote}/{dispatch['branch']}",
            "commit_sha": candidate,
            "changed_files": changed,
            "checks_run": [
                {
                    "command": command,
                    "result": "pass",
                    "evidence": f"accepted clean-room QA evidence for {candidate}",
                }
                for command in dispatch["verification_commands"]
            ],
            "risks": "none",
            "blockers": "none",
            "next_coordinator_action": "accept publication or inspect remote evidence",
            "report_language": "ru",
        }
        report_path = _persist_report(ledger, root, batch, dispatch, report)
    return {
        "dispatch_id": dispatch["dispatch_id"],
        "state": "reported",
        "report": str(report_path),
        "candidate_commit": candidate,
    }
