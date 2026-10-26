"""Coordinator-facing access to the lifecycle ledger.

Two jobs, and nothing else: resolve where a request's state lives, and load or write a typed
record.  Every call translates `LedgerError` into `CoordinatorError` here, so no caller above this
layer can leak a ledger error into the CLI boundary, which only catches the coordinator's own.
"""

from __future__ import annotations

import argparse
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING

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
from harness.orchestration.ledger.lifecycle import (
    STORE_LEDGER_VERSION as STORE_LEDGER_VERSION,
)
from harness.orchestration.ledger.lifecycle import (
    store_is_authority as store_is_authority,
)


if TYPE_CHECKING:
    from harness.orchestration.runtime_client import RuntimeClient, RuntimeReply


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
    every other refusal.

    In the runtime-store epoch (ADR 0020) there is no lock: the block is the store unit of one
    command. The primitives of the ledger stage their writes on it, the command reads its own
    writes from the view, and a clean exit sends them to the runtime as one ``state.write`` for
    each batch (a block that raises sends nothing). A nested block joins the unit of the outer one."""
    cut_over = store_is_authority(ledger.root)
    try:
        if not cut_over:
            with ledger.lock():
                if not store_is_authority(ledger.root):
                    yield
                    return
            # The migration finished between the epoch check and the lock: the file lock means
            # nothing any more, so the block goes on as a command of the store.
        with _store_unit(ledger):
            yield
    except LedgerLockBusy as exc:
        raise LedgerBusyError(exc.message, remedy=exc.remedy) from exc
    except LedgerSandboxDenied as exc:
        raise SandboxDeniedError(exc.message, remedy=exc.remedy) from exc
    except LedgerError as exc:
        raise CoordinatorError(exc.message, remedy=exc.remedy) from exc


# How long a read waits for the runtime to answer before it is `runtime_unavailable`.
READ_TIMEOUT_SECONDS = 30.0
# The same for a write; the runtime itself gives up on a locked store after 5 seconds.
WRITE_TIMEOUT_SECONDS = 30.0


class _LazyClient:
    """The runtime client of a repository, made at the first request: a command that never
    reaches the runtime (a refusal of its arguments, a ``ledger`` command of the file epoch) does
    not need the address of the runtime to be resolvable."""

    def __init__(self, repo: Path) -> None:
        self._repo = repo
        self._client: RuntimeClient | None = None

    def call(
        self,
        operation: str,
        *,
        operation_id: str,
        payload: JsonObject | None = None,
        expected_revision: int | None = None,
        batch_id: str | None = None,
        timeout_seconds: float,
    ) -> RuntimeReply:
        if self._client is None:
            from harness.orchestration.runtime_client import RuntimeClient

            self._client = RuntimeClient(self._repo)
        return self._client.call(
            operation,
            operation_id=operation_id,
            payload=payload,
            expected_revision=expected_revision,
            batch_id=batch_id,
            timeout_seconds=timeout_seconds,
        )


def _runtime_commit(repo: Path) -> state_view.Commit:
    """``state.write`` through ``runtime_client``.

    The ``operation_id`` follows ADR 0021, section 4: ``sha256(batch_id, expected_revision,
    operation, payload_digest)``. A repeat of the same request, such as one whose answer was
    lost, gets the same key and the stored result. A command that runs again after it was killed
    builds its payload from the clock again, so it gets another key, and the revision check
    answers ``revision_conflict`` instead of a stored result."""
    from harness.orchestration.operational_guards import _digest
    from harness.orchestration.runtime_client import RuntimeClientError

    client = _LazyClient(repo)

    def commit(
        batch_id: str | None, expected_revision: int | None, mutations: list[JsonObject]
    ) -> dict[str, int]:
        payload: JsonObject = {"mutations": mutations}
        try:
            reply = client.call(
                "state.write",
                operation_id=_digest(
                    [batch_id, expected_revision, "state.write", _digest(payload)]
                ),
                payload=payload,
                expected_revision=expected_revision,
                batch_id=batch_id,
                timeout_seconds=WRITE_TIMEOUT_SECONDS,
            )
        except RuntimeClientError as exc:
            if exc.code == "store_busy":
                raise LedgerBusyError(exc.message, remedy=exc.remedy) from exc
            raise
        revisions = reply.result.get("revisions")
        return {
            path: revision
            for path, revision in (revisions if isinstance(revisions, dict) else {}).items()
            if isinstance(revision, int)
        }

    return commit


@contextmanager
def _store_unit(ledger: LifecycleLedger) -> Iterator[None]:
    """The store unit of one command: reads from the view, writes staged until a clean exit."""
    view = state_view.current_view()
    if view is not None and view.root != ledger.root:
        view = None
    if view is not None and view.unit is not None:
        yield
        return
    repo = ledger.repository()
    owned = view is None
    if view is None:
        view = state_view.StateView(ledger.root, _runtime_fetch(repo))
    else:
        # The view of the whole command (``command_state``) answered the reads before this
        # block. Each block starts on a fresh snapshot, as a ``read_state`` does.
        view.refresh()
    unit = view.begin_unit(_runtime_commit(repo))
    with state_view.bind(view) if owned else nullcontext():
        try:
            yield
        except BaseException:
            unit.abort()
            raise
        unit.flush()


def persist_writes(ledger: LifecycleLedger) -> None:
    """Keep what the running ``_ledger_lock`` block wrote when the block raises next.

    In the file epoch the writes are on disk already and this does nothing. In the store epoch a
    block that raises sends nothing, so a refusal that must leave its evidence behind (the
    attempt record of a QA access refusal, the attention flag of a refused dispatch) calls this
    right before it raises."""
    unit = state_view.current_unit()
    if unit is not None and unit.view.root == ledger.root:
        unit.persist()


def _runtime_fetch(repo: Path) -> state_view.Fetch:
    """``state.read`` through ``runtime_client``; a busy or moving store reads as a busy ledger."""
    from harness.orchestration.runtime_client import RuntimeClientError

    client = _LazyClient(repo)

    def fetch(
        collections: Sequence[str], paths: Sequence[str], revision: int | None
    ) -> tuple[dict[str, JsonObject], int | None, dict[str, int]]:
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
        revisions = reply.result.get("revisions")
        return (
            (records if isinstance(records, dict) else {}),
            reply.revision,
            {
                path: revision
                for path, revision in (
                    revisions if isinstance(revisions, dict) else {}
                ).items()
                if isinstance(revision, int)
            },
        )

    return fetch


@contextmanager
def command_state(args: argparse.Namespace) -> Iterator[None]:
    """The view of the runtime store for one command, when the store is the authority.

    A command checks its arguments against the records before it takes ``_ledger_lock``. In the
    store epoch there is no file to read for that, so the command runs on one view from its first
    read, and the blocks of the command join it: it sees one snapshot of the store and no read
    falls back to the file ledger. Without a store epoch this changes nothing."""
    repo = Path(getattr(args, "repo", ".")).resolve()
    root = _state_root(args, repo)
    if (
        state_view.current_view() is not None
        or not store_is_authority(root)
        or root != (repo / STATE_REL).resolve()
    ):
        yield
        return
    with state_view.bind(state_view.StateView(root, _runtime_fetch(repo))):
        yield


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
    view = state_view.current_view()
    if view is not None and view.root == ledger.root:
        if view.unit is None:
            # A poll that reads again and again sees the store as it is at each read.
            view.refresh()
        yield
        return
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
