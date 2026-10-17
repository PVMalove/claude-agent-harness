"""``coordinator.py recovery status|reload|resume --batch <id>``: the headless face of recovery.

The handlers are thin: the reconciliation, its diagnostics and the durable intent live in
``recovery_api``, which the Harness console calls directly.  No handler reads standard input or
needs a terminal or a UI toolkit; a refusal is printed as the same structured document and exits
2, a success exits 0.  An exception that is not a refusal is reported as an internal invariant
with a correlation ID, never as a configuration problem.
"""

from __future__ import annotations

import argparse
import uuid

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


def recovery_status(args: argparse.Namespace) -> JsonObject:
    """The read-only recovery status of a batch; it takes no lock and writes nothing."""
    repo = _repo(args)
    try:
        document = recovery_api.status(repo, _state_root(args, repo), args.batch)
    except HarnessError:
        raise
    except Exception as exc:  # noqa: BLE001 -- shown as an internal invariant, never a typo
        raise recovery_api.RecoveryInternalError(
            "status", exc, str(uuid.uuid4())
        ) from exc
    return {"action": "status", "outcome": "done", **document}


def recovery_reload(args: argparse.Namespace) -> JsonObject:
    """Re-read and validate the current configuration; never starts a worker."""
    repo = _repo(args)
    return recovery_api.reload(repo, _state_root(args, repo), args.batch)


def recovery_resume(args: argparse.Namespace) -> JsonObject:
    """Reconcile the batch, release its hold and continue a recorded stop through the audited
    resume-stop when an approval is given."""
    approval = _approval(args)
    repo = _repo(args)
    return recovery_api.resume(
        repo, _state_root(args, repo), args.batch, approval=approval
    )
