"""Coordinator-facing access to the lifecycle ledger.

Two jobs, and nothing else: resolve where a request's state lives, and load or write a typed
record.  Every call translates `LedgerError` into `CoordinatorError` here, so no caller above this
layer can leak a ledger error into the CLI boundary, which only catches the coordinator's own.
"""

from __future__ import annotations

import argparse
import json
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from harness.orchestration.core import state_view
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
    LedgerSandboxDenied,
    LedgerRecordVO,
    LifecycleLedger,
    RiskAssessmentRecord,
)


class LedgerBusyError(CoordinatorError):
    """Another coordinator operation holds the ledger lock."""


class SandboxDeniedError(CoordinatorError):
    """The environment forbids writing the ledger; classify as infrastructure, not the task."""


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
    except LedgerSandboxDenied as exc:
        raise SandboxDeniedError(exc.message, remedy=exc.remedy) from exc
    except LedgerError as exc:
        raise CoordinatorError(exc.message, remedy=exc.remedy) from exc


# The version of ``ledger.json`` that says the runtime store is the authority (ADR 0020).
STORE_LEDGER_VERSION = 4
# How long a read waits for the runtime to answer before it is `runtime_unavailable`.
READ_TIMEOUT_SECONDS = 30.0


def store_is_authority(root: Path) -> bool:
    """Whether ``ledger.json`` selects the runtime store epoch.

    The pointer is the epoch selector of ADR 0020, section 1, not operational state: this is the
    one read of the file ledger that a store-backed read makes."""
    try:
        pointer = json.loads((root / "ledger.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(pointer, dict) and pointer.get("version") == STORE_LEDGER_VERSION


def _runtime_fetch(repo: Path) -> state_view.Fetch:
    """``state.read`` through ``runtime_client``; a busy or moving store reads as a busy ledger."""
    from harness.orchestration.runtime_client import RuntimeClient, RuntimeClientError

    client = RuntimeClient(repo)

    def fetch(
        collections: Sequence[str], paths: Sequence[str], revision: int | None
    ) -> tuple[dict[str, JsonObject], int | None]:
        try:
            reply = client.call(
                "state.read",
                operation_id=f"state.read-{uuid.uuid4().hex}",
                payload={"collections": list(collections), "paths": list(paths)},
                expected_revision=revision,
                timeout_seconds=READ_TIMEOUT_SECONDS,
            )
        except RuntimeClientError as exc:
            if exc.code in {"store_busy", "revision_conflict"}:
                raise LedgerBusyError(exc.message, remedy=exc.remedy) from exc
            if exc.facts.get("reason") == "answer_too_large":
                raise state_view.AnswerTooLarge(exc.message) from exc
            raise
        records = reply.result.get("records")
        return (records if isinstance(records, dict) else {}), reply.revision

    return fetch


@contextmanager
def read_state(ledger: LifecycleLedger, repo: Path) -> Iterator[None]:
    """Read operational state for one command (ADR 0020, sections 1 and 3).

    In the file-ledger epoch this is ``_ledger_lock``. In the runtime-store epoch there is no
    lock: the commands that read bind a view that asks the runtime (``state.read``), so they never
    open the file ledger or ``runtime.db``, and the records they read come from one store revision."""
    if not store_is_authority(ledger.root):
        with _ledger_lock(ledger):
            yield
        return
    if ledger.root != (repo / STATE_REL).resolve():
        raise CoordinatorError(
            "the runtime store serves only the state directory of its own repository",
            remedy="repeat the command without --state-dir",
        )
    with state_view.bind(state_view.StateView(ledger.root, _runtime_fetch(repo))):
        yield


def _load_batch(root: Path, batch_id: str) -> JsonObject:
    with _ledger_errors():
        return BatchRecord.from_dict(
            _read_object(
                _records_root(root)
                / BatchRecord.directory
                / f"{_safe_id(batch_id, 'batch')}.json",
                "batch record",
            )
        ).to_dict()


def _load_dispatch(root: Path, dispatch_id: str) -> JsonObject:
    with _ledger_errors():
        return DispatchRecord.from_dict(
            _read_object(
                _records_root(root)
                / DispatchRecord.directory
                / f"{_safe_id(dispatch_id, 'dispatch')}.json",
                "dispatch record",
            )
        ).to_dict()


def _load_dispatch_status(root: Path, dispatch_id: str) -> JsonObject:
    with _ledger_errors():
        return DispatchStatusRecord.from_dict(
            _read_object(
                _records_root(root)
                / DispatchStatusRecord.directory
                / f"{_safe_id(dispatch_id, 'dispatch')}.json",
                "dispatch status",
            )
        ).to_dict()


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
