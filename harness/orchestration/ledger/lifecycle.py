"""Versioned, append-audited persistence for the orchestration lifecycle.

The coordinator is deliberately only a CLI adapter.  This module selects the active record
generation, records immutable writes and transitions, and performs the explicit migration from
the pre-ledger directory layout or from an older-schema generation.  A pointer is the only mutable
selector: a replacement generation is validated and moved into place before that pointer is
atomically switched.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import shutil
import socket
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, ClassVar, Protocol, cast

from ...errors import INTERNAL_INVARIANT_REMEDY, HarnessError
from ..core.constants import BatchState, DispatchState
from ..core.system_fields import enum_refusal
from ..core.utils import sealed
from .field_migration import write_problems
from .recovery_chain import event_audit, terminal

LEDGER_VERSION = 3
SUPPORTED_LEDGER_VERSIONS = (1, 2, 3)
POINTER_NAME = "ledger.json"
GENERATIONS = "generations"
LOCK_DIRECTORY = ".coordinator.lock"
LOCK_OWNER = "owner.json"
# The operating-system file lock that lets one release at a time judge and remove the ledger lock.
LOCK_RELEASE_GUARD = ".coordinator.lock.release"
RECORD_DIRECTORIES = (
    "batches",
    "plans",
    "dispatches",
    "dispatch-status",
    "risk-assessments",
    "context-packages",
    "checkpoints",
    "reports",
    "qa-lane",
    "qa-artifacts",
    "audit",
)
# The system-fields migration's manifest and quarantine beside the record directories: no record
# directory, so no ordinary loader reads it and an older generation never lacks it.
MIGRATION_DIRECTORY = "migration"
# The routes of persisted history no migration vouches for: inspection shows it, export copies a
# batch byte for byte, clean removes dispatch evidence no batch owns, and the system-fields
# migration maps a known historical value or quarantines the batch that holds it.
LEDGER_INSPECT = "'coordinator.py ledger inspect'"
LEDGER_EXPORT = "'coordinator.py ledger export'"
LEDGER_CLEAN = "'coordinator.py ledger clean'"
LEDGER_MIGRATE = "'coordinator.py ledger migrate'"
LEDGER_MIGRATE_ROUTE = f"run {LEDGER_MIGRATE}"
# The record directories whose unreadable or unknown value the migration quarantines; the
# generation migration moves exactly these records of a quarantined batch.
MIGRATED_DIRECTORIES = ("batches", "plans", "dispatches", "dispatch-status")
# The action and the generation purpose of the system-fields migration.
SYSTEM_FIELDS_MIGRATION = "system-fields-migration"
# The `ledger.audit_action` values: what one audited write did.  Reconciliation and the
# idempotency of a deterministic audit event compare it exactly.
AUDIT_ACTIONS = (
    "immutable-record",
    "immutable-artifact",
    "transition",
    "deletion",
    "generation-created",
    "migration",
    "schema-upgrade",
    "reset",
    SYSTEM_FIELDS_MIGRATION,
)
# The `ledger.generation_purpose` values: why a `generation-created` event made a generation.
GENERATION_PURPOSES = (
    "initialize",
    "migration",
    "schema-upgrade",
    "reset",
    SYSTEM_FIELDS_MIGRATION,
)

# Ledger records are persisted JSON.  Keep the dynamic boundary at ``_read`` explicit while
# preserving arbitrary, forward-compatible JSON in each record's ``extra`` fields.
type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
type JsonObject = dict[str, JsonValue]


class LedgerError(HarnessError):
    """The durable lifecycle state cannot safely be selected or changed."""


class LedgerLockBusy(LedgerError):
    """Another operation holds the ledger lock."""


class LedgerWriteConflict(LedgerError):
    """A record changed or appeared between the check of a write and the write itself."""


class LedgerUnmigrated(LedgerError):
    """The selected generation holds history no current migration vouches for: only
    ``ledger inspect``, ``ledger export`` and ``ledger migrate`` read it."""


def _parse_state[T: StrEnum](value: object, vocabulary: type[T]) -> T:
    if isinstance(value, str):
        try:
            return vocabulary(value)
        except ValueError:
            pass
    raise LedgerError(
        f"invalid {vocabulary.__name__} value {value!r}",
        remedy=f"use an exact English state from: {', '.join(vocabulary)}; restore the coordinator's recorded state before continuing",
    )


def _validate_record_state(directory: str, value: JsonObject) -> None:
    if directory == "batches":
        _parse_state(value.get("state"), BatchState)
        entries = value.get("dispatches", [])
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and "state" in entry:
                _parse_state(entry["state"], DispatchState)
    elif directory in {"dispatches", "dispatch-status"}:
        _parse_state(value.get("state"), DispatchState)


def _validate_record_write(directory: str, value: JsonObject) -> None:
    """Refuse a record about to be written to ``directory`` whose machine fields are not exactly
    canonical, before anything is written; its explanations stay in its own text fields."""
    _validate_record_state(directory, value)
    if problems := write_problems(directory, value):
        problem, remedy = problems[0]
        raise LedgerError(
            f"refusing a non-canonical machine value: {problem}", remedy=remedy
        )


def _audit_problem(action: object, details: object) -> tuple[str, str] | None:
    """The refusal and remedy of an audit event outside its closed vocabularies, else ``None``:
    its action, a new generation's purpose and the batch states of a batch transition.  A write
    refuses such an event before it is written, and a read refuses one found on disk."""
    if not isinstance(details, dict):
        return (
            "ledger audit details must be a JSON object",
            "write the audit details as one JSON object",
        )
    checks: list[tuple[str, object, tuple[str, ...]]] = [
        ("ledger audit action", action, AUDIT_ACTIONS)
    ]
    if action == "generation-created":
        checks.append(
            ("generation-created purpose", details.get("purpose"), GENERATION_PURPOSES)
        )
    path = details.get("path")
    if action == "transition" and isinstance(path, str) and path.startswith("batches/"):
        states = tuple(state.value for state in BatchState)
        checks += [
            ("batch transition from", details.get("from"), states),
            ("batch transition to", details.get("to"), states),
        ]
    for subject, value, allowed in checks:
        if (refusal := enum_refusal(subject, value, allowed)) is not None:
            return refusal
    return None


def _path_segment(value: object) -> bool:
    """Whether ``value`` names one entry of a directory: a non-empty string without a path
    separator that is neither ``.`` nor ``..``."""
    return (
        isinstance(value, str)
        and bool(value)
        and "/" not in value
        and "\\" not in value
        and value not in {".", ".."}
    )


def _lock_busy() -> LedgerLockBusy:
    return LedgerLockBusy(
        "ledger is locked by another operation",
        remedy="repeat the command once the other operation finishes; if the lock stays held, "
        "run 'coordinator.py ledger release-lock', which releases it only when its owner process "
        "is gone",
    )


def _guard_byte(handle: BinaryIO, *, acquire: bool) -> None:
    """Take without waiting, or drop, the operating-system lock on a file's first byte."""
    handle.seek(0)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(
            handle.fileno(), msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1
        )
    else:
        import fcntl

        fcntl.flock(
            handle.fileno(),
            (fcntl.LOCK_EX | fcntl.LOCK_NB) if acquire else fcntl.LOCK_UN,
        )


@dataclass(frozen=True)
class BatchRecord:
    """Value Object for a ``batches/*.json`` record."""

    directory: ClassVar[str] = "batches"

    batch_id: str
    state: BatchState
    dispatches: list[JsonValue]
    coordinator_approval: JsonObject | None
    extra: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", _parse_state(self.state, BatchState))
        _validate_record_state(self.directory, self.to_dict())

    @property
    def record_id(self) -> str:
        return self.batch_id

    def to_dict(self) -> JsonObject:
        return {
            **self.extra,
            "batch_id": self.batch_id,
            "state": self.state,
            "dispatches": self.dispatches,
            "coordinator_approval": self.coordinator_approval,
        }

    @classmethod
    def from_dict(cls, data: JsonObject) -> BatchRecord:
        known = ("batch_id", "state", "dispatches", "coordinator_approval")
        extra = {key: value for key, value in data.items() if key not in known}
        return cls(
            batch_id=cast(str, data.get("batch_id")),
            state=_parse_state(data.get("state"), BatchState),
            dispatches=cast(list[JsonValue], data.get("dispatches")),
            coordinator_approval=cast(
                JsonObject | None, data.get("coordinator_approval")
            ),
            extra=extra,
        )


@dataclass(frozen=True)
class PlanRecord:
    """Value Object for a ``plans/*.json`` record."""

    directory: ClassVar[str] = "plans"

    batch_id: str
    extra: JsonObject = field(default_factory=dict)

    @property
    def record_id(self) -> str:
        return self.batch_id

    def to_dict(self) -> JsonObject:
        return {**self.extra, "batch_id": self.batch_id}

    @classmethod
    def from_dict(cls, data: JsonObject) -> PlanRecord:
        known = ("batch_id",)
        extra = {key: value for key, value in data.items() if key not in known}
        return cls(batch_id=cast(str, data.get("batch_id")), extra=extra)


@dataclass(frozen=True)
class DispatchRecord:
    """Value Object for a ``dispatches/*.json`` record."""

    directory: ClassVar[str] = "dispatches"

    dispatch_id: str
    batch_id: str
    state: DispatchState
    coordinator_approval: JsonObject | None
    extra: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", _parse_state(self.state, DispatchState))

    @property
    def record_id(self) -> str:
        return self.dispatch_id

    def to_dict(self) -> JsonObject:
        return {
            **self.extra,
            "dispatch_id": self.dispatch_id,
            "batch_id": self.batch_id,
            "state": self.state,
            "coordinator_approval": self.coordinator_approval,
        }

    @classmethod
    def from_dict(cls, data: JsonObject) -> DispatchRecord:
        known = ("dispatch_id", "batch_id", "state", "coordinator_approval")
        extra = {key: value for key, value in data.items() if key not in known}
        return cls(
            dispatch_id=cast(str, data.get("dispatch_id")),
            batch_id=cast(str, data.get("batch_id")),
            state=_parse_state(data.get("state"), DispatchState),
            coordinator_approval=cast(
                JsonObject | None, data.get("coordinator_approval")
            ),
            extra=extra,
        )


