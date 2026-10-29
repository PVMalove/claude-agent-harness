"""``coordinator.py recovery status|reload|resume|fix|abort --batch <id>``: the headless face of
recovery.

The handlers are thin: the reconciliation, its diagnostics and the durable intent live in
``recovery_api``, and the hold and the intent are recovery records of the runtime store
(ADR 0026).  No handler reads standard input or needs a terminal or a UI toolkit; a refusal is
printed as the same structured document and exits 2, a success exits 0.  A command that cannot
run at all (the file-ledger epoch, a runtime error, a partial approval, an interrupt) exits 2
with ``ERROR`` and ``REMEDY`` on stderr and prints no document.  An exception that is not a
refusal is reported as an internal invariant with a correlation ID, never as a configuration
problem.
"""

from __future__ import annotations

import argparse
import uuid
from collections.abc import Callable

from harness.errors import HarnessError
from harness.orchestration import recovery_api
from harness.orchestration.core.utils import CoordinatorError, JsonObject, _repo
from harness.orchestration.ledger.ledger_ops import _state_root


def _approval(args: argparse.Namespace) -> dict[str, str] | None:
    """The human approval of a stopped batch's resume-stop: all three values, or none."""
    values = {
        key: getattr(args, key, None) for key in ("approved_by", "approved_at", "note")
    }
    given = {key: value for key, value in values.items() if value is not None}
    if not given:
        return None
    if len(given) != len(values):
        raise CoordinatorError(
            "a recovery approval names --approved-by, --approved-at and --note together",
            remedy="pass all three for the audited resume-stop of a stopped batch, or none",
        )
    return {key: str(value) for key, value in given.items()}


def _guarded(action: str, run: Callable[[], JsonObject]) -> JsonObject:
    """Run an action; an exception that is not a refusal is an internal invariant."""
    try:
        return run()
    except HarnessError:
        raise
    except Exception as exc:  # noqa: BLE001 -- shown as an internal invariant, never a typo
        raise recovery_api.RecoveryInternalError(action, exc, str(uuid.uuid4())) from exc


def recovery_status(args: argparse.Namespace) -> JsonObject:
    """The read-only recovery status of a batch; it takes no lock and writes nothing.
    ``--skip`` hides an allowlisted advisory for this call only."""
    repo = _repo(args)
    root = _state_root(args, repo)

    def run() -> JsonObject:
        document = recovery_api.status(repo, root, args.batch)
        skipped = list(getattr(args, "skip", None) or [])
        if skipped:
            session = recovery_api.RecoverySession()
            for diagnostic_id in skipped:
                session = recovery_api.skip(session, document, diagnostic_id)
            document = recovery_api.status(repo, root, args.batch, session=session)
        return {"action": "status", "outcome": "done", **document}

    return _guarded("status", run)


def recovery_reload(args: argparse.Namespace) -> JsonObject:
    """Re-read and validate the current configuration and the runtime; never starts a worker."""
    repo = _repo(args)
    return _guarded(
        "reload", lambda: recovery_api.reload(repo, _state_root(args, repo), args.batch)
    )


def recovery_resume(args: argparse.Namespace) -> JsonObject:
    """Re-check the batch, release its hold and continue a recorded stop through the audited
    resume-stop when an approval is given."""
    approval = _approval(args)
    repo = _repo(args)
    return _guarded(
        "resume",
        lambda: recovery_api.resume(
            repo, _state_root(args, repo), args.batch, approval=approval
        ),
    )


def recovery_fix(args: argparse.Namespace) -> JsonObject:
    """Preview, or with ``--apply`` write, the safe normalization of the configuration."""
    repo = _repo(args)
    return _guarded(
        "fix",
        lambda: recovery_api.fix(
            repo, _state_root(args, repo), args.batch, apply=bool(args.apply)
        ),
    )


def recovery_abort(args: argparse.Namespace) -> JsonObject:
    """Record that the operator left recovery; the batch stays exactly as it is."""
    repo = _repo(args)
    return _guarded(
        "abort", lambda: recovery_api.abort(repo, _state_root(args, repo), args.batch)
    )
