"""Carrying an approved brief: hand-off to a transport, and following it to publication.

`dispatch.py` decides whether a brief may exist and writes it; from here on the brief is fixed.
These commands only move it: they check out the worktree a role will work in, hand the file to the
selected transport, watch the dispatch while it runs, and publish the accepted candidate.  None of
them may widen scope, re-approve a transition or edit the brief.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

from harness.orchestration import (
    hook_policy,
    operation_access,
    runtime_access,
    runtime_probe,
)
from harness.errors import INTERNAL_INVARIANT_REMEDY
from harness.storage import storage_path
from harness.orchestration.core import config as core_config
from harness.orchestration.core import state_view
from harness.orchestration.core import utils
from harness.orchestration.core.config import (
    _attention_policy,
    _context_advisory,
    _execution_policy,
)
from harness.orchestration.core.constants import (
    LIVE_DISPATCH_STATES,
    ROLE_TRANSPORTS,
    RUNTIME_PATH_PREFIXES,
)
from harness.orchestration.core.git_utils import (
    _changed_files_between,
    _commit_changed_files,
    _git,
    _remote_branch_tip,
    git_environment,
    git_failure,
)
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _canonical,
    _non_empty,
    _read_object,
    _repo,
    _safe_id,
    _sanitise,
    _silent_seconds,
)
from harness.orchestration.core.workspace import (
    _agent_inbox,
    _prepare_agent_inbox,
)
from harness.orchestration.ledger.ledger_ops import (
    read_state,
    store_is_authority,
    LedgerBusyError,
    _ledger_lock,
    _load_batch,
    _load_dispatch,
    _load_dispatch_status,
    _records_root,
    _replace_record,
    _state_root,
)
from harness.orchestration.ledger.lifecycle import (
    FORCE_RELEASE_COMMAND,
    BatchRecord,
    DispatchRecord,
    DispatchStatusRecord,
    LifecycleLedger,
)
from harness.orchestration.runtime_attestation import ISSUE_WORKTREE_WRITER_ROLES
from harness.orchestration.workflow import failure_domains, rebase
from harness.orchestration.workflow.attention import (
    _flag_stale_dispatch,
    staged_report,
)
from harness.orchestration.workflow.container_status import (
    MAX_ENGINE_READS,
    STOP_PROOFS,
    needs_engine,
    reconcile,
)
from harness.orchestration.workflow.history import (
    _accepted_qa_for_candidate,
    _validate_batch_integrity,
    _validate_dispatch,
)
from harness.orchestration.workflow.reports import (
    _persist_report,
)

# A push of one already verified commit is bounded; the limit only stops a hung transport.
PUBLISH_PUSH_TIMEOUT_SECONDS = 600


def _worker_brief_path(repo: Path, root: Path, dispatch: JsonObject) -> Path:
    """Export the validated store record for a worker that needs an ordinary file.

    The store remains authoritative. The copy survives send and is never replaced: a conflicting
    copy is an integrity failure, not permission to change the approved worker contract.
    """
    name = f"{_safe_id(dispatch['dispatch_id'], 'dispatch')}.json"
    if not store_is_authority(root):
        return _records_root(root) / DispatchRecord.directory / name
    _prepare_agent_inbox(repo)
    path = storage_path(repo, "scratch", "briefs", name)
    path.parent.mkdir(parents=True, exist_ok=True)
    expected = _canonical(dispatch).encode("utf-8")
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(expected)
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or path.read_bytes() != expected:
            raise CoordinatorError(
                "the exported worker brief differs from its approved store record",
                remedy="inspect the conflicting brief export before retrying; never edit the approved dispatch",
            ) from None
    path.chmod(0o400)
    return path


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
        if not path.startswith(RUNTIME_PATH_PREFIXES):
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


class AdapterUnavailable(CoordinatorError):
    """The OS refused to start the adapter; no worker was handed a brief."""


def _run_adapter(command: list[str], *, cwd: Path | None = None) -> JsonObject | None:
    """Hand the brief to a project runtime adapter.

    The adapter contract bounds the call: the adapter returns after it starts the worker. A worker
    session has no fixed duration, so no timeout is set here.
    """
    try:
        # Windows consoles default to a legacy ANSI codepage: without an explicit encoding a
        # UTF-8 adapter message is mojibaked before it ever reaches the coordinator error.
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise AdapterUnavailable(
            f"runtime adapter could not be started: {exc}",
            remedy="make the runtime adapter executable (or pass a .py adapter) and send the "
            "dispatch again; nothing was handed off",
        ) from exc
    if result.returncode != 0:
        detail = _sanitise((result.stderr or result.stdout).strip())
        try:
            refused = json.loads(result.stdout)
        except (ValueError, TypeError):
            refused = None
        from harness.orchestration.worker_observation import PROTOCOL

        error_type = (
            AdapterUnavailable
            if (
                isinstance(refused, dict)
                and refused.get("protocol") == PROTOCOL
                and refused.get("worker_started") is False
            )
            else CoordinatorError
        )
        raise error_type(
            f"runtime adapter rejected dispatch: {detail}",
            remedy=f"inspect the runtime adapter's rejection above: {detail}",
        )

    # Legacy adapters may print nothing or plain text. They remain usable, with no invented
    # observation capability. Structured receipts are preserved for the local monitor.
    try:
        receipt = json.loads(result.stdout)
    except (ValueError, TypeError):
        return None
    if not isinstance(receipt, dict):
        return None
    allowed = {
        "protocol",
        "dispatch_id",
        "worker_started",
        "launch_id",
        "runtime_session_id",
        "log_path",
        "terminal_path",
        "visibility",
    }
    safe: JsonObject = {
        key: _sanitise(value)[:4096] if isinstance(value, str) else value
        for key, value in receipt.items()
        if key in allowed and isinstance(value, (str, bool, int))
    }
    if isinstance(receipt.get("process"), dict):
        safe["process"] = {
            key: value
            for key, value in receipt["process"].items()
            if key in {"pid", "host", "boot_id", "start_time", "pid_namespace"}
            and isinstance(value, (str, int))
        }
    if isinstance(receipt.get("preflight"), dict):
        safe["preflight"] = {
            "worker_context": str(
                receipt["preflight"].get("worker_context", "unverified")
            )[:80]
        }
    return safe


class LaunchUnsettled(CoordinatorError):
    """The runtime did not confirm a launch; a repeat of ``dispatch send`` never launches twice."""


def execution_operation_id(dispatch: JsonObject) -> str:
    """The deterministic operation id of the first launch of a supervised brief:
    ``sha256([dispatch_id, brief_sha256, 1])`` in the canonical digest form (ADR 0021, section 4)."""
    brief_sha256 = hashlib.sha256(_canonical(dispatch).encode("utf-8")).hexdigest()
    key = _canonical([dispatch["dispatch_id"], brief_sha256, 1])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _launch_execution(repo: Path, dispatch: JsonObject) -> JsonObject:
    """Ask the runtime to launch the execution of a supervised dispatch from its stored brief
    and status; the answer is the receipt. A launch the OS refused (``engine_unavailable``) is
    a proven launch failure; any other refusal leaves the dispatch dispatched. Only a repeatable
    refusal advises a repeat of ``dispatch send``; any other carries the remedy of the runtime."""
    from harness.orchestration.runtime_client import RuntimeClient, RuntimeClientError

    try:
        reply = RuntimeClient(repo).call(
            "execution.launch",
            operation_id=execution_operation_id(dispatch),
            payload={"dispatch_id": dispatch["dispatch_id"]},
        )
    except RuntimeClientError as exc:
        if exc.code == "engine_unavailable":
            error_type = (
                AdapterUnavailable if "spawn_error" in exc.facts else CoordinatorError
            )
            raise error_type(
                f"the runtime could not launch the worker: {exc.message}",
                remedy=exc.remedy,
            ) from exc
        if not exc.retryable:
            raise LaunchUnsettled(
                f"the runtime refused the launch [{exc.code}]: {exc.message}",
                remedy=f"{exc.remedy}; the dispatch stays dispatched, and a repeat of dispatch "
                "send gets the same refusal",
            ) from exc
        raise LaunchUnsettled(
            f"the runtime did not confirm the launch [{exc.code}]: {exc.message}",
            remedy="repeat dispatch send; it never launches twice: the runtime answers the "
            "stored receipt, or adopts the process it started",
        ) from exc
    return reply.result


def observed_execution(repo: Path, dispatch_id: str) -> JsonObject | None:
    """The execution of a supervised dispatch, as the runtime observes it now."""
    from harness.orchestration.runtime_client import RuntimeClient

    reply = RuntimeClient(repo).call(
        "execution.observe",
        operation_id=f"execution.observe-{dispatch_id}-{time.time_ns()}",
        payload={"dispatch_id": dispatch_id},
    )
    execution = reply.result.get("execution")
    return cast(JsonObject, execution) if isinstance(execution, dict) else None


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
    if transport == "external":
        if not args.adapter:
            raise CoordinatorError(
                "an external dispatch requires an explicit runtime adapter",
                remedy="pass --adapter for an external dispatch",
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
    adapter_receipt: JsonObject | None = None
    with _ledger_lock(ledger):
        dispatch = _load_dispatch(root, args.dispatch)
        batch = _load_batch(root, dispatch["batch_id"])
        _validate_batch_integrity(root, batch)
        config = core_config._config(repo)
        _validate_dispatch(repo, config, root, batch, dispatch)
        from harness.orchestration.workflow import preserved_progress

        progress = dispatch.get("preserved_progress")
        if progress is not None:
            preserved_progress.require_current(repo, root, batch, progress)
        from harness.orchestration.workflow.recovery import verified_checkout

        worker_path = getattr(args, "worktree", None) or (
            args.checkout if dispatch["role"] == "code-review" else dispatch["worktree"]
        )
        if not worker_path:
            raise CoordinatorError(
                "no worker checkout selected",
                remedy="pass --worktree with the worker checkout; code-review also requires --checkout",
            )
        launch = verified_checkout(
            repo,
            {
                **dispatch,
                "worker_worktree": str(Path(dispatch["worktree"]).resolve())
                if dispatch["role"] in ISSUE_WORKTREE_WRITER_ROLES
                else str(Path(worker_path).resolve()),
            },
            worker_path,
        )
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
            if checkout != Path(worker_path).resolve():
                raise CoordinatorError(
                    "--checkout and --worktree name different worker checkouts",
                    remedy="select one canonical review checkout for both arguments",
                )
        status = _load_dispatch_status(root, dispatch["dispatch_id"])
        # A brief that pins ``execution`` is launched by the runtime supervisor (ADR 0022). Its
        # repeat from ``dispatched`` only asks the runtime again: the runtime never launches twice.
        supervised = bool(dispatch.get("execution"))
        relaunch = (
            supervised
            and status.get("state") == "dispatched"
            and isinstance(status.get("launch"), dict)
        )
        if status.get("state") != "approved" and not relaunch:
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
        if not relaunch and (not entry or entry.get("state") != "approved"):
            raise CoordinatorError(
                "dispatch was already sent or is not registered in its batch",
                remedy="the dispatch was already sent or is not registered in its batch -- "
                + INTERNAL_INVARIANT_REMEDY,
            )
        brief_path = _worker_brief_path(repo, root, dispatch)
        command = None
        adapter_preflight: JsonObject | None = None
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
            if adapter_args:
                # Only now: a send that fails any check above never runs the adapter (#679).
                adapter_preflight = runtime_probe.probe_adapter(
                    command, adapter_args, cwd=Path(launch["worktree"])
                )
                if adapter_preflight["status"] == "unsupported":
                    raise CoordinatorError(
                        "runtime adapter dispatch does not support "
                        + ", ".join(adapter_preflight["unsupported_arguments"]),
                        remedy="remove or repair the unsupported --adapter-arg flags and send the "
                        "same approved dispatch again; nothing was handed off and the brief is "
                        "unchanged",
                    )
            command.append("dispatch")
            command.extend(adapter_args)
            command.extend(["--repo", str(repo), "--brief", str(brief_path)])
        access_evidence = runtime_access.apply_plan(
            dispatch,
            transport,
            handoff=True,
            defer_handoff=True,
            command=tuple(command) if command is not None else None,
        )
        if supervised and access_evidence["status"] != "legacy-inherit":
            raise CoordinatorError(
                "a dispatch the runtime supervises takes no runtime_access provider hand-off",
                remedy="create the dispatch without a runtime_access provider, or on the file "
                "ledger; nothing was handed off and the brief is unchanged",
            )
        if not relaunch and entry is not None:
            # The guard above proved this is the batch's own entry for the dispatch.
            entry["state"] = "dispatched"
            sent_at = utils._now()
            _safe_id(dispatch["dispatch_id"], "dispatch")
            _replace_record(
                ledger,
                DispatchStatusRecord.from_dict(
                    {
                        **status,
                        "dispatch_id": dispatch["dispatch_id"],
                        "state": "dispatched",
                        "updated_at": sent_at,
                        "heartbeat_at": sent_at,
                        "sent_at": sent_at,
                        "runtime_access": access_evidence,
                        **(
                            {"adapter_receipt": adapter_receipt}
                            if adapter_receipt
                            else {}
                        ),
                        # The launch spec the runtime reads: never a value of the request.
                        **(
                            {
                                "launch": {
                                    "argv": list(command),
                                    "cwd": launch["worktree"],
                                }
                            }
                            if supervised and command is not None
                            else {}
                        ),
                        "worker_worktree": launch["worktree"],
                        "worker_snapshot_commit": launch["head_commit"],
                    }
                ),
            )
            _safe_id(batch["batch_id"], "batch")
            _replace_record(ledger, BatchRecord.from_dict(batch))
    # Reserve this handoff before launching. A concurrent send is now refused, and a worker
    # may acquire the ledger lock for its self-report while the adapter is still returning.
    execution_receipt: JsonObject | None = None
    if command is not None or access_evidence["status"] != "legacy-inherit":
        try:
            if supervised:
                execution_receipt = _launch_execution(repo, dispatch)
            elif access_evidence["status"] == "legacy-inherit":
                if command is not None:
                    adapter_receipt = _run_adapter(
                        command, cwd=Path(launch["worktree"])
                    )
            else:
                access_evidence = runtime_access.handoff_plan(
                    dispatch,
                    transport,
                    tuple(command) if command else None,
                    access_evidence,
                )
        except runtime_access.LaunchRefused:
            # The provider proved that nothing started: the dispatch is approved again, so the
            # same approval can be sent once more.
            with _ledger_lock(ledger):
                current = _load_dispatch_status(root, dispatch["dispatch_id"])
                current_batch = _load_batch(root, dispatch["batch_id"])
                if current.get("state") == "dispatched":
                    for item in current_batch["dispatches"]:
                        if item["dispatch_id"] == dispatch["dispatch_id"]:
                            item["state"] = "approved"
                    _replace_record(ledger, DispatchStatusRecord.from_dict(status))
                    _replace_record(ledger, BatchRecord.from_dict(current_batch))
            raise
        except LaunchUnsettled:
            raise
        except (CoordinatorError, runtime_access.AccessError) as exc:
            with _ledger_lock(ledger):
                current = _load_dispatch_status(root, dispatch["dispatch_id"])
                current_batch = _load_batch(root, dispatch["batch_id"])
                if current.get("state") == "dispatched":
                    current.update(
                        {
                            "state": "blocked",
                            "updated_at": utils._now(),
                            "runtime_failure": {
                                "kind": "runtime-unavailable",
                                "operation": "startup",
                                "worker_started": False
                                if isinstance(exc, AdapterUnavailable)
                                else None,
                                "snapshot_commit": dispatch.get("snapshot_commit"),
                                "error": exc.message,
                            },
                        }
                    )
                    for item in current_batch["dispatches"]:
                        if item["dispatch_id"] == dispatch["dispatch_id"]:
                            item["state"] = "blocked"
                    current_batch["state"] = "blocked"
                    _replace_record(ledger, DispatchStatusRecord.from_dict(current))
                    _replace_record(ledger, BatchRecord.from_dict(current_batch))
            raise CoordinatorError(
                exc.message,
                remedy="inspect dispatch status; batch auto-report observes a proven launch failure, "
                "batch auto-decide records its pause, then batch resume-stop requires human approval; "
                "an unknown worker outcome never permits a second launch",
            ) from exc
        if adapter_receipt is not None or access_evidence["status"] != "legacy-inherit":
            with _ledger_lock(ledger):
                current = _load_dispatch_status(root, dispatch["dispatch_id"])
                # Merge into the latest status; never overwrite a worker's self-report/report.
                current["runtime_access"] = access_evidence
                if adapter_receipt is not None:
                    current["adapter_receipt"] = adapter_receipt
                _replace_record(ledger, DispatchStatusRecord.from_dict(current))
    return {
        "dispatch_id": dispatch["dispatch_id"],
        "state": "dispatched",
        "runtime_access": access_evidence,
        "transport": transport,
        **({"adapter_receipt": adapter_receipt} if adapter_receipt else {}),
        **({"execution": execution_receipt} if execution_receipt else {}),
        "brief": str(brief_path),
        "expected_model": dispatch["resolved_model"],
        "worker_worktree": launch["worktree"],
        "worker_snapshot_commit": launch["head_commit"],
        "report_staging_path": dispatch.get("report_staging_path")
        or str(_agent_inbox(repo) / f"{dispatch['dispatch_id']}.json"),
        "next_role_action": "dispatch self-report",
        **({"adapter_preflight": adapter_preflight} if adapter_preflight else {}),
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


def _worker_stopped(worker: JsonObject | None) -> bool:
    """Whether the engine proved the container worker stopped; silence never does."""
    return worker is not None and worker["state"] in STOP_PROOFS


def wait_dispatch(args: argparse.Namespace) -> JsonObject:
    """Wait locally for a significant event; heartbeat updates never reach the coordinator chat."""
    repo = _repo(args)
    root = _state_root(args, repo)
    config = core_config._config(repo)
    execution = _execution_policy(config)
    timeout = (
        args.timeout
        if args.timeout is not None
        else execution["dispatch_wait_timeout_seconds"]
    )
    interval = (
        args.poll_interval
        if args.poll_interval is not None
        else execution["dispatch_poll_interval_seconds"]
    )
    threshold = (
        args.stale_after
        if args.stale_after is not None
        else _attention_policy(config)["stale_dispatch_seconds"]
    )
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
    from harness.orchestration.workflow import execution_reaction

    while True:
        # The ended execution of a supervised dispatch decides its transition first (ADR 0021,
        # section 6); a busy ledger defers that to the next poll.
        with contextlib.suppress(LedgerBusyError):
            execution_reaction.observe(repo, root, args.dispatch)
        # A ledger busy with another coordinator operation is a missed poll, not a failure: the
        # next poll retries it until this wait's own timeout.  Only the lock acquisition is
        # tolerated -- nothing below takes the lock again.
        with contextlib.suppress(LedgerBusyError), _ledger_lock(ledger):
            dispatch = _load_dispatch(root, args.dispatch)
            status = _load_dispatch_status(root, dispatch["dispatch_id"])
            state = status.get("state")
            worker = reconcile(ledger, dispatch, status)
            from harness.orchestration.workflow.startup import assess_startup

            startup = assess_startup(dispatch, status, repo=repo)
            if (
                startup["classification"] == "runtime-crash"
                and startup["worker_state"] == "stopped"
            ):
                batch = _load_batch(root, dispatch["batch_id"])
                if state in LIVE_DISPATCH_STATES:
                    status.update(
                        {
                            "state": "blocked",
                            "updated_at": utils._now(),
                            "runtime_failure": {
                                "kind": "runtime-crash",
                                "worker_started": True,
                                "operation": "startup",
                                "error": "worker exited before self-report",
                            },
                            "runtime_observation": startup["observation"],
                        }
                    )
                    for item in batch["dispatches"]:
                        if item["dispatch_id"] == dispatch["dispatch_id"]:
                            item["state"] = "blocked"
                    batch["state"] = "blocked"
                    _replace_record(ledger, DispatchStatusRecord.from_dict(status))
                    _replace_record(ledger, BatchRecord.from_dict(batch))
                return {
                    "dispatch_id": dispatch["dispatch_id"],
                    "event": "startup_failed",
                    "startup": startup,
                }
            if state == "reported":
                return {"dispatch_id": dispatch["dispatch_id"], "event": "reported"}
            if startup["classification"] == "worker-exited-without-report":
                # Read-only: the work may be done and its report not yet delivered, so the
                # ledger keeps the dispatch as it is and the coordinator decides.
                return {
                    "dispatch_id": dispatch["dispatch_id"],
                    "event": "worker_exited_without_report",
                    "startup": startup,
                }
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
            exited = _worker_stopped(worker)
            if (
                state in LIVE_DISPATCH_STATES
                and startup["deadline_exceeded"]
                and not exited
            ):
                return {
                    "dispatch_id": dispatch["dispatch_id"],
                    "event": "startup_timeout",
                    "startup": startup,
                }
            if state in LIVE_DISPATCH_STATES and (
                exited or _silent_seconds(status) >= threshold
            ):
                _flag_stale_dispatch(
                    ledger,
                    repo,
                    root,
                    dispatch,
                    _silent_seconds(status),
                    status.get("worker_exit") if exited else None,
                )
                return {
                    "dispatch_id": dispatch["dispatch_id"],
                    "event": "stale",
                    "silent_seconds": _silent_seconds(status),
                    **({"worker": worker} if exited else {}),
                    **(
                        {"staged_report": staged}
                        if exited and (staged := staged_report(dispatch))
                        else {}
                    ),
                }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"dispatch_id": args.dispatch, "event": "timeout"}
        time.sleep(min(interval, max(1, int(remaining))))


def observe_dispatch(args: argparse.Namespace) -> JsonObject:
    """Read process/status/event facts locally, without running or prompting any model."""
    from harness.orchestration.worker_observation import (
        read_events,
        read_terminal,
        redact_visible,
    )
    from harness.orchestration.runtime_permissions import pending_requests
    from harness.orchestration.workflow.startup import assess_startup

    repo = _repo(args)
    offset = getattr(args, "offset", 0)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise CoordinatorError(
            "observation offset must be nonnegative",
            remedy="pass a nonnegative --offset from the previous observation",
        )
    root = _state_root(args, repo)
    with _ledger_lock(LifecycleLedger(root)):
        dispatch = _load_dispatch(root, args.dispatch)
        status = _load_dispatch_status(root, args.dispatch)
    startup = assess_startup(dispatch, status, repo=repo)
    receipt = status.get("adapter_receipt")
    if (
        isinstance(receipt, dict)
        and receipt.get("dispatch_id") != dispatch["dispatch_id"]
    ):
        receipt = None
    return {
        "dispatch_id": dispatch["dispatch_id"],
        "batch_id": dispatch["batch_id"],
        "role": dispatch["role"],
        "ticket": dispatch["ticket"],
        "state": status["state"],
        "worktree": dispatch["worktree"],
        "model": dispatch["resolved_model"],
        "runtime": dispatch["resolved_runtime"],
        "effort": dispatch["resolved_effort"],
        "snapshot_commit": dispatch.get("snapshot_commit"),
        "worktree_attestation": status.get("worktree_attestation"),
        "startup": startup,
        "model_self_report": status.get("model_self_report"),
        "heartbeat_at": status.get("heartbeat_at"),
        "report_staged": Path(dispatch.get("report_staging_path", "")).is_file(),
        "report_recorded": status["state"] == "reported",
        "runtime_exit": read_terminal(repo, receipt),
        "failure": {
            "kind": status["runtime_failure"].get("kind"),
            "error": redact_visible(str(status["runtime_failure"].get("error", ""))),
            "failure_domain": failure_domains.of_runtime_failure(
                status["runtime_failure"]
            ).to_json(),
        }
        if isinstance(status.get("runtime_failure"), dict)
        else None,
        "access_mode": dispatch.get("runtime_access", {}).get("mode", "inherit"),
        "permission_requests": pending_requests(repo, dispatch, status),
        "launch_id": receipt.get("launch_id") if isinstance(receipt, dict) else None,
        "runtime_session_id": receipt.get("runtime_session_id")
        if isinstance(receipt, dict)
        else None,
        **read_events(repo, receipt, offset),
    }


def respond_native_permission(
    repo: Path, dispatch_id: str, request_id: str, expected_sha256: str, *, allow: bool
) -> JsonObject:
    """An explicit console/native operator action; never an automatic policy decision."""
    from harness.orchestration.runtime_permissions import respond

    args = argparse.Namespace(repo=str(repo), state_dir=None)
    root = _state_root(args, repo)
    with _ledger_lock(LifecycleLedger(root)):
        dispatch = _load_dispatch(root, dispatch_id)
        status = _load_dispatch_status(root, dispatch_id)
    return respond(repo, dispatch, status, request_id, expected_sha256, allow=allow)


def dispatch_status(args: argparse.Namespace) -> JsonObject:
    """Liveness view the coordinator session polls; a stale entry is a blocker to surface, never a
    reason for the coordinator to change state on its own.

    For a docker dispatch the view also asks the engine about the receipt's container and records
    the first proof that it stopped. That evidence is the only write; it never changes the
    lifecycle state. Without ``--dispatch`` at most ``MAX_ENGINE_READS`` dispatches reach the
    engine per read; the rest show ``not_observed``."""
    from harness.orchestration.workflow.startup import assess_startup

    repo = _repo(args)
    root = _state_root(args, repo)
    config = core_config._config(repo)
    threshold = (
        args.stale_after
        if args.stale_after is not None
        else _attention_policy(config)["stale_dispatch_seconds"]
    )
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
        raise CoordinatorError(
            "stale-after must be a positive number of seconds",
            remedy="pass --stale-after as a positive number of seconds",
        )
    ledger = LifecycleLedger(root)
    entries: list[JsonObject] | None = None
    lane: JsonObject | None = None
    engine_reads = 0
    # The runtime store is read, never written, by this view: the engine is not asked, so no
    # evidence is persisted (ADR 0020, section 2).
    from_store = store_is_authority(root)
    # The execution of the named supervised dispatch is observed, and an ended one decides its
    # transition, before the view is read (ADR 0021, section 6).
    executions: dict[str, JsonObject | None] = {}
    reaction_error: JsonObject | None = None
    if from_store and args.dispatch:
        from harness.orchestration.workflow import execution_reaction

        try:
            executions[args.dispatch] = execution_reaction.observe(
                repo, root, args.dispatch
            )
        except LedgerBusyError:
            pass  # a busy ledger defers the reaction to the next view
        except CoordinatorError as exc:
            # A report that waited for the exit and is refused now stays pending: the operator
            # sees why (execution_reaction), and the next view tries again.
            reaction_error = {"error": exc.message, "remedy": exc.remedy}
    # A busy ledger answers with a structured retry instead of an error: the caller polls again.
    with contextlib.suppress(LedgerBusyError), read_state(ledger, repo):
        from harness.orchestration import qa_lane

        lane = qa_lane.lane_summary(ledger)
        entries = []
        for path in state_view.glob(
            _records_root(root) / DispatchStatusRecord.directory, "dispatch-*.json"
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
            execution = (
                (
                    executions[dispatch["dispatch_id"]]
                    if dispatch["dispatch_id"] in executions
                    else observed_execution(repo, dispatch["dispatch_id"])
                )
                if from_store and dispatch.get("execution")
                else None
            )
            asks_engine = needs_engine(status)
            observe = not from_store and (
                bool(args.dispatch) or engine_reads < MAX_ENGINE_READS
            )
            worker = reconcile(ledger, dispatch, status, observe=observe)
            engine_reads += asks_engine and observe
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
                    "startup": assess_startup(
                        dispatch, status, repo=repo, execution=execution
                    ),
                    "adapter_receipt": status.get("adapter_receipt"),
                    **({"execution": execution} if dispatch.get("execution") else {}),
                    # A report that waited for an execution that was lost (execution_reaction).
                    **(
                        {"held_report": status["held_report"]}
                        if "held_report" in status
                        else {}
                    ),
                    **({"reaction_error": reaction_error} if reaction_error else {}),
                    "runtime_access": status.get("runtime_access"),
                    "heartbeat_at": status.get("heartbeat_at")
                    or status.get("updated_at"),
                    "silent_seconds": silent,
                    "worker": worker,
                    "stale": live and (silent >= threshold or _worker_stopped(worker)),
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
    if entries is None:
        return {
            "ledger_busy": True,
            "retry_after_seconds": _execution_policy(config)[
                "dispatch_poll_interval_seconds"
            ],
            "lock": None if from_store else ledger.lock_state(),
            "remedy": "repeat dispatch status: another coordinator operation holds the ledger "
            "lock. 'coordinator.py ledger lock-status' shows its holder, age and when a timeout "
            "release becomes available. If it stays held, run 'coordinator.py ledger "
            "release-lock', which refuses a lock whose owner process is alive; for the lock of a "
            f"stopped sandboxed worker, an operator can run '{FORCE_RELEASE_COMMAND}'",
        }
    return {
        "stale_after_seconds": threshold,
        "stale": [entry["dispatch_id"] for entry in entries if entry["stale"]],
        "dispatches": entries,
        # The QA lane: its owner, its fencing token and whether the owner missed its heartbeat
        # (``stale_heartbeat``); ``null`` while the lane is free.
        "qa_lane": lane,
    }


def probe_dispatch_runtime(args: argparse.Namespace) -> JsonObject:
    """Run the read-only app-server READY probe (#552) against one dispatch's immutable brief.

    The probe changes no ledger record and hands no brief to a worker; its result is evidence for
    a launch or recovery decision and never replaces the worker's model self-report.
    """
    timeout = (
        runtime_probe.APP_SERVER_PROBE_TIMEOUT_SECONDS
        if args.timeout is None
        else args.timeout
    )
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
        raise CoordinatorError(
            "runtime probe timeout must be a positive number of seconds",
            remedy="pass --timeout as a positive number of seconds",
        )
    repo = _repo(args)
    dispatch = _load_dispatch(_state_root(args, repo), args.dispatch)
    worktree = Path(args.worktree or dispatch["worktree"]).resolve()
    return {
        "dispatch_id": dispatch["dispatch_id"],
        **runtime_probe.probe_app_server(
            [args.runtime_command, *(args.runtime_arg or [])],
            dispatch,
            cwd=worktree,
            timeout=timeout,
        ),
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
        from harness.orchestration.infrastructure_retry import (
            require_publish_destination,
            stop_attention,
        )

        try:
            require_publish_destination(repo, root, dispatch, remote)
        except CoordinatorError as exc:
            stop_attention(repo, root, args.dispatch, exc, locked=True)
            raise
        candidate = dispatch["candidate_commit"]
        accepted_qa = _accepted_qa_for_candidate(root, batch, candidate)
        # Strict fails closed before the push: the plan is the one the accepted QA brief carried.
        hook_policy.check_strict_boundary(
            repo,
            hook_policy.dispatch_policy(dispatch),
            role="coordinator",
            boundary="publish",
            batch=batch,
            candidate=candidate,
            plan=_load_dispatch(root, accepted_qa["dispatch_id"]).get(
                "verification_plan"
            ),
        )
        # Only a candidate with accepted QA reaches this check, and nothing leaves the machine
        # before the pinned plan of this brief is proven for Git metadata, storage and the remote.
        try:
            access = operation_access.require(
                repo,
                core_config._config(repo),
                "publish",
                brief=dispatch,
                remote=remote,
            )
        except operation_access.OperationAccessError as exc:
            from harness.orchestration.infrastructure_retry import (
                pinned,
                record_operation_failure,
            )

            policy = pinned(dispatch)
            if policy and policy["enabled"]:
                record_operation_failure(
                    repo, root, dispatch, "publish", exc.evidence, remote, locked=True
                )
            raise
        try:
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
                env=git_environment(),
                timeout=PUBLISH_PUSH_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            # The ledger is unchanged, and a repeated push of the same commit is a no-op.
            raise CoordinatorError(
                f"git push to {remote!r} did not finish within "
                f"{PUBLISH_PUSH_TIMEOUT_SECONDS} seconds",
                remedy=f"check connectivity to {remote!r} and repeat dispatch publish; the "
                "dispatch stays approved",
            ) from exc
        except OSError as exc:
            raise CoordinatorError(
                f"git push could not be started: {exc}",
                remedy="make git available on PATH and repeat dispatch publish",
            ) from exc
        if result.returncode != 0:
            detail = _sanitise((result.stderr or result.stdout).strip())
            raise git_failure(
                f"could not publish the accepted candidate: {detail or 'unknown error'}",
                detail,
                remedy="inspect the git push error above and fix it before retrying publish",
            )
        # Bounded and exact: an ls-remote pattern also matches deeper names ending in the branch.
        if _remote_branch_tip(repo, remote, dispatch["branch"]) != candidate:
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
        base = rebase.batch_base(repo, batch)
        changed = (
            _changed_files_between(repo, base, candidate)
            if base
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
            "risks": {"status": "none", "details": ""},
            "blockers": {"status": "none", "details": ""},
            "next_coordinator_action": "accept publication or inspect remote evidence",
            "report_language": "ru",
        }
        report_path = _persist_report(ledger, root, batch, dispatch, report)
    return {
        "dispatch_id": dispatch["dispatch_id"],
        "state": "reported",
        "report": str(report_path),
        "candidate_commit": candidate,
        "access": access,
    }