@dataclass(frozen=True)
class DispatchStatusRecord:
    """Value Object for a ``dispatch-status/*.json`` record."""

    directory: ClassVar[str] = "dispatch-status"

    dispatch_id: str
    state: DispatchState
    extra: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", _parse_state(self.state, DispatchState))

    @property
    def record_id(self) -> str:
        return self.dispatch_id

    def to_dict(self) -> JsonObject:
        return {**self.extra, "dispatch_id": self.dispatch_id, "state": self.state}

    @classmethod
    def from_dict(cls, data: JsonObject) -> DispatchStatusRecord:
        known = ("dispatch_id", "state")
        extra = {key: value for key, value in data.items() if key not in known}
        return cls(
            dispatch_id=cast(str, data.get("dispatch_id")),
            state=_parse_state(data.get("state"), DispatchState),
            extra=extra,
        )


@dataclass(frozen=True)
class RiskAssessmentRecord:
    """Value Object for a ``risk-assessments/*.json`` record."""

    directory: ClassVar[str] = "risk-assessments"

    risk_assessment_id: str
    extra: JsonObject = field(default_factory=dict)

    @property
    def record_id(self) -> str:
        return self.risk_assessment_id

    def to_dict(self) -> JsonObject:
        return {**self.extra, "risk_assessment_id": self.risk_assessment_id}

    @classmethod
    def from_dict(cls, data: JsonObject) -> RiskAssessmentRecord:
        known = ("risk_assessment_id",)
        extra = {key: value for key, value in data.items() if key not in known}
        return cls(
            risk_assessment_id=cast(str, data.get("risk_assessment_id")), extra=extra
        )


@dataclass(frozen=True)
class ContextPackageRecord:
    """Value Object for a ``context-packages/*.json`` record."""

    directory: ClassVar[str] = "context-packages"

    context_package_id: str
    extra: JsonObject = field(default_factory=dict)

    @property
    def record_id(self) -> str:
        return self.context_package_id

    def to_dict(self) -> JsonObject:
        return {**self.extra, "context_package_id": self.context_package_id}

    @classmethod
    def from_dict(cls, data: JsonObject) -> ContextPackageRecord:
        known = ("context_package_id",)
        extra = {key: value for key, value in data.items() if key not in known}
        return cls(
            context_package_id=cast(str, data.get("context_package_id")), extra=extra
        )


@dataclass(frozen=True)
class CheckpointRecord:
    """Value Object for a ``checkpoints/*.json`` record."""

    directory: ClassVar[str] = "checkpoints"

    checkpoint_id: str
    extra: JsonObject = field(default_factory=dict)

    @property
    def record_id(self) -> str:
        return self.checkpoint_id

    def to_dict(self) -> JsonObject:
        return {**self.extra, "checkpoint_id": self.checkpoint_id}

    @classmethod
    def from_dict(cls, data: JsonObject) -> CheckpointRecord:
        known = ("checkpoint_id",)
        extra = {key: value for key, value in data.items() if key not in known}
        return cls(checkpoint_id=cast(str, data.get("checkpoint_id")), extra=extra)


def _derived_id(prefix: str, members: JsonObject) -> str:
    """A deterministic record id: the prefix and the first 32 hex digits of the canonical digest."""
    digest = hashlib.sha256(_canonical(members).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:32]}"


@dataclass(frozen=True)
class IntegrationRecord:
    """Value Object for a ``reports/integration/*.json`` record.

    The record lives in a subdirectory of the already validated ``reports`` directory, so adding it
    needs neither a schema bump nor a ``ledger migrate``.  Its id is derived from the identity it
    links, so repeating a prepare finds the same record instead of writing a second one.
    """

    directory: ClassVar[str] = "reports/integration"
    IDENTITY_MEMBERS: ClassVar[tuple[str, ...]] = (
        "ticket",
        "branch",
        "source_batch_id",
        "candidate_sha",
        "target_sha",
    )

    integration_record_id: str
    extra: JsonObject = field(default_factory=dict)

    @classmethod
    def derive_id(cls, identity: JsonObject) -> str:
        return _derived_id(
            "integration", {key: identity.get(key) for key in cls.IDENTITY_MEMBERS}
        )

    @property
    def record_id(self) -> str:
        return self.integration_record_id

    def to_dict(self) -> JsonObject:
        return {**self.extra, "integration_record_id": self.integration_record_id}

    @classmethod
    def from_dict(cls, data: JsonObject) -> IntegrationRecord:
        known = ("integration_record_id",)
        extra = {key: value for key, value in data.items() if key not in known}
        return cls(
            integration_record_id=cast(str, data.get("integration_record_id")),
            extra=extra,
        )


@dataclass(frozen=True)
class IntegrationEvidenceRecord:
    """Value Object for a ``reports/integration-evidence/*.json`` record: one new check of a
    candidate/target pair linked to an integration record.  The id excludes the recording time, so
    linking the same evidence twice is idempotent."""

    directory: ClassVar[str] = "reports/integration-evidence"
    ID_MEMBERS: ClassVar[tuple[str, ...]] = (
        "integration_record_id",
        "kind",
        "candidate_sha",
        "target_sha",
        "result",
        "reference",
        "artifact_sha256",
    )

    evidence_id: str
    extra: JsonObject = field(default_factory=dict)

    @classmethod
    def derive_id(cls, members: JsonObject) -> str:
        return _derived_id(
            "evidence", {key: members.get(key) for key in cls.ID_MEMBERS}
        )

    @property
    def record_id(self) -> str:
        return self.evidence_id

    def to_dict(self) -> JsonObject:
        return {**self.extra, "evidence_id": self.evidence_id}

    @classmethod
    def from_dict(cls, data: JsonObject) -> IntegrationEvidenceRecord:
        known = ("evidence_id",)
        extra = {key: value for key, value in data.items() if key not in known}
        return cls(evidence_id=cast(str, data.get("evidence_id")), extra=extra)


@dataclass(frozen=True)
class IntegrationLocalQaRecord:
    """Append-only request, attempt or result in the existing reports contract."""

    directory: ClassVar[str] = "reports/integration-local-qa"
    local_qa_id: str
    extra: JsonObject = field(default_factory=dict)

    @classmethod
    def derive_id(cls, members: JsonObject) -> str:
        return _derived_id("local-qa", members)

    @property
    def record_id(self) -> str:
        return self.local_qa_id

    def to_dict(self) -> JsonObject:
        return {**self.extra, "local_qa_id": self.local_qa_id}

    @classmethod
    def from_dict(cls, data: JsonObject) -> IntegrationLocalQaRecord:
        return cls(
            local_qa_id=cast(str, data.get("local_qa_id")),
            extra={key: value for key, value in data.items() if key != "local_qa_id"},
        )


@dataclass(frozen=True)
class IntegrationRefreshRecord:
    """Value Object for a ``reports/integration-refresh/*.json`` record (issue #533): one clean
    rebase of an issue branch onto a new integration SHA, linking the candidate before and after,
    the target SHA and the resulting history.  The id excludes the recording time, so repeating a
    refresh that already happened finds the same record."""

    directory: ClassVar[str] = "reports/integration-refresh"
    ID_MEMBERS: ClassVar[tuple[str, ...]] = (
        "integration_record_id",
        "previous_candidate_sha",
        "previous_target_sha",
        "new_candidate_sha",
        "target_sha",
    )

    refresh_id: str
    extra: JsonObject = field(default_factory=dict)

    @classmethod
    def derive_id(cls, members: JsonObject) -> str:
        return _derived_id("refresh", {key: members.get(key) for key in cls.ID_MEMBERS})

    @property
    def record_id(self) -> str:
        return self.refresh_id

    def to_dict(self) -> JsonObject:
        return {**self.extra, "refresh_id": self.refresh_id}

    @classmethod
    def from_dict(cls, data: JsonObject) -> IntegrationRefreshRecord:
        known = ("refresh_id",)
        extra = {key: value for key, value in data.items() if key not in known}
        return cls(refresh_id=cast(str, data.get("refresh_id")), extra=extra)


@dataclass(frozen=True)
class ResolverRecord:
    """Value Object for a ``reports/resolver/*.json`` record (issue #534): the conflict-resolver
    route taken for one integration record.  Immutable; the cycle budget is never stored on it but
    derived from the append-only ``ResolverEventRecord`` files, so a lost session cannot reset it."""

    directory: ClassVar[str] = "reports/resolver"

    resolver_id: str
    extra: JsonObject = field(default_factory=dict)

    @classmethod
    def derive_id(cls, integration_record_id: str) -> str:
        return _derived_id("resolver", {"integration_record_id": integration_record_id})

    @property
    def record_id(self) -> str:
        return self.resolver_id

    def to_dict(self) -> JsonObject:
        return {**self.extra, "resolver_id": self.resolver_id}

    @classmethod
    def from_dict(cls, data: JsonObject) -> ResolverRecord:
        extra = {key: value for key, value in data.items() if key != "resolver_id"}
        return cls(resolver_id=cast(str, data.get("resolver_id")), extra=extra)


@dataclass(frozen=True)
class ResolverEventRecord:
    """Value Object for a ``reports/resolver-events/*.json`` record (issue #534): one immutable
    audit event of the conflict-resolver route (``cycle-spent``, ``same-target-fix``,
    ``human-decision``, ``scope-change``, ``exhausted``).  The id covers what makes an event the
    same event, so repeating it finds the record instead of writing a second one."""

    directory: ClassVar[str] = "reports/resolver-events"
    ID_MEMBERS: ClassVar[tuple[str, ...]] = (
        "kind",
        "integration_record_id",
        "target_sha",
        "dispatch_id",
        "discriminator",
    )

    event_id: str
    extra: JsonObject = field(default_factory=dict)

    @classmethod
    def derive_id(cls, members: JsonObject) -> str:
        return _derived_id(
            "resolver-event", {key: members.get(key) for key in cls.ID_MEMBERS}
        )

    @property
    def record_id(self) -> str:
        return self.event_id

    def to_dict(self) -> JsonObject:
        return {**self.extra, "event_id": self.event_id}

    @classmethod
    def from_dict(cls, data: JsonObject) -> ResolverEventRecord:
        extra = {key: value for key, value in data.items() if key != "event_id"}
        return cls(event_id=cast(str, data.get("event_id")), extra=extra)


