"""Coordinator-facing access to the lifecycle ledger.

Two jobs, and nothing else: resolve where a request's state lives, and load or write a typed
record.  Every call translates `LedgerError` into `CoordinatorError` here, so no caller above this
layer can leak a ledger error into the CLI boundary, which only catches the coordinator's own.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from harness.orchestration.core.constants import STATE_REL
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _read_object,
    _safe_id,
)
from harness.orchestration.ledger.lifecycle import (
    BatchRecord,
    CheckpointRecord,
    ContextPackageRecord,
    DispatchRecord,
    DispatchStatusRecord,
    LedgerError,
    LedgerLockBusy,
    LedgerRecordVO,
    LifecycleLedger,
    RiskAssessmentRecord,
)


class LedgerBusyError(CoordinatorError):
    """Another coordinator operation holds the ledger lock."""


@contextmanager
def _ledger_errors() -> Iterator[None]:
    """Translate a ``LedgerError`` raised inside the block into ``CoordinatorError``."""
    try:
        yield
    except LedgerError as exc:
        raise CoordinatorError(exc.message, remedy=exc.remedy) from exc


def _write_exclusive(ledger: LifecycleLedger, path: Path, value: JsonObject) -> None:
    with _ledger_errors():
        ledger.write_immutable(path, value)


def _write_text_exclusive(ledger: LifecycleLedger, path: Path, value: str) -> None:
    with _ledger_errors():
        ledger.write_artifact(path, value)


def _write_record(ledger: LifecycleLedger, record: LedgerRecordVO) -> None:
    with _ledger_errors():
        ledger.write_record(record)


def _replace_record(
    ledger: LifecycleLedger,
    record: LedgerRecordVO,
    *,
    decision: JsonObject | None = None,
) -> None:
    with _ledger_errors():
        ledger.replace_record(record, decision=decision)


def _state_root(args: argparse.Namespace, repo: Path) -> Path:
    supplied = getattr(args, "state_dir", None)
    return (Path(supplied).resolve() if supplied else repo / STATE_REL).resolve()


def _records_root(root: Path) -> Path:
    with _ledger_errors():
        return LifecycleLedger(root).records_root()


@contextmanager
def _ledger_lock(ledger: LifecycleLedger) -> Iterator[None]:
    """Exclusive lock through ``LifecycleLedger.lock()``, translating ``LedgerError`` to
    ``CoordinatorError`` for this call site -- the same translation ``_write_exclusive`` and
    ``_replace_record`` already apply on every write.  Centralising the translation here (rather than
    repeating a ``try/except`` at every one of this module's lock sites) removes the risk of a lock
    site forgetting it and leaking an uncaught ``LedgerError`` into the CLI.  Contention becomes
    the ``LedgerBusyError`` marker, so a call site that may wait out a busy ledger can tell it from
    every other refusal."""
    try:
        with ledger.lock():
            yield
    except LedgerLockBusy as exc:
        raise LedgerBusyError(exc.message, remedy=exc.remedy) from exc
    except LedgerError as exc:
        raise CoordinatorError(exc.message, remedy=exc.remedy) from exc


def _load_batch(root: Path, batch_id: str) -> JsonObject:
    return _read_object(
        _records_root(root)
        / BatchRecord.directory
        / f"{_safe_id(batch_id, 'batch')}.json",
        "batch record",
    )


def _load_dispatch(root: Path, dispatch_id: str) -> JsonObject:
    return _read_object(
        _records_root(root)
        / DispatchRecord.directory
        / f"{_safe_id(dispatch_id, 'dispatch')}.json",
        "dispatch record",
    )


def _load_dispatch_status(root: Path, dispatch_id: str) -> JsonObject:
    return _read_object(
        _records_root(root)
        / DispatchStatusRecord.directory
        / f"{_safe_id(dispatch_id, 'dispatch')}.json",
        "dispatch status",
    )


def _load_risk(root: Path, risk_id: str) -> JsonObject:
    return _read_object(
        _records_root(root)
        / RiskAssessmentRecord.directory
        / f"{_safe_id(risk_id, 'risk assessment')}.json",
        "risk assessment",
    )


def _load_checkpoint(root: Path, checkpoint_id: str) -> JsonObject:
    return _read_object(
        _records_root(root)
        / CheckpointRecord.directory
        / f"{_safe_id(checkpoint_id, 'checkpoint')}.json",
        "checkpoint",
    )


def _load_context_package(root: Path, package_id: str) -> JsonObject:
    return _read_object(
        _records_root(root)
        / ContextPackageRecord.directory
        / f"{_safe_id(package_id, 'context package')}.json",
        "context package",
    )
