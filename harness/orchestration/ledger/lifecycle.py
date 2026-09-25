"""Versioned, append-audited persistence for the orchestration lifecycle.

The coordinator is deliberately only a CLI adapter.  This module selects the active record
generation, records immutable writes and transitions, and performs the explicit migration from
the pre-ledger directory layout or from an older-schema generation.  A pointer is the only mutable
selector: a replacement generation is validated and moved into place before that pointer is
atomically switched.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Protocol, cast

from ...errors import INTERNAL_INVARIANT_REMEDY, HarnessError

LEDGER_VERSION = 3
SUPPORTED_LEDGER_VERSIONS = (1, 2, 3)
POINTER_NAME = "ledger.json"
GENERATIONS = "generations"
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

# Ledger records are persisted JSON.  Keep the dynamic boundary at ``_read`` explicit while
# preserving arbitrary, forward-compatible JSON in each record's ``extra`` fields.
type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
type JsonObject = dict[str, JsonValue]


class LedgerError(HarnessError):
    """The durable lifecycle state cannot safely be selected or changed."""


@dataclass(frozen=True)
class BatchRecord:
    """Value Object for a ``batches/*.json`` record."""

    directory: ClassVar[str] = "batches"

    batch_id: str
    state: str
    dispatches: list[JsonValue]
    coordinator_approval: JsonObject | None
    extra: JsonObject = field(default_factory=dict)

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
            state=cast(str, data.get("state")),
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
    state: str
    coordinator_approval: JsonObject | None
    extra: JsonObject = field(default_factory=dict)

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
            state=cast(str, data.get("state")),
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
    state: str
    extra: JsonObject = field(default_factory=dict)

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
            state=cast(str, data.get("state")),
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
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LedgerError(
            f"{label} is not valid JSON: {path.name}",
            remedy=f"fix the JSON syntax in {path}",
        ) from exc
    if not isinstance(value, dict):
        raise LedgerError(
            f"{label} must be a JSON object: {path.name}",
            remedy=f"rewrite {path} as a JSON object",
        )
    # json.loads is a dynamic boundary; after confirming the top-level object, JSON itself
    # guarantees string object keys and recursive JSON-compatible values.
    return cast(JsonObject, value)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class LifecycleLedger:
    """Own the selected versioned record generation and its append-only audit."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Non-blocking exclusive lock on ``self.root``, mirroring coordinator.py's own
        ``_state_lock`` technique (same lock path, same mkdir/rmdir mechanism) but raising
        ``LedgerError`` on contention so this module never imports an exception type from
        coordinator.py."""
        self.root.mkdir(parents=True, exist_ok=True)
        lock_dir = self.root / ".coordinator.lock"
        try:
            lock_dir.mkdir()
        except FileExistsError as exc:
            raise LedgerError(
                "ledger is locked by another operation",
                remedy=f"wait for the other operation to finish, or remove a stale lock at {lock_dir} if no operation is actually running",
            ) from exc
        try:
            yield
        finally:
            try:
                lock_dir.rmdir()
            except OSError:
                pass

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
        if not generation.startswith("generation-") or not isinstance(selected_at, str):
            raise LedgerError(
                "ledger pointer has an invalid generation",
                remedy=f"fix {self.pointer_path}: generation must start with 'generation-' and selected_at must be a string",
            )
        return pointer

    @staticmethod
    def _pointer_generation(pointer: JsonObject) -> str:
        """Narrow a generation selected by ``pointer()``, which already validates this field."""
        generation = pointer["generation"]
        assert isinstance(generation, str)
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
            if batch.get("state") == "active":
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

        referenced: set[str] = set()
        for batch_path in (root / "batches").glob("*.json"):
            try:
                batch = _read(batch_path, "batch record")
                entries = batch.get("dispatches", [])
                if isinstance(entries, list):
                    for entry in entries:
                        if isinstance(entry, dict) and isinstance(
                            entry.get("dispatch_id"), str
                        ):
                            dispatch_id = entry["dispatch_id"]
                            assert isinstance(dispatch_id, str)
                            referenced.add(dispatch_id)
            except LedgerError:
                continue

        removed = 0
        for directory in ("dispatches", "dispatch-status", "reports", "checkpoints"):
            dir_path = root / directory
            if dir_path.exists():
                for path in dir_path.glob("*.json"):
                    if path.stem not in referenced:
                        path.unlink()
                        removed += 1

        return {
            "version": pointer["version"] if pointer else 0,
            "generation": pointer["generation"] if pointer else None,
            "cleaned": removed,
        }

    def _record_path(self, record: LedgerRecordVO) -> Path:
        self._check_record_id(record.record_id)
        return self.records_root() / record.directory / f"{record.record_id}.json"

    @staticmethod
    def _check_record_id(record_id: object) -> None:
        if (
            not isinstance(record_id, str)
            or not record_id
            or "/" in record_id
            or "\\" in record_id
            or record_id in {".", ".."}
        ):
            raise LedgerError(
                "record id is not a valid path segment",
                remedy="use a non-empty record id with no path separators and not '.' or '..'",
            )

    def write_record(self, record: LedgerRecordVO) -> None:
        """Persist a new Value-Object-backed record, deriving its path from the record itself."""
        self.write_immutable(self._record_path(record), record.to_dict())

    def replace_record(self, record: LedgerRecordVO) -> None:
        """Persist a Value-Object-backed record transition, deriving its path from the record."""
        self.replace(self._record_path(record), record.to_dict())

    def write_immutable(
        self, path: Path, value: JsonObject, *, artifact: bool = False
    ) -> None:
        generation, relative = self._selected_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(_canonical(value))
        except FileExistsError as exc:
            raise LedgerError(
                f"refusing to overwrite immutable record: {path.name}",
                remedy=f"use a different record id, or 'ledger clean'/inspect {path} if it is orphaned evidence",
            ) from exc
        self._append_audit(
            generation,
            "immutable-artifact" if artifact else "immutable-record",
            {
                "path": relative,
                "sha256": _digest(path),
            },
        )

    def write_artifact(self, path: Path, value: str) -> None:
        generation, relative = self._selected_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(value)
        except FileExistsError as exc:
            if path.read_text(encoding="utf-8") != value:
                raise LedgerError(
                    f"refusing to overwrite immutable artifact: {path.name}",
                    remedy=f"write a new artifact under a different name instead of overwriting {path}",
                ) from exc
        self._append_audit(
            generation,
            "immutable-artifact",
            {"path": relative, "sha256": _digest(path)},
        )

    def replace(self, path: Path, value: JsonObject) -> None:
        """Atomically persist one allowed record transition and append its immutable audit event."""
        generation, relative = self._selected_path(path)
        if not path.is_file():
            raise LedgerError(
                f"ledger transition targets a missing record: {relative}",
                remedy=f"write the record at {path} before transitioning it",
            )
        before = _read(path, "current lifecycle record")
        transition: JsonObject = {"path": relative, "before_sha256": _digest(path)}
        if relative.startswith("batches/"):
            self._validate_batch_transition(generation, before, value)
            transition.update({"from": before.get("state"), "to": value.get("state")})
        self._atomic_write(path, value)
        transition["sha256"] = _digest(path)
        self._append_audit(generation, "transition", transition)

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
        previous = before.get("state")
        target = after.get("state")
        allowed = {
            "planned": {
                "planned",
                "awaiting-approval",
                "blocked",
                "failed",
                "not-required",
            },
            "awaiting-approval": {
                "awaiting-approval",
                "active",
                "completed",
                "blocked",
                "failed",
                "not-required",
                "abandoned",
            },
            "active": {
                "active",
                "awaiting-approval",
                "blocked",
                "failed",
                "not-required",
            },
            "blocked": {"blocked", "awaiting-approval", "failed"},
            "failed": {"failed"},
            "completed": {"completed", "failed"},
            "not-required": {"not-required"},
            "abandoned": {"abandoned"},
        }
        if (
            not isinstance(previous, str)
            or not isinstance(target, str)
            or previous not in allowed
            or target not in allowed[previous]
        ):
            allowed_targets = (
                sorted(allowed.get(previous, ())) if isinstance(previous, str) else []
            )
            raise LedgerError(
                f"ledger rejects batch transition {previous!r} -> {target!r}",
                remedy=f"transition through one of the allowed states for {previous!r}: {allowed_targets}",
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
        if previous == "blocked" and target == "awaiting-approval":
            before_entries = before.get("dispatches")
            after_entries = after.get("dispatches")
            decisions = after.get("coordinator_decisions")
            last_before = before_entries[-1] if isinstance(before_entries, list) and before_entries else None
            last_after = after_entries[-1] if isinstance(after_entries, list) and after_entries else None
            resume = decisions[-1] if isinstance(decisions, list) and decisions else None
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

    def _append_audit(self, generation: Path, action: str, details: JsonObject) -> None:
        audit: JsonObject = {
            "audit_id": f"audit-{uuid.uuid4()}",
            "at": _now(),
            "action": action,
            "details": details,
        }
        audit["record_sha256"] = hashlib.sha256(
            _canonical(audit).encode("utf-8")
        ).hexdigest()
        path = generation / "audit" / f"{audit['audit_id']}.json"
        path.write_text(_canonical(audit), encoding="utf-8", newline="\n")

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
        dispatches = {path.stem: path for path in (root / "dispatches").glob("*.json")}
        statuses = {
            path.stem: path for path in (root / "dispatch-status").glob("*.json")
        }
        referenced: set[str] = set()
        for batch_path in (root / "batches").glob("*.json"):
            batch = _read(batch_path, "batch record")
            entries = batch.get("dispatches", [])
            if not isinstance(entries, list):
                raise LedgerError(
                    f"batch dispatches are invalid: {batch_path.name}",
                    remedy=f"fix {batch_path.name} so its dispatches field is a list",
                )
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(
                    entry.get("dispatch_id"), str
                ):
                    raise LedgerError(
                        f"batch has an invalid dispatch entry: {batch_path.name}",
                        remedy=f"fix {batch_path.name} so each dispatches entry is an object with a string dispatch_id",
                    )
                dispatch_id = entry["dispatch_id"]
                assert isinstance(dispatch_id, str)
                referenced.add(dispatch_id)
                dispatch_path = dispatches.get(dispatch_id)
                status_path = statuses.get(dispatch_id)
                if dispatch_path is None or status_path is None:
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
                        remedy=f"fix dispatches/{dispatch_id}.json's dispatch_id/batch_id fields, or remove it from {batch_path.name}",
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
                            remedy=f"fix {batch_path.name}'s report path for dispatch {dispatch_id} to a relative path inside the generation",
                        )
                    report_path = root / report_name
                    report = _read(report_path, "completion report")
                    if (
                        entry.get("report_sha256")
                        != hashlib.sha256(
                            _canonical(report).encode("utf-8")
                        ).hexdigest()
                    ):
                        raise LedgerError(
                            f"completion report failed immutable integrity check: {dispatch_id}",
                            remedy=f"{report_path} was modified after its report_sha256 was recorded -- {INTERNAL_INVARIANT_REMEDY}",
                        )
        if set(dispatches) != referenced or set(statuses) != referenced:
            raise LedgerError(
                "lifecycle state contains orphaned dispatch evidence (run 'ledger clean' to fix)",
                remedy="run 'coordinator.py ledger clean' to remove dispatch/status records no batch references",
            )

    @staticmethod
    def _validate_audit(root: Path) -> None:
        for path in root.glob("*.json"):
            record = _read(path, "ledger audit record")
            checksum = record.pop("record_sha256", None)
            expected = hashlib.sha256(_canonical(record).encode("utf-8")).hexdigest()
            if checksum != expected:
                raise LedgerError(
                    f"ledger audit record failed immutable integrity check: {path.name}",
                    remedy=f"{path} was modified after its record_sha256 was recorded -- {INTERNAL_INVARIANT_REMEDY}",
                )