class LedgerRecordVO(Protocol):
    """Structural shape a Value Object must have to be persisted via ``write_record``/
    ``replace_record`` -- satisfied by ``BatchRecord``, ``DispatchRecord``, and the other frozen
    record dataclasses above without inheriting from this class."""

    directory: ClassVar[str]

    @property
    def record_id(self) -> str: ...

    def to_dict(self) -> JsonObject: ...


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical(value: JsonObject) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _read(path: Path, label: str) -> JsonObject:
    """A persisted record of ``path``.  A record the system-fields migration takes over -- an
    unreadable file or an unknown state in a batch, plan, dispatch or status -- is
    ``LedgerUnmigrated``, whose message and remedy name ``ledger migrate``."""
    migrated = path.parent.name in MIGRATED_DIRECTORIES
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        if migrated and isinstance(exc, ValueError):
            raise _unmigrated(path, f"{label} is not valid JSON: {path.name}") from exc
        raise LedgerError(
            f"{label} is not valid JSON: {path.name}",
            remedy=f"fix the JSON syntax in {path}",
        ) from exc
    if not isinstance(value, dict):
        if migrated:
            raise _unmigrated(path, f"{label} must be a JSON object: {path.name}")
        raise LedgerError(
            f"{label} must be a JSON object: {path.name}",
            remedy=f"rewrite {path} as a JSON object",
        )
    # json.loads is a dynamic boundary; after confirming the top-level object, JSON itself
    # guarantees string object keys and recursive JSON-compatible values.
    record = cast(JsonObject, value)
    try:
        _validate_record_state(path.parent.name, record)
    except LedgerError as exc:
        relative = f"{path.parent.name}/{path.name}"
        raise _unmigrated(path, f"{label} {relative} holds {exc.message}") from exc
    return record


