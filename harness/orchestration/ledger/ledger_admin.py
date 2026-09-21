"""The `ledger` CLI commands: inspect, migrate, reset and clean the lifecycle state.

These are the only commands that operate on the ledger as a whole rather than on a batch, and the
only ones allowed to discard state -- each behind its own explicit subcommand.
"""

from __future__ import annotations

import argparse

from harness.orchestration.core.utils import CoordinatorError, JsonObject, _repo
from harness.orchestration.ledger.ledger_ops import _ledger_lock, _state_root
from harness.orchestration.ledger.lifecycle import LedgerError, LifecycleLedger


def ledger_status(args: argparse.Namespace) -> JsonObject:
    """Report the selected lifecycle-ledger generation without changing it."""
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        try:
            return ledger.status()
        except LedgerError as exc:
            raise CoordinatorError(exc.message, remedy=exc.remedy) from exc


def migrate_ledger(args: argparse.Namespace) -> JsonObject:
    """Explicitly validate legacy state and atomically select its versioned replacement."""
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        try:
            return ledger.migrate()
        except LedgerError as exc:
            raise CoordinatorError(exc.message, remedy=exc.remedy) from exc


def reset_ledger(args: argparse.Namespace) -> JsonObject:
    """Select an empty generation only after an explicit confirmation and no active batch."""
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        try:
            return ledger.reset(args.confirm)
        except LedgerError as exc:
            raise CoordinatorError(exc.message, remedy=exc.remedy) from exc


def clean_ledger(args: argparse.Namespace) -> JsonObject:
    """Safely remove orphaned dispatch evidence from the ledger state."""
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        try:
            return ledger.clean()
        except LedgerError as exc:
            raise CoordinatorError(exc.message, remedy=exc.remedy) from exc
