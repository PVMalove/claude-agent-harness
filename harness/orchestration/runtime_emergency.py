"""Diagnostics of a command that stopped before it could reach its batch's state.

An invalid current config, or a busy or unreadable ledger, stops a command before it can read or
reconcile its batch.  ``report_failure`` prints one ``DIAGNOSTICS`` line for such a failure and, when
the command named a batch or dispatch, writes the independent emergency record
(``runtimeEmergency.v1``) beside the runtime state records.  The record is evidence only: nothing
decides on it, it carries no approval and no ledger state, and it is written after the command
released the ledger lock, under its own bounded lock.  It never takes or breaks the coordinator
lock, never writes the ledger or a batch's runtime state, and never loads the current config.

A runtime state failure prints its own diagnostics and writes no emergency record: the record
lives in that same store.  A bootstrap command (``batch create``, ``batch preflight``) names no
batch or dispatch, so it prints diagnostics and writes nothing.  Any other failure is an ordinary
refusal of the command and prints only its error and remedy.
"""

from __future__ import annotations

import argparse
import json
import sys

from harness.errors import HarnessError
from harness.orchestration.core.config import ConfigInvalidError
from harness.orchestration.core.utils import JsonObject, _repo
from harness.orchestration.ledger.ledger_ops import (
    LedgerBusyError,
    LedgerUnreadableError,
    _state_root,
)
from harness.orchestration.runtime_state_store import (
    RuntimeStateError,
    RuntimeStateStore,
    sanitize_detail,
)

# The failures that stop a command before its batch state is reachable, by type.
_CLASSES: tuple[tuple[type[HarnessError], str, str], ...] = (
    (ConfigInvalidError, "config-invalid", "config"),
    (LedgerBusyError, "concurrency-conflict", "ledger-lock"),
    (LedgerUnreadableError, "integrity-failure", "ledger-read"),
)


def report_failure(args: argparse.Namespace, exc: HarnessError) -> None:
    """Print the ``DIAGNOSTICS`` line of a classified failure and record it; never raises, so
    the original error is always the one reported.  Diagnostics that cannot be built print an
    ``internal-error`` line instead, so the failure stays visible."""
    diagnostics: JsonObject | None
    try:
        diagnostics = _diagnostics(args, exc)
    except Exception as error:  # noqa: BLE001 -- the original failure is the one to report
        diagnostics = {
            "code": "internal-error",
            "step": "diagnostics",
            "detail": sanitize_detail(str(error)) or type(error).__name__,
            "emergency_record": None,
        }
    if diagnostics is not None:
        print(
            "DIAGNOSTICS: "
            + json.dumps(diagnostics, ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )


def _diagnostics(args: argparse.Namespace, exc: HarnessError) -> JsonObject | None:
    group = str(getattr(args, "command", ""))
    action = getattr(args, f"{group.replace('-', '_')}_command", None)
    command = f"{group} {action}" if action else group
    subject: JsonObject = {
        "batch_id": _named(args, "batch"),
        "dispatch_id": _named(args, "dispatch"),
    }
    seen: set[int] = set()
    error: BaseException | None = exc
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, RuntimeStateError):
            return {
                **error.diagnostics,
                "durable_revision": error.durable_revision,
                "command": command,
                "subject": subject,
                "emergency_record": None,
            }
        for kind, code, step in _CLASSES:
            if isinstance(error, kind):
                detail = sanitize_detail(error.message)
                failure: JsonObject = {
                    "code": code,
                    "step": step,
                    "message": detail or code,
                }
                named = subject["batch_id"] or subject["dispatch_id"]
                return {
                    "code": code,
                    "step": step,
                    "detail": detail,
                    "command": command,
                    "subject": subject,
                    "emergency_record": _record(args, command, subject, failure)
                    if named
                    else None,
                }
        error = error.__cause__
    return None


def _named(args: argparse.Namespace, name: str) -> str | None:
    """The batch or dispatch ID the command was given, as given; the ledger is not consulted."""
    value = getattr(args, name, None)
    if not isinstance(value, str):
        return None
    return sanitize_detail(value) or None


def _record(
    args: argparse.Namespace, command: str, subject: JsonObject, failure: JsonObject
) -> JsonObject:
    try:
        repo = _repo(args)
        store = RuntimeStateStore(repo, _state_root(args, repo))
        record = store.write_emergency(command, subject, failure)
    except RuntimeStateError as error:
        return {"written": False, "code": error.code}
    except Exception:  # noqa: BLE001 -- an emergency record never adds a failure of its own
        return {"written": False, "code": "internal-error"}
    return {
        "written": True,
        "path": store.location.relative(store.emergency_path),
        "sequence": record["sequence"],
    }