def _unmigrated(path: Path, problem: str) -> LedgerUnmigrated:
    """The refusal of persisted history at ``path`` that only the migration takes over."""
    return LedgerUnmigrated(
        f"{LEDGER_MIGRATE_ROUTE}: {problem}",
        remedy=f"run {LEDGER_INSPECT} to see {path.parent.name}/{path.name}, "
        f"then {LEDGER_MIGRATE}, which maps a known historical value or "
        "quarantines its batch; the ledger rewrites nothing",
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verified_audit(path: Path) -> JsonObject:
    """An audit event whose ``record_sha256`` matches its content, or a ``LedgerError``."""
    record = _read(path, "ledger audit record")
    body = {key: value for key, value in record.items() if key != "record_sha256"}
    if (
        record.get("record_sha256")
        != hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
    ):
        raise LedgerError(
            f"ledger audit record failed immutable integrity check: {path.name}",
            remedy=f"{path} was modified after its record_sha256 was recorded -- {INTERNAL_INVARIANT_REMEDY}",
        )
    return record


def _transition_chain(
    name: str,
    transitions: list[JsonObject],
    current: str,
    *,
    subject: str = "batch",
    repair: str | None = None,
) -> list[JsonObject]:
    """The recorded transitions of the batch record ``name`` in the order their hash chain links
    them: each one starts from the version of the record (``before_sha256``) the one before it
    wrote (``sha256``).  Audit times decide nothing: two transitions may share one, and the clock
    may step back between them.  A version the record returns to -- a write that changed
    nothing, or bytes it held before -- is walked again, so the chain is the one walk over every
    transition; a chain that ends where it started ends at the ``current`` digest of the record.
    Transitions that fork from one version, leave a gap between two versions or cannot be walked
    as one chain, and a chain that ends where it started without the ``current`` version, are a
    ``LedgerError`` that names the versions.  The walk takes the transitions in the given order,
    so a fixed order makes it deterministic.  ``subject`` names the kind of record in a refusal
    and ``repair`` replaces the route a batch record gets, for a record the batch names."""
    following: dict[str, list[JsonObject]] = {}
    # Per version: the transitions that leave it minus the transitions that reach it.  One
    # chain leaves its first version once more than it reaches it, reaches its last version
    # once more than it leaves it and balances every other one; a chain that ends in the version
    # it started from balances every version.
    leaves_minus_reaches: dict[str, int] = {}
    for details in transitions:
        before, after = details.get("before_sha256"), details.get("sha256")
        if not isinstance(before, str) or not isinstance(after, str):
            raise _not_one_chain(
                name,
                "a transition names no record version it starts from or writes",
                subject,
                repair,
            )
        following.setdefault(before, []).append(details)
        leaves_minus_reaches[before] = leaves_minus_reaches.get(before, 0) + 1
        leaves_minus_reaches[after] = leaves_minus_reaches.get(after, 0) - 1
    starts = sorted(v for v, count in leaves_minus_reaches.items() if count > 0)
    if len(starts) > 1 or any(
        abs(count) > 1 for count in leaves_minus_reaches.values()
    ):
        ends = sorted(v for v, count in leaves_minus_reaches.items() if count < 0)
        raise _not_one_chain(
            name,
            f"they leave the versions {_shown_versions(starts)} more often than they reach "
            f"them and reach {_shown_versions(ends)} more often than they leave them, while "
            "one chain has a single first and a single last version; the record holds "
            f"{_shown_versions([current])}",
            subject,
            repair,
        )
    if not starts and current not in leaves_minus_reaches:
        raise _changed_after_last_transition(
            name,
            f"{subject} record changed after its last recorded transition: {name} holds the "
            f"version {_shown_versions([current])}, which none of its {len(transitions)} "
            "recorded transitions wrote, and they end in the version they started from",
            repair,
        )
    if starts:
        [last] = (v for v, count in leaves_minus_reaches.items() if count < 0)
        if last != current:
            raise _changed_after_last_transition(
                name,
                f"{subject} record changed after its last recorded transition: {name} holds "
                f"the version {_shown_versions([current])}, but that transition wrote "
                f"{_shown_versions([last])}",
                repair,
            )
    for steps in following.values():
        steps.reverse()  # pop() takes them in the given order
    # Hierholzer's walk: follow unused transitions until a version has none left, then settle
    # the transition that led there, so every return to a version is walked in its place.
    first = starts[0] if starts else current
    chain: list[JsonObject] = []
    walk: list[tuple[str, JsonObject | None]] = [(first, None)]
    while walk:
        version, step = walk[-1]
        if following.get(version):
            taken = following[version].pop()
            walk.append((cast(str, taken["sha256"]), taken))
        else:
            walk.pop()
            if step is not None:
                chain.append(step)
    if len(chain) != len(transitions):
        apart = sorted(version for version, steps in following.items() if steps)
        raise _not_one_chain(
            name,
            f"the transitions that start from {_shown_versions(apart)} are not reached from "
            f"{'its first version' if starts else 'the version the record holds'} "
            f"{_shown_versions([first])}",
            subject,
            repair,
        )
    chain.reverse()
    return chain


def _shown_versions(versions: list[str]) -> str:
    """Record versions as a refusal names them: the first 12 characters of each digest."""
    return ", ".join(version[:12] for version in versions)


def _not_one_chain(
    name: str, detail: str, subject: str = "batch", repair: str | None = None
) -> LedgerError:
    """The refusal of the ``subject`` record ``name`` whose recorded transitions do not link into
    one sequence of record versions, each starting from the version the one before it wrote:
    ``detail`` names the versions where the sequence breaks."""
    return LedgerError(
        f"{subject} transitions are not one recorded chain: {name}: {detail}",
        remedy=f"a version of the {subject} record has no recorded transition, or two "
        "transitions start from one version: the record was changed outside the ledger, or a "
        "harness without durable write intents stopped between a write and its audit event, "
        f"and a later transition started from the changed version; {repair or _no_repair(name)}",
    )


def _changed_after_last_transition(
    name: str, problem: str, repair: str | None = None
) -> LedgerError:
    """The refusal ``problem`` of the record ``name`` that changed after its last recorded
    transition: only reconciliation of an interrupted coordinator operation can append the
    transition that wrote it."""
    return LedgerError(
        problem,
        remedy="if the runtime state holds an unfinished coordinator operation on this batch, "
        "repeat the interrupted command so reconciliation appends the transition it planned; "
        "without one nothing can append it -- the record was changed outside the ledger, or a "
        "harness without durable write intents stopped between its write and its audit "
        f"event; {repair or _no_repair(name)}",
    )


def _check_creation(
    root: Path,
    relative: str,
    writes: dict[str, list[tuple[str, JsonObject]]],
    subject: str,
    name: str,
    repair: str,
) -> None:
    """The ``subject`` record ``relative`` that no transition moved holds the version the audit
    event of its creation recorded; a record without such an event, or one that is missing,
    passes.  A refusal names both versions."""
    path = root / relative
    if not path.is_file():
        return
    for action, details in writes.get(relative, []):
        if action == "immutable-record" and details.get("sha256") != _digest(path):
            raise _changed_after_last_transition(
                name,
                f"{subject} record changed after its creation was recorded: {name} holds the "
                f"version {_shown_versions([_digest(path)])}, but its creation recorded "
                f"{_shown_versions([str(details.get('sha256'))])}",
                repair,
            )


def _no_repair(name: str) -> str:
    """What the operator of a batch record that no ledger command repairs can do: keep the
    evidence the read-only routes show and escalate, since this release restores nothing."""
    return (
        f"run {LEDGER_INSPECT} and {LEDGER_EXPORT}, each with --batch "
        f"{name.removesuffix('.json')}, keep their output as evidence and escalate the batch "
        "to a human: no command of this release restores a batch record or re-links its "
        "transitions, and the audit trail keeps the digests and states of its versions, not "
        "their contents; recovering such a batch belongs to the harness recovery tooling "
        "(issue #684). Do not edit the record by hand: the next transition would record the "
        "edit. The ledger rewrites nothing"
    )


def audit_details(
    mode: str,
    relative: str,
    sha256: str,
    *,
    before_sha256: str | None = None,
    before: JsonObject | None = None,
    after: JsonObject | None = None,
) -> tuple[str, JsonObject]:
    """The audit action and details of one write: the single source both the ledger writes and
    a planner that records them in advance use, so the two can never diverge.

    ``mode`` is ``create`` (an immutable JSON record), ``create-artifact`` (an immutable text
    artifact) or ``replace`` (a record transition, whose batch states ``before`` and ``after``
    name)."""
    if mode == "create":
        return "immutable-record", {"path": relative, "sha256": sha256}
    if mode == "create-artifact":
        return "immutable-artifact", {"path": relative, "sha256": sha256}
    if mode != "replace":
        raise LedgerError(
            f"unknown ledger write mode {mode!r}", remedy=INTERNAL_INVARIANT_REMEDY
        )
    details: JsonObject = {
        "path": relative,
        "before_sha256": before_sha256,
        "sha256": sha256,
    }
    if relative.startswith("batches/"):
        details.update(
            {
                "from": (before or {}).get("state"),
                "to": (after or {}).get("state"),
            }
        )
    return "transition", details


class LifecycleLedger:
    """Own the selected versioned record generation and its append-only audit."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Non-blocking exclusive lock on ``self.root``, raising ``LedgerLockBusy`` on contention
        so this module never imports an exception type from coordinator.py.

        The lock is held by whoever first creates its owner record, ``owner.json`` (pid, host,
        acquired_at), exclusively inside the lock directory; creating the directory alone does
        not hold it.  The record lets ``ledger release-lock`` judge a lock left behind by a dead
        process.  Release removes the record and then the directory."""
        lock_dir = self._take_lock_directory()
        owner = self._record_lock_owner(lock_dir)
        try:
            yield
        finally:
            try:
                owner.unlink(missing_ok=True)
                lock_dir.rmdir()
            except OSError:
                pass

    def _take_lock_directory(self) -> Path:
        """The first step of ``lock()``: create the lock directory, or be busy."""
        self.root.mkdir(parents=True, exist_ok=True)
        lock_dir = self.root / LOCK_DIRECTORY
        try:
            lock_dir.mkdir()
        except FileExistsError as exc:
            raise _lock_busy() from exc
        return lock_dir

    def _record_lock_owner(self, lock_dir: Path) -> Path:
        """The second step of ``lock()``: hold the lock by creating its owner record exclusively.

        A directory that has no owner record yet can be released as stale and taken again before
        its creator records itself, so taking the directory alone does not hold the lock: whoever
        creates the record first does.  A later creator meets that record, or no directory at all,
        and is busy; it never writes into, or removes, a lock it does not hold."""
        owner = lock_dir / LOCK_OWNER
        created = False
        try:
            with owner.open("x", encoding="utf-8") as stream:
                created = True
                stream.write(
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "host": socket.gethostname(),
                            "acquired_at": _now(),
                        }
                    )
                )
        except (FileExistsError, FileNotFoundError) as exc:
            raise _lock_busy() from exc
        except OSError as exc:
            with contextlib.suppress(OSError):
                if created:
                    owner.unlink()
                lock_dir.rmdir()
            raise LedgerError(
                "ledger lock owner could not be recorded",
                remedy=f"make {self.root} writable, then repeat the command",
            ) from exc
        return owner

    def lock_state(self) -> JsonObject | None:
        """The current lock as its owner recorded it, or ``None`` when nothing holds it.

        ``owner_record`` says what the owner record is: ``readable``; ``unreadable``, when its
        owner died before writing the record it created, so ``owner`` is ``None`` and the age is
        measured from the record's mtime; or ``absent``, for a lock taken by an older runtime,
        aged from the lock directory.  A readable record without a recorded ``acquired_at`` is
        aged from the lock directory as well."""
        lock_dir = self.root / LOCK_DIRECTORY
        try:
            age_origin = lock_dir.stat().st_mtime
        except FileNotFoundError:
            return None
        record = lock_dir / LOCK_OWNER
        owner = self.read_record_lenient(record)
        owner_record = "readable"
        if owner is None:
            try:
                age_origin = record.stat().st_mtime
            except FileNotFoundError:
                owner_record = "absent"
            else:
                owner_record = "unreadable"
        recorded = owner.get("acquired_at") if owner is not None else None
        try:
            since = (
                datetime.fromisoformat(recorded) if isinstance(recorded, str) else None
            )
        except ValueError:
            since = None
        if since is None or since.tzinfo is None:
            since = datetime.fromtimestamp(age_origin, UTC)
        held = (datetime.now(UTC) - since).total_seconds()
        return {
            "path": str(lock_dir),
            "owner": owner,
            "owner_record": owner_record,
            "acquired_at": since.isoformat(),
            "held_seconds": max(0, int(held)),
        }

    @contextmanager
    def _release_guard(self) -> Iterator[None]:
        """Let one ``break_lock`` at a time judge and remove the lock.

        An operating-system file lock, not a lock directory: the system drops it when its holder
        exits, so a release that dies never leaves a guard behind for anyone to remove."""
        with (self.root / LOCK_RELEASE_GUARD).open("a+b") as handle:
            try:
                _guard_byte(handle, acquire=True)
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise LedgerError(
                        f"ledger release guard could not be taken: {exc.strerror}",
                        remedy=f"make {self.root} support file locks, then run "
                        "'coordinator.py ledger release-lock' again",
                    ) from exc
                raise LedgerError(
                    "another ledger release-lock is releasing the ledger lock",
                    remedy="run 'coordinator.py ledger release-lock' again once it finishes",
                ) from exc
            try:
                yield
            finally:
                _guard_byte(handle, acquire=False)

    def break_lock(self, observed: JsonObject) -> None:
        """Remove the lock ``lock_state()`` returned as ``observed``, and only that lock.

        The caller decides whether the lock may go; this only guarantees it removes the lock it
        judged.  Under the release guard the lock is read again and must still be the observed
        one, and nothing else can change it before it goes: a holder only creates an owner record
        where none exists and removes only its own, and the observed owner was judged gone.  An
        unreadable record goes only when it was observed unreadable; a lock observed without a
        record goes by rmdir, which never removes a record created since."""
        lock_dir = self.root / LOCK_DIRECTORY
        owner = lock_dir / LOCK_OWNER
        changed = LedgerError(
            "ledger lock changed while it was being released",
            remedy="run 'coordinator.py ledger release-lock' again",
        )
        with self._release_guard():
            current = self.lock_state()
            if current is None or any(
                current[key] != observed.get(key)
                for key in ("owner", "owner_record", "acquired_at")
            ):
                raise changed
            try:
                if current["owner_record"] != "absent":
                    owner.unlink()
                lock_dir.rmdir()
            except OSError as exc:
                raise changed from exc

    @property
    def pointer_path(self) -> Path:
        return self.root / POINTER_NAME

    def pointer(self) -> JsonObject | None:
        if not self.pointer_path.exists():
            return None
        pointer = _read(self.pointer_path, "ledger pointer")
        if set(pointer) != {"version", "generation", "selected_at"}:
            raise LedgerError(
                "ledger pointer has an invalid schema",
                remedy=f"fix or remove the corrupted pointer at {self.pointer_path} (it must have exactly version, generation and selected_at)",
            )
        version = pointer["version"]
        generation = pointer["generation"]
        selected_at = pointer["selected_at"]
        if (
            not isinstance(version, int)
            or version not in SUPPORTED_LEDGER_VERSIONS
            or not isinstance(generation, str)
        ):
            raise LedgerError(
                "ledger pointer has an unsupported version or generation",
                remedy=f"fix {self.pointer_path}: version must be one of {SUPPORTED_LEDGER_VERSIONS} and generation a string",
            )
        # The generation is joined under `generations/`: a separator would select a directory
        # outside the state root.
        if (
            not generation.startswith("generation-")
            or "/" in generation
            or "\\" in generation
            or not isinstance(selected_at, str)
        ):
            raise LedgerError(
                "ledger pointer has an invalid generation",
                remedy=f"fix {self.pointer_path}: generation must be one directory name starting with 'generation-' and selected_at must be a string",
            )
        return pointer

    @staticmethod
    def _pointer_generation(pointer: JsonObject) -> str:
        """Narrow a generation selected by ``pointer()``, which already validates this field."""
        generation = pointer["generation"]
        if not isinstance(generation, str):
            raise LedgerError(
                "ledger pointer generation is not a string after validation",
                remedy=INTERNAL_INVARIANT_REMEDY,
            )
        return generation

    def records_root(self) -> Path:
        """Return the selected generation; legacy state is readable only by ``migrate``."""
        pointer = self.pointer()
        if pointer is None:
            if self._legacy_records_present():
                raise LedgerError(
                    "legacy lifecycle state requires an explicit ledger migrate",
                    remedy="run 'coordinator.py ledger migrate' once",
                )
            return self.root
        if pointer["version"] != LEDGER_VERSION:
            raise LedgerError(
                "ledger generation requires an explicit ledger migrate to the current schema version",
                remedy=f"run 'coordinator.py ledger migrate' to move generation {self._pointer_generation(pointer)!r} to version {LEDGER_VERSION}",
            )
        generation = self.root / GENERATIONS / self._pointer_generation(pointer)
        # A coordinator operation may create an immutable dispatch and its status record in two
        # writes under one state lock.  Validate the container and audit here; validate the full
        # cross-record graph before a migration/selects a generation.
        self._validate_generation(generation, complete=False)
        return generation

    def records_root_lenient(self) -> Path | None:
        """Return the selected generation without raising, for a caller (delivery_stats.py) that
        must degrade a missing or malformed record to an absence signal instead of aborting.

        Unlike ``records_root()``, this deliberately never calls ``pointer()`` (which raises on a
        schema/version defect) and never checks the pointer's ``version`` field, and it never runs
        ``_validate_generation()`` (no per-record JSON parsing, no batch/plan or record-graph cross
        check). It is a read of whatever is on disk right now, not a validated selection."""
        if not self.pointer_path.exists():
            return self.root if self.root.is_dir() else None
        try:
            pointer = json.loads(self.pointer_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        generation = pointer.get("generation") if isinstance(pointer, dict) else None
        if not isinstance(generation, str):
            return None
        candidate = self.root / GENERATIONS / generation
        return candidate if candidate.is_dir() else None

    @staticmethod
    def read_record_lenient(path: Path) -> JsonObject | None:
        """Read one record file, returning ``None`` instead of raising for any of: a missing
        file, unparseable JSON, or JSON that does not parse to an object."""
        try:
            return _read(path, "ledger record")
        except LedgerError:
            return None

    def ensure(self) -> JsonObject:
        """Create the first empty generation; never reinterpret legacy records implicitly."""
        pointer = self.pointer()
        if pointer is not None:
            if pointer["version"] != LEDGER_VERSION:
                raise LedgerError(
                    "ledger generation requires an explicit ledger migrate to the current schema version",
                    remedy=f"run 'coordinator.py ledger migrate' to move generation {self._pointer_generation(pointer)!r} to version {LEDGER_VERSION}",
                )
            return pointer
        if self._legacy_records_present():
            raise LedgerError(
                "legacy lifecycle state requires an explicit ledger migrate",
                remedy="run 'coordinator.py ledger migrate' once",
            )
        generation = self._new_generation("initialize")
        return self._select(generation)

    def status(self) -> JsonObject:
        pointer = self.pointer()
        if pointer is None:
            return {
                "version": 0,
                "generation": None,
                "legacy": self._legacy_records_present(),
            }
        if pointer["version"] != LEDGER_VERSION:
            return {
                "version": pointer["version"],
                "generation": pointer["generation"],
                "legacy": False,
                "stale_schema": True,
            }
        root = self.records_root()
        return {
            "version": pointer["version"],
            "generation": pointer["generation"],
            "selected_at": pointer["selected_at"],
            "audit_records": len(list((root / "audit").glob("*.json"))),
        }

    def validate(self) -> JsonObject:
        """Validate the selected generation without ensure, migration or pointer writes."""
        pointer = self.pointer()
        root = (
            self.root
            if pointer is None
            else self.root / GENERATIONS / self._pointer_generation(pointer)
        )
        version = pointer["version"] if pointer else 0
        if pointer and version == LEDGER_VERSION:
            self._validate_generation(root)
        elif pointer or self._legacy_records_present():
            self._validate_legacy(root)
            self._validate_audit(root / "audit")
        return {
            "valid": True,
            "version": version,
            "generation": pointer["generation"] if pointer else None,
            "batches": [p.stem for p in sorted((root / "batches").glob("*.json"))],
        }

    def upgrade_source_batches(self) -> list[JsonObject]:
        """The batches of the older-schema generation ``migrate`` would upgrade; empty when no
        schema upgrade is pending."""
        pointer = self.pointer()
        if pointer is None or pointer["version"] == LEDGER_VERSION:
            return []
        batches = (
            self.root / GENERATIONS / self._pointer_generation(pointer) / "batches"
        )
        return [_read(path, "batch record") for path in sorted(batches.glob("*.json"))]

    def migrate(self) -> JsonObject:
        """Explicitly select a current-schema generation, from legacy (pre-ledger) state or from an
        older-schema generation already selected by an earlier harness version.  Either source is
        validated tolerantly (its own, possibly incomplete, set of record directories); the new
        generation it is copied into is always validated against the complete current schema before
        its pointer is switched."""
        pointer = self.pointer()
        if pointer is not None and pointer["version"] == LEDGER_VERSION:
            return {
                "version": pointer["version"],
                "generation": pointer["generation"],
                "migrated": False,
            }
        if pointer is None and not self._legacy_records_present():
            return {"version": 0, "generation": None, "migrated": False}
        if pointer is None:
            source_root = self.root
            purpose = "migration"
            audit_details: JsonObject = {}
        else:
            source_root = self.root / GENERATIONS / self._pointer_generation(pointer)
            purpose = "schema-upgrade"
            audit_details = {
                "previous_generation": self._pointer_generation(pointer),
                "previous_version": pointer["version"],
            }
        self._validate_legacy(source_root)
        generation = self._new_generation(purpose)
        for directory in RECORD_DIRECTORIES:
            source = source_root / directory
            if source.exists():
                shutil.copytree(source, generation / directory, dirs_exist_ok=True)
        # A schema upgrade keeps the system-fields migration's evidence with the records it covers.
        if pointer is not None and (source_root / MIGRATION_DIRECTORY).is_dir():
            shutil.copytree(
                source_root / MIGRATION_DIRECTORY, generation / MIGRATION_DIRECTORY
            )
        self._validate_generation(generation)
        imported: JsonObject = {
            path.relative_to(source_root).as_posix(): _digest(path)
            for directory in RECORD_DIRECTORIES
            for path in sorted((source_root / directory).rglob("*"))
            if path.is_file()
        }
        self._append_audit(
            generation, purpose, {**audit_details, "legacy_records": imported}
        )
        self._validate_generation(generation)
        pointer = self._select(generation)
        return {
            "version": pointer["version"],
            "generation": pointer["generation"],
            "migrated": True,
        }

    def reset(self, confirmation: str) -> JsonObject:
        if confirmation != "RESET":
            raise LedgerError(
                "ledger reset requires --confirm RESET",
                remedy="pass --confirm RESET (the literal string) to acknowledge the reset",
            )
        current = self.records_root()
        active = []
        for path in (current / "batches").glob("*.json"):
            batch = _read(path, "batch record")
            if batch.get("state") in {
                "active",
                "awaiting-approval",
                "paused",
                "blocked",
                "failed",
            } and not terminal(batch):
                active.append(batch.get("batch_id", path.stem))
        if active:
            raise LedgerError(
                "ledger reset is refused while batches are active: "
                + ", ".join(map(str, active)),
                remedy="decide (complete or abandon) the listed active batches before resetting the ledger",
            )
        previous = self.pointer()
        generation = self._new_generation("reset")
        self._append_audit(
            generation,
            "reset",
            {
                "previous_generation": previous["generation"] if previous else None,
                "confirmation": "RESET",
            },
        )
        pointer = self._select(generation)
        return {
            "version": pointer["version"],
            "generation": pointer["generation"],
            "reset": True,
        }

    def clean(self) -> JsonObject:
        """Remove orphaned dispatch evidence from the current generation or legacy state."""
        pointer = self.pointer()
        if pointer is not None:
            if pointer["version"] != LEDGER_VERSION:
                raise LedgerError(
                    "ledger generation requires an explicit ledger migrate before cleaning",
                    remedy=f"run 'coordinator.py ledger migrate' to move generation {self._pointer_generation(pointer)!r} to version {LEDGER_VERSION}",
                )
            root = self.root / GENERATIONS / self._pointer_generation(pointer)
        else:
            root = self.root
            if not self._legacy_records_present():
                return {"version": 0, "generation": None, "cleaned": 0}

        # A batch that cannot be read names no evidence, yet it may own some: deleting then would
        # destroy referenced evidence irreversibly, so an unreadable batch stops the clean.
        referenced: set[str] = set()
        for batch_path in (root / "batches").glob("*.json"):
            batch = _read(batch_path, "batch record")
            entries = batch.get("dispatches", [])
            if not isinstance(entries, list):
                raise LedgerError(
                    f"batch dispatches are invalid: {batch_path.name}",
                    remedy=f"fix {batch_path.name} so its dispatches field is a list, "
                    "then run 'ledger clean' again",
                )
            for entry in entries:
                if isinstance(entry, dict) and isinstance(
                    dispatch_id := entry.get("dispatch_id"), str
                ):
                    referenced.add(dispatch_id)
        # A quarantined batch keeps its reports and checkpoints in place: they are its evidence.
        quarantine = root / MIGRATION_DIRECTORY / "quarantine"
        referenced.update(
            path.stem
            for directory in ("dispatches", "dispatch-status")
            for path in quarantine.glob(f"*/{directory}/*.json")
        )

        removed = 0
        for directory in ("dispatches", "dispatch-status", "reports", "checkpoints"):
            dir_path = root / directory
            if dir_path.exists():
                for path in dir_path.glob("*.json"):
                    if not self._dispatch_evidence_referenced(path, referenced):
                        path.unlink()
                        removed += 1

        return {
            "version": pointer["version"] if pointer else 0,
            "generation": pointer["generation"] if pointer else None,
            "cleaned": removed,
        }

    @classmethod
    def _dispatch_evidence_referenced(cls, path: Path, referenced: set[str]) -> bool:
        """Whether a dispatch evidence file belongs to a dispatch some batch references.

        Dispatch, status and report files are named by their dispatch; a checkpoint file is named
        by its own ``checkpoint-*`` id and names its dispatch inside the record."""
        if path.parent.name != "checkpoints":
            return path.stem in referenced
        record = cls.read_record_lenient(path)
        owner = record.get("dispatch_id") if record is not None else None
        return isinstance(owner, str) and owner in referenced

    def record_path(self, record: LedgerRecordVO) -> Path:
        """Where a Value-Object-backed record lives in the selected generation."""
        self._check_record_id(record.record_id)
        return self.records_root() / record.directory / f"{record.record_id}.json"

    @staticmethod
    def _check_record_id(record_id: object) -> None:
        if not _path_segment(record_id):
            raise LedgerError(
                "record id is not a valid path segment",
                remedy="use a non-empty record id with no path separators and not '.' or '..'",
            )

    def write_record(
        self, record: LedgerRecordVO, *, audit_id: str | None = None
    ) -> None:
        """Persist a new Value-Object-backed record, deriving its path from the record itself."""
        self.write_immutable(
            self.record_path(record), record.to_dict(), audit_id=audit_id
        )

    def replace_record(
        self,
        record: LedgerRecordVO,
        *,
        decision: JsonObject | None = None,
        audit_id: str | None = None,
        expected_before_sha256: str | None = None,
    ) -> None:
        """Persist a Value-Object-backed record transition, deriving its path from the record."""
        self.replace(
            self.record_path(record),
            record.to_dict(),
            decision=decision,
            audit_id=audit_id,
            expected_before_sha256=expected_before_sha256,
        )

    def write_immutable(
        self,
        path: Path,
        value: JsonObject,
        *,
        artifact: bool = False,
        audit_id: str | None = None,
    ) -> None:
        """Create an immutable record; ``audit_id`` makes its audit event deterministic."""
        if not artifact:
            _validate_record_write(path.parent.name, value)
        generation, relative = self._selected_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(_canonical(value))
        except FileExistsError as exc:
            raise LedgerWriteConflict(
                f"refusing to overwrite immutable record: {path.name}",
                remedy=f"use a different record id, or 'ledger clean'/inspect {path} if it is orphaned evidence",
            ) from exc
        action, details = audit_details(
            "create-artifact" if artifact else "create", relative, _digest(path)
        )
        self._append_audit(generation, action, details, audit_id=audit_id)

    def write_artifact(
        self, path: Path, value: str, *, audit_id: str | None = None
    ) -> None:
        generation, relative = self._selected_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(value)
        except FileExistsError as exc:
            if path.read_text(encoding="utf-8") != value:
                raise LedgerWriteConflict(
                    f"refusing to overwrite immutable artifact: {path.name}",
                    remedy=f"write a new artifact under a different name instead of overwriting {path}",
                ) from exc
        action, details = audit_details("create-artifact", relative, _digest(path))
        self._append_audit(generation, action, details, audit_id=audit_id)

    def replace(
        self,
        path: Path,
        value: JsonObject,
        *,
        decision: JsonObject | None = None,
        audit_id: str | None = None,
        expected_before_sha256: str | None = None,
    ) -> None:
        """Atomically persist one allowed record transition and append its immutable audit event.

        ``decision`` is the coordinator decision this transition records; it is stored in the same
        audit event, under the same checksum, so the decision and its transition cannot diverge.
        ``expected_before_sha256`` makes the write a compare-and-swap: a record whose bytes no
        longer have that digest is refused before anything is written.
        """
        generation, relative, before, before_sha256 = self._check_replace(
            path, value, decision
        )
        if (
            expected_before_sha256 is not None
            and before_sha256 != expected_before_sha256
        ):
            raise LedgerWriteConflict(
                f"ledger record changed since its transition was planned: {relative}",
                remedy="read the record again and repeat the command; another writer changed it",
            )
        self._atomic_write(path, value)
        action, transition = audit_details(
            "replace",
            relative,
            _digest(path),
            before_sha256=before_sha256,
            before=before,
            after=value,
        )
        if decision is not None:
            transition["decision"] = decision
        self._append_audit(generation, action, transition, audit_id=audit_id)

    def _check_replace(
        self, path: Path, value: JsonObject, decision: JsonObject | None
    ) -> tuple[Path, str, JsonObject, str]:
        """Every check of ``replace`` that needs no write: the generation, relative path, current
        record and its digest of an allowed transition, or a ``LedgerError``."""
        _validate_record_write(path.parent.name, value)
        generation, relative = self._selected_path(path)
        if not path.is_file():
            raise LedgerError(
                f"ledger transition targets a missing record: {relative}",
                remedy=f"write the record at {path} before transitioning it",
            )
        before = _read(path, "current lifecycle record")
        if relative.startswith("batches/"):
            self._validate_batch_transition(generation, before, value)
            events = cast(list[JsonObject], value.get("recovery_events", []))
            if len(events) > len(
                cast(list[JsonObject], before.get("recovery_events", []))
            ):
                if decision != event_audit(events[-1]):
                    raise LedgerError(
                        "recovery event requires its matching audit decision",
                        remedy="use the recovery command to bind approval and transition in one audit record",
                    )
        return generation, relative, before, _digest(path)

    def check_step(self, mode: str, path: Path, value: JsonObject | str) -> None:
        """Refuse exactly what the write of ``mode`` would refuse, without writing anything.

        ``mode`` is ``create`` or ``replace`` for a JSON record and ``create-artifact`` for a
        text artifact; a create refuses a path that already exists."""
        if mode == "replace":
            self._check_replace(path, cast(JsonObject, value), None)
            return
        if mode == "create":
            _validate_record_write(path.parent.name, cast(JsonObject, value))
        elif mode != "create-artifact":
            raise LedgerError(
                f"unknown ledger write mode {mode!r}", remedy=INTERNAL_INVARIANT_REMEDY
            )
        self._selected_path(path)
        if path.exists():
            raise LedgerWriteConflict(
                f"refusing to overwrite immutable record: {path.name}",
                remedy=f"use a different record id, or 'ledger clean'/inspect {path} if it is orphaned evidence",
            )

    def read_audit(self, audit_id: str) -> JsonObject | None:
        """The audit event ``audit_id`` of the selected generation with a verified checksum, or
        ``None`` when it was never appended."""
        self._check_record_id(audit_id)
        path = self.records_root() / "audit" / f"{audit_id}.json"
        return verified_audit(path) if path.is_file() else None

    def append_audit(
        self, path: Path, action: str, details: JsonObject, *, audit_id: str
    ) -> None:
        """Append the audit event ``audit_id`` of a write to ``path`` that is already on disk;
        an identical event already appended is kept, a different one is refused."""
        generation, _ = self._selected_path(path)
        self._append_audit(generation, action, details, audit_id=audit_id)

    def delete(self, path: Path, *, reason: str) -> None:
        """Delete a mutable coordination lease/queue record with immutable evidence of removal."""
        generation, relative = self._selected_path(path)
        if not path.is_file():
            raise LedgerError(
                f"ledger deletion targets a missing record: {relative}",
                remedy=f"verify {path} exists before deleting it",
            )
        previous = _digest(path)
        path.unlink()
        self._append_audit(
            generation,
            "deletion",
            {"path": relative, "sha256": previous, "reason": reason},
        )

    def _selected_path(self, path: Path) -> tuple[Path, str]:
        generation = self.records_root()
        try:
            relative = path.resolve().relative_to(generation).as_posix()
        except ValueError as exc:
            raise LedgerError(
                "lifecycle record escapes the selected generation",
                remedy=f"pass a path inside the selected generation {generation}",
            ) from exc
        if relative.startswith("audit/"):
            raise LedgerError(
                "lifecycle audit records are append-only",
                remedy="write a new audit event instead of modifying an existing one",
            )
        return generation, relative

    @staticmethod
    def _atomic_write(path: Path, value: JsonObject) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(_canonical(value), encoding="utf-8", newline="\n")
            LifecycleLedger._replace_with_windows_retry(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _replace_with_windows_retry(source: Path, target: Path) -> None:
        """Retry brief Windows sharing conflicts while preserving atomic replacement."""
        for attempt in range(5):
            try:
                os.replace(source, target)
                return
            except PermissionError:
                if os.name != "nt" or attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))

    def _validate_batch_transition(
        self, generation: Path, before: JsonObject, after: JsonObject
    ) -> None:
        previous = _parse_state(before.get("state"), BatchState)
        target = _parse_state(after.get("state"), BatchState)
        old_events = before.get("recovery_events", [])
        new_events = after.get("recovery_events", [])
        if (
            not isinstance(old_events, list)
            or not isinstance(new_events, list)
            or new_events[: len(old_events)] != old_events
            or len(new_events) > len(old_events) + 1
        ):
            raise LedgerError(
                "recovery events are append-only",
                remedy="use one audited recovery command; never replace or remove historical evidence",
            )
        added = new_events[len(old_events) :]
        recovery = added[-1] if added else None
        if recovery is not None:
            if (
                not isinstance(recovery, dict)
                or recovery.get("sequence") != len(new_events)
                or recovery.get("before_state") != previous
                or recovery.get("state") != target
                or recovery.get("next_action") != after.get("next_action")
            ):
                raise LedgerError(
                    "recovery event does not bind this transition",
                    remedy="record the actual before/after state and next gate through the coordinator",
                )
            if recovery != sealed(
                {k: v for k, v in recovery.items() if k != "record_sha256"}
            ):
                raise LedgerError(
                    "recovery event checksum mismatch",
                    remedy="use the coordinator to create a sealed event",
                )
        if before.get("auto_stop") and before["auto_stop"] != after.get("auto_stop"):
            raise LedgerError(
                "historical auto stop is immutable",
                remedy="append a recovery event; preserve the original stop",
            )
        if before.get("manual_recovery") and not after.get("manual_recovery"):
            raise LedgerError(
                "recovered batches remain manual",
                remedy="keep manual_all after recovery",
            )
        if (
            after.get("manual_recovery")
            and not before.get("manual_recovery")
            and (not recovery or recovery.get("kind") not in {"resume-stop", "rewind"})
        ):
            raise LedgerError(
                "manual recovery requires a fresh recovery event",
                remedy="use batch resume-stop or rewind",
            )
        old_startup = before.get("startup_evidence", [])
        new_startup = after.get("startup_evidence", [])
        if (
            not isinstance(old_startup, list)
            or not isinstance(new_startup, list)
            or new_startup[: len(old_startup)] != old_startup
        ):
            raise LedgerError(
                "startup evidence is append-only",
                remedy="preserve original startup proof",
            )
        allowed = {
            BatchState.PLANNED: {
                BatchState.PLANNED,
                BatchState.AWAITING_APPROVAL,
                BatchState.BLOCKED,
                BatchState.FAILED,
                BatchState.NOT_REQUIRED,
                BatchState.ABANDONED,
            },
            BatchState.AWAITING_APPROVAL: {
                BatchState.AWAITING_APPROVAL,
                BatchState.ACTIVE,
                BatchState.COMPLETED,
                BatchState.BLOCKED,
                BatchState.FAILED,
                BatchState.NOT_REQUIRED,
                BatchState.ABANDONED,
            },
            BatchState.ACTIVE: {
                BatchState.ACTIVE,
                BatchState.AWAITING_APPROVAL,
                BatchState.BLOCKED,
                BatchState.FAILED,
                BatchState.NOT_REQUIRED,
                BatchState.ABANDONED,
            },
            BatchState.BLOCKED: {
                BatchState.BLOCKED,
                BatchState.AWAITING_APPROVAL,
                BatchState.FAILED,
                BatchState.ABANDONED,
            },
            BatchState.FAILED: {BatchState.FAILED, BatchState.ABANDONED},
            BatchState.COMPLETED: {BatchState.COMPLETED},
            BatchState.NOT_REQUIRED: {BatchState.NOT_REQUIRED},
            BatchState.ABANDONED: {BatchState.ABANDONED},
        }
        for state in (
            BatchState.PLANNED,
            BatchState.AWAITING_APPROVAL,
            BatchState.ACTIVE,
            BatchState.BLOCKED,
            BatchState.FAILED,
        ):
            allowed[state].add(BatchState.PAUSED)
        allowed[BatchState.PAUSED] = {
            BatchState.PAUSED,
            BatchState.AWAITING_APPROVAL,
            BatchState.PLANNED,
            BatchState.ABANDONED,
        }
        allowed[BatchState.FAILED].update(
            {BatchState.AWAITING_APPROVAL, BatchState.PLANNED}
        )
        allowed[BatchState.ACTIVE].add(BatchState.PLANNED)
        allowed[BatchState.BLOCKED].add(BatchState.PLANNED)
        for state in (
            BatchState.PLANNED,
            BatchState.ACTIVE,
            BatchState.BLOCKED,
            BatchState.FAILED,
            BatchState.PAUSED,
        ):
            allowed[state].add(BatchState.COMPLETED)
        if target not in allowed[previous]:
            allowed_targets = sorted(allowed[previous])
            raise LedgerError(
                f"ledger rejects batch transition {previous.value!r} -> {target.value!r}",
                remedy=f"transition through one of the allowed states for {previous.value!r}: {[state.value for state in allowed_targets]}",
            )
        if previous == "planned" and target == "awaiting-approval":
            approval = after.get("coordinator_approval")
            if not isinstance(approval, dict) or not all(
                isinstance(value := approval.get(key), str) and value.strip()
                for key in ("approved_by", "approved_at")
            ):
                raise LedgerError(
                    "ledger requires recorded coordinator approval before a batch awaits dispatch",
                    remedy="set coordinator_approval.approved_by and .approved_at before moving the batch to awaiting-approval",
                )
        if target == "abandoned" and previous != "abandoned":
            refusal = after.get("abandoned")
            if (
                not isinstance(refusal, dict)
                or not all(
                    isinstance(item := refusal.get(key), str) and item.strip()
                    for key in ("approved_by", "approved_at", "reason")
                )
                or not isinstance(refusal.get("approved_by"), str)
                or cast(str, refusal["approved_by"]).startswith("policy:")
            ):
                raise LedgerError(
                    "abandon requires recorded human refusal",
                    remedy="record approved_by, approved_at and reason through batch abandon",
                )
        if (
            target == "paused"
            and previous != "paused"
            and (not recovery or recovery.get("kind") != "pause")
        ):
            raise LedgerError(
                "pause requires a recorded stop event",
                remedy="use batch auto-decide to record the policy stop",
            )
        recovery_transition = (
            previous in {"paused", "failed", "blocked"}
            and target in {"planned", "awaiting-approval"}
        ) or (previous == "active" and target == "planned")
        human_event = (
            recovery
            and recovery.get("kind") in {"resume-stop", "rewind"}
            and isinstance(recovery.get("approved_by"), str)
            and not cast(str, recovery["approved_by"]).startswith("policy:")
        )
        if (
            recovery_transition
            and not human_event
            and not (
                previous == "blocked"
                and target == "awaiting-approval"
                and not new_events
            )
        ):
            raise LedgerError(
                "recovery transition requires a fresh human event",
                remedy="use batch resume-stop or rewind with human approval",
            )
        if previous == "blocked" and target == "awaiting-approval" and not human_event:
            before_entries = before.get("dispatches")
            after_entries = after.get("dispatches")
            decisions = after.get("coordinator_decisions")
            last_before = (
                before_entries[-1]
                if isinstance(before_entries, list) and before_entries
                else None
            )
            last_after = (
                after_entries[-1]
                if isinstance(after_entries, list) and after_entries
                else None
            )
            resume = (
                decisions[-1] if isinstance(decisions, list) and decisions else None
            )
            if not (
                isinstance(last_before, dict)
                and last_before.get("state") == "blocked"
                and isinstance(last_after, dict)
                and last_after.get("dispatch_id") == last_before.get("dispatch_id")
                and last_after.get("state") == "abandoned"
                and isinstance(resume, dict)
                and resume.get("decision") == "resume"
                and resume.get("dispatch_id") == last_before.get("dispatch_id")
                and isinstance(after.get("next_action"), str)
            ):
                raise LedgerError(
                    "ledger resume requires a retired blocked dispatch and recorded next action",
                    remedy="record a resume decision for the blocked dispatch without changing its immutable brief or accepted history",
                )
        if previous == "awaiting-approval" and target == "active":
            dispatches = after.get("dispatches")
            if not isinstance(dispatches, list) or not dispatches:
                raise LedgerError(
                    "ledger requires an approved dispatch before activating a batch",
                    remedy="approve a dispatch for this batch before activating it",
                )
            dispatch_id = (
                dispatches[-1].get("dispatch_id")
                if isinstance(dispatches[-1], dict)
                else None
            )
            if not isinstance(dispatch_id, str):
                raise LedgerError(
                    "ledger requires a valid approved dispatch ID before activating a batch",
                    remedy="ensure the batch's last dispatch entry has a string dispatch_id before activating it",
                )
            dispatch = _read(
                generation / "dispatches" / f"{dispatch_id}.json", "approved dispatch"
            )
            if dispatch.get("state") != "approved" or not isinstance(
                dispatch.get("coordinator_approval"), dict
            ):
                raise LedgerError(
                    "ledger requires an immutable approved dispatch before activating a batch",
                    remedy=f"approve dispatch {dispatch_id!r} (state=approved with coordinator_approval) before activating this batch",
                )

    def _legacy_records_present(self) -> bool:
        return any(
            (self.root / directory).exists()
            for directory in RECORD_DIRECTORIES
            if directory != "audit"
        )

    def _new_generation(self, purpose: str) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        # State roots commonly sit below a long project path on Windows.  Keep the generation
        # selector short enough that immutable record IDs and atomic temporary names remain below
        # the traditional path-length limit.
        generation = self.root / GENERATIONS / f"generation-{uuid.uuid4().hex[:8]}"
        while generation.exists():
            generation = self.root / GENERATIONS / f"generation-{uuid.uuid4().hex[:8]}"
        generation.mkdir(parents=True)
        for directory in RECORD_DIRECTORIES:
            (generation / directory).mkdir(exist_ok=True)
        self._append_audit(generation, "generation-created", {"purpose": purpose})
        return generation

    def _select(self, generation: Path) -> JsonObject:
        self._validate_generation(generation)
        pointer: JsonObject = {
            "version": LEDGER_VERSION,
            "generation": generation.name,
            "selected_at": _now(),
        }
        temporary = self.pointer_path.with_name(
            f".{POINTER_NAME}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(_canonical(pointer), encoding="utf-8", newline="\n")
            self._replace_with_windows_retry(temporary, self.pointer_path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return pointer

    def select_generation(
        self, name: str, *, expected_pointer_sha256: str | None
    ) -> JsonObject:
        """Select the complete generation ``name`` while the pointer still holds the bytes whose
        digest is ``expected_pointer_sha256`` (``None``: no pointer exists).  A pointer that
        changed meanwhile is a ``LedgerWriteConflict`` and stays as it is; the replace itself is
        atomic.  The caller holds the ledger lock."""
        if not name.startswith("generation-") or "/" in name or "\\" in name:
            raise LedgerError(
                f"not a ledger generation name: {name!r}",
                remedy=INTERNAL_INVARIANT_REMEDY,
            )
        current = _digest(self.pointer_path) if self.pointer_path.is_file() else None
        if current != expected_pointer_sha256:
            raise LedgerWriteConflict(
                "the ledger pointer changed since the generation switch was planned",
                remedy="repeat the command; it plans the switch against the current pointer",
            )
        return self._select(self.root / GENERATIONS / name)

    def validate_generation(self, generation: Path) -> None:
        """Validate the complete generation directory ``generation``, selected or not, as the
        ledger validates a generation before it selects it; any defect is a ``LedgerError``."""
        self._validate_generation(generation, complete=True)

    def append_generation_audit(
        self, generation: Path, action: str, details: JsonObject, *, audit_id: str
    ) -> None:
        """Append the audit event ``audit_id`` to ``generation``, a generation under construction
        that no pointer selects yet; an identical event already appended is kept, a different
        one is refused."""
        self._append_audit(generation, action, details, audit_id=audit_id)

    def _append_audit(
        self,
        generation: Path,
        action: str,
        details: JsonObject,
        *,
        audit_id: str | None = None,
    ) -> None:
        """Append one audit event.  A given ``audit_id`` is created exclusively, so repeating the
        same event is a no-op and a different event under that id is refused.  An event outside
        its closed vocabularies is refused before anything is written."""
        if (refusal := _audit_problem(action, details)) is not None:
            problem, remedy = refusal
            raise LedgerError(
                f"refusing a non-canonical audit event: {problem}", remedy=remedy
            )
        audit: JsonObject = {
            "audit_id": audit_id or f"audit-{uuid.uuid4()}",
            "at": _now(),
            "action": action,
            "details": details,
        }
        audit["record_sha256"] = hashlib.sha256(
            _canonical(audit).encode("utf-8")
        ).hexdigest()
        path = generation / "audit" / f"{audit['audit_id']}.json"
        if audit_id is None:
            path.write_text(_canonical(audit), encoding="utf-8", newline="\n")
            return
        self._check_record_id(audit_id)
        try:
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(_canonical(audit))
        except FileExistsError:
            existing = verified_audit(path)
            if existing.get("action") != action or existing.get("details") != details:
                raise LedgerError(
                    f"audit event {audit_id} already records a different write",
                    remedy=f"inspect {path}; an audit event is append-only and never rewritten",
                ) from None

    def _validate_legacy(self, root: Path) -> None:
        """Tolerantly validate a migration source: legacy (pre-ledger) state, or an older-schema
        generation that predates a record directory this schema version introduced."""
        for directory in RECORD_DIRECTORIES:
            source = root / directory
            if not source.exists():
                continue
            if not source.is_dir():
                raise LedgerError(
                    f"legacy lifecycle path is not a directory: {directory}",
                    remedy=f"remove or replace the non-directory at {source}",
                )
            self._validate_json_records(
                source,
                "legacy lifecycle record",
                allow_non_json=directory in {"qa-artifacts", "reports"},
            )
        self._validate_batch_plans(root)
        self._validate_record_graph(root)

    def _validate_generation(self, generation: Path, *, complete: bool = True) -> None:
        if not generation.is_dir():
            raise LedgerError(
                "selected ledger generation is missing",
                remedy=f"restore or re-migrate the generation directory {generation}",
            )
        for directory in RECORD_DIRECTORIES:
            path = generation / directory
            if not path.is_dir():
                raise LedgerError(
                    f"ledger generation is missing {directory}",
                    remedy=f"restore the {directory} directory under {generation}",
                )
            self._validate_json_records(
                path,
                "ledger record",
                allow_non_json=directory in {"qa-artifacts", "reports"},
            )
            if directory == "audit":
                self._validate_audit(path)
        self._validate_batch_plans(generation)
        if complete:
            self._validate_record_graph(generation)

    @staticmethod
    def _validate_json_records(
        root: Path, label: str, *, allow_non_json: bool = False
    ) -> None:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix != ".json":
                if allow_non_json:
                    continue
                raise LedgerError(
                    f"{label} has an unsupported file: {path.name}",
                    remedy=f"remove or rename {path} to end in .json",
                )
            _read(path, label)

    @staticmethod
    def _validate_batch_plans(root: Path) -> None:
        batches = root / "batches"
        plans = root / "plans"
        for batch_path in batches.glob("*.json"):
            batch = _read(batch_path, "batch record")
            plan_path = plans / batch_path.name
            if not plan_path.is_file():
                raise LedgerError(
                    f"batch has no immutable plan: {batch_path.name}",
                    remedy=f"restore the missing plan record {plan_path}",
                )
            plan = _read(plan_path, "immutable batch plan")
            if any(batch.get(key) != value for key, value in plan.items()):
                raise LedgerError(
                    f"batch does not match its immutable plan: {batch_path.name}",
                    remedy=f"the batch record and its immutable plan {plan_path} have diverged -- {INTERNAL_INVARIANT_REMEDY}",
                )

    @staticmethod
    def _validate_record_graph(root: Path) -> None:
        """Reject syntactically-valid but incomplete or orphaned lifecycle evidence."""
        dispatches = {path.stem for path in (root / "dispatches").glob("*.json")}
        statuses = {path.stem for path in (root / "dispatch-status").glob("*.json")}
        referenced: set[str] = set()
        for batch_path in (root / "batches").glob("*.json"):
            referenced |= LifecycleLedger.check_batch_references(
                root, _read(batch_path, "batch record"), batch_path.name
            )
        if dispatches != referenced or statuses != referenced:
            raise LedgerError(
                "lifecycle state contains orphaned dispatch evidence (run 'ledger clean' to fix)",
                remedy="run 'coordinator.py ledger clean' to remove dispatch/status records no batch references",
            )

    @staticmethod
    def check_batch_references(root: Path, batch: JsonObject, name: str) -> set[str]:
        """Check what the batch record ``name`` names in the generation ``root``: the brief and
        status of each dispatch entry, the brief's ``brief_sha256`` and, for a reported entry,
        its completion report and ``report_sha256``; returns the dispatch IDs it references.
        A violation is a ``LedgerError``, and nothing is written.  A dispatch and status no
        batch names yet pass: a composite write creates them before it names them."""
        entries = batch.get("dispatches", [])
        if not isinstance(entries, list):
            raise LedgerError(
                f"batch dispatches are invalid: {name}",
                remedy=f"its dispatches field is not a list; {_no_repair(name)}",
            )
        referenced: set[str] = set()
        for entry in entries:
            # The ID is joined under the generation: a separator would lead out of it.
            if (
                not isinstance(entry, dict)
                or not isinstance(dispatch_id := entry.get("dispatch_id"), str)
                or not _path_segment(dispatch_id)
            ):
                raise LedgerError(
                    f"batch has an invalid dispatch entry: {name}",
                    remedy="a dispatches entry is not an object with a dispatch_id that is "
                    f"one path segment; {_no_repair(name)}",
                )
            referenced.add(dispatch_id)
            dispatch_path = root / "dispatches" / f"{dispatch_id}.json"
            status_path = root / "dispatch-status" / f"{dispatch_id}.json"
            if not dispatch_path.is_file() or not status_path.is_file():
                raise LedgerError(
                    f"batch dispatch evidence is incomplete: {dispatch_id}",
                    remedy=f"restore the missing dispatches/{dispatch_id}.json and/or dispatch-status/{dispatch_id}.json records",
                )
            dispatch = _read(dispatch_path, "dispatch record")
            status = _read(status_path, "dispatch status")
            if dispatch.get("dispatch_id") != dispatch_id or dispatch.get(
                "batch_id"
            ) != batch.get("batch_id"):
                raise LedgerError(
                    f"dispatch does not belong to its batch: {dispatch_id}",
                    remedy=f"dispatches/{dispatch_id}.json names another dispatch_id or "
                    f"batch_id than {name} gives it; {_no_repair(name)}",
                )
            if status.get("dispatch_id") != dispatch_id or not isinstance(
                status.get("state"), str
            ):
                raise LedgerError(
                    f"dispatch status is invalid: {dispatch_id}",
                    remedy=f"fix dispatch-status/{dispatch_id}.json's dispatch_id/state fields",
                )
            expected_brief = entry.get("brief_sha256")
            if (
                expected_brief
                != hashlib.sha256(_canonical(dispatch).encode("utf-8")).hexdigest()
            ):
                raise LedgerError(
                    f"dispatch failed immutable brief integrity check: {dispatch_id}",
                    remedy=f"dispatches/{dispatch_id}.json was modified after its brief_sha256 was recorded -- {INTERNAL_INVARIANT_REMEDY}",
                )
            if entry.get("state") == "reported":
                report_name = entry.get("report")
                if (
                    not isinstance(report_name, str)
                    or Path(report_name).is_absolute()
                    or ".." in Path(report_name).parts
                ):
                    raise LedgerError(
                        f"reported dispatch has an invalid report path: {dispatch_id}",
                        remedy=f"the report path {name} gives dispatch {dispatch_id} is not a "
                        f"relative path inside the generation; {_no_repair(name)}",
                    )
                report_path = root / report_name
                report = _read(report_path, "completion report")
                if (
                    entry.get("report_sha256")
                    != hashlib.sha256(_canonical(report).encode("utf-8")).hexdigest()
                ):
                    raise LedgerError(
                        f"completion report failed immutable integrity check: {dispatch_id}",
                        remedy=f"{report_path} was modified after its report_sha256 was recorded -- {INTERNAL_INVARIANT_REMEDY}",
                    )
        return referenced

    @staticmethod
    def check_batch_transitions(root: Path, batch: JsonObject, name: str) -> None:
        """Check that the batch record ``name`` of the generation ``root`` holds the state its
        recorded transitions left: the transitions form one hash chain (``_transition_chain``),
        each starts in the state the one before it ended in, and the last one ends in the
        batch's current version and state.  A batch no transition moved holds the version its
        creation recorded; one imported by a migration has no such event and passes.  The same
        holds for what the batch names: the status of each dispatch ends the hash chain of its
        recorded transitions in the version it holds, or holds the version its creation
        recorded, and a dispatch brief holds the version its creation recorded.

        Only the strict read runs this check, after a command reconciled its batch: a transition
        whose audit event an interrupted write has not appended yet stops the read, never the
        reconciliation that appends it.  A violation is a ``LedgerError``; nothing is written."""
        writes: dict[str, list[tuple[str, JsonObject]]] = {}
        for path in sorted((root / "audit").glob("*.json")):
            event = _read(path, "ledger audit record")
            details = event.get("details")
            if event.get("action") in ("transition", "immutable-record") and isinstance(
                details, dict
            ):
                writes.setdefault(str(details.get("path")), []).append(
                    (str(event["action"]), details)
                )
        LifecycleLedger._check_batch_chain(root, batch, name, writes)
        entries = batch.get("dispatches")
        for entry in entries if isinstance(entries, list) else []:
            dispatch_id = entry.get("dispatch_id") if isinstance(entry, dict) else None
            if not isinstance(dispatch_id, str):
                continue  # the reference check refuses it
            LifecycleLedger._check_dispatch_records(root, name, dispatch_id, writes)

    @staticmethod
    def _check_batch_chain(
        root: Path,
        batch: JsonObject,
        name: str,
        writes: dict[str, list[tuple[str, JsonObject]]],
    ) -> None:
        """The part of ``check_batch_transitions`` about the batch record itself."""
        relative = f"batches/{name}"
        recorded = [
            d for action, d in writes.get(relative, []) if action == "transition"
        ]
        if not recorded:
            _check_creation(root, relative, writes, "batch", name, _no_repair(name))
            return
        chain = _transition_chain(name, recorded, _digest(root / f"batches/{name}"))
        for earlier, later in zip(chain, chain[1:]):
            if later.get("from") != earlier.get("to"):
                raise LedgerError(
                    f"batch transitions name two states for one record version: {name} moved "
                    f"from {later.get('from')!r} after a transition that left it "
                    f"{earlier.get('to')!r}",
                    remedy="two linked transitions name different states for one version of "
                    f"the batch record, which the ledger never writes; {_no_repair(name)}",
                )
        if batch.get("state") != (last := chain[-1].get("to")):
            raise _changed_after_last_transition(
                name,
                f"batch state is not the one its last recorded transition left: {name} holds "
                f"{batch.get('state')!r}, its audit trail ends in {last!r}",
            )

    @staticmethod
    def _check_dispatch_records(
        root: Path,
        name: str,
        dispatch_id: str,
        writes: dict[str, list[tuple[str, JsonObject]]],
    ) -> None:
        """The brief and the status of ``dispatch_id``, a dispatch of the batch record ``name``,
        against the audit events ``writes`` of their paths; a record the reference check finds
        missing is left to it."""
        _check_creation(
            root,
            f"dispatches/{dispatch_id}.json",
            writes,
            "dispatch brief",
            f"{dispatch_id}.json",
            _no_repair(name),
        )
        relative = f"dispatch-status/{dispatch_id}.json"
        status = root / relative
        transitions = [
            details
            for action, details in writes.get(relative, [])
            if action == "transition"
        ]
        if not transitions:
            _check_creation(
                root,
                relative,
                writes,
                "dispatch status",
                f"{dispatch_id}.json",
                _no_repair(name),
            )
        elif status.is_file():
            _transition_chain(
                f"{dispatch_id}.json",
                transitions,
                _digest(status),
                subject="dispatch status",
                repair=_no_repair(name),
            )

    @staticmethod
    def _validate_audit(root: Path) -> None:
        """Verify each audit event's checksum and its closed vocabularies: an event the ledger
        never writes stops every read, and no migration takes it over."""
        for path in root.glob("*.json"):
            record = _read(path, "ledger audit record")
            checksum = record.pop("record_sha256", None)
            expected = hashlib.sha256(_canonical(record).encode("utf-8")).hexdigest()
            if checksum != expected:
                raise LedgerError(
                    f"ledger audit record failed immutable integrity check: {path.name}",
                    remedy=f"{path} was modified after its record_sha256 was recorded -- {INTERNAL_INVARIANT_REMEDY}",
                )
            refusal = _audit_problem(record.get("action"), record.get("details"))
            if refusal is not None:
                raise LedgerError(
                    f"ledger audit record {path.name} holds a non-canonical machine value: "
                    f"{refusal[0]}",
                    remedy=f"read {path.name} with {LEDGER_INSPECT}: the ledger never writes "
                    f"such an event and rewrites none -- {INTERNAL_INVARIANT_REMEDY}",
                )
