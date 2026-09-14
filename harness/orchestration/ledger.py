"""Versioned, append-audited persistence for the orchestration lifecycle.

The coordinator is deliberately only a CLI adapter.  This module selects the active record
generation, records immutable writes and transitions, and performs the one explicit migration
from the pre-ledger directory layout.  A pointer is the only mutable selector: a replacement
generation is validated and moved into place before that pointer is atomically switched.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LEDGER_VERSION = 1
POINTER_NAME = "ledger.json"
GENERATIONS = "generations"
RECORD_DIRECTORIES = (
    "batches", "plans", "dispatches", "dispatch-status", "risk-assessments", "reports",
    "qa-lane", "qa-artifacts", "audit",
)


class LedgerError(Exception):
    """The durable lifecycle state cannot safely be selected or changed."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _read(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerError(f"{label} is not valid JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise LedgerError(f"{label} must be a JSON object: {path.name}")
    return value


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class LifecycleLedger:
    """Own the selected versioned record generation and its append-only audit."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    @property
    def pointer_path(self) -> Path:
        return self.root / POINTER_NAME

    def pointer(self) -> dict[str, Any] | None:
        if not self.pointer_path.exists():
            return None
        pointer = _read(self.pointer_path, "ledger pointer")
        if set(pointer) != {"version", "generation", "selected_at"}:
            raise LedgerError("ledger pointer has an invalid schema")
        if pointer["version"] != LEDGER_VERSION or not isinstance(pointer["generation"], str):
            raise LedgerError("ledger pointer has an unsupported version or generation")
        if not pointer["generation"].startswith("generation-") or not isinstance(pointer["selected_at"], str):
            raise LedgerError("ledger pointer has an invalid generation")
        return pointer

    def records_root(self) -> Path:
        """Return the selected generation; legacy state is readable only by ``migrate``."""
        pointer = self.pointer()
        if pointer is None:
            if self._legacy_records_present():
                raise LedgerError("legacy lifecycle state requires an explicit ledger migrate")
            return self.root
        generation = self.root / GENERATIONS / pointer["generation"]
        # A coordinator operation may create an immutable dispatch and its status record in two
        # writes under one state lock.  Validate the container and audit here; validate the full
        # cross-record graph before a migration/selects a generation.
        self._validate_generation(generation, complete=False)
        return generation

    def ensure(self) -> dict[str, Any]:
        """Create the first empty generation; never reinterpret legacy records implicitly."""
        pointer = self.pointer()
        if pointer is not None:
            return pointer
        if self._legacy_records_present():
            raise LedgerError("legacy lifecycle state requires an explicit ledger migrate")
        generation = self._new_generation("initialize")
        return self._select(generation)

    def status(self) -> dict[str, Any]:
        pointer = self.pointer()
        if pointer is None:
            return {"version": 0, "generation": None, "legacy": self._legacy_records_present()}
        root = self.records_root()
        return {
            "version": pointer["version"],
            "generation": pointer["generation"],
            "selected_at": pointer["selected_at"],
            "audit_records": len(list((root / "audit").glob("*.json"))),
        }

    def migrate(self) -> dict[str, Any]:
        pointer = self.pointer()
        if pointer is not None:
            return {"version": pointer["version"], "generation": pointer["generation"], "migrated": False}
        self._validate_legacy()
        generation = self._new_generation("migration")
        for directory in RECORD_DIRECTORIES:
            source = self.root / directory
            if source.exists():
                shutil.copytree(source, generation / directory, dirs_exist_ok=True)
        self._validate_generation(generation)
        imported = {
            path.relative_to(self.root).as_posix(): _digest(path)
            for directory in RECORD_DIRECTORIES
            for path in sorted((self.root / directory).rglob("*"))
            if path.is_file()
        }
        self._append_audit(generation, "migration", {"legacy_records": imported})
        self._validate_generation(generation)
        pointer = self._select(generation)
        return {"version": pointer["version"], "generation": pointer["generation"], "migrated": True}

    def reset(self, confirmation: str) -> dict[str, Any]:
        if confirmation != "RESET":
            raise LedgerError("ledger reset requires --confirm RESET")
        current = self.records_root()
        active = []
        for path in (current / "batches").glob("*.json"):
            batch = _read(path, "batch record")
            if batch.get("state") == "active":
                active.append(batch.get("batch_id", path.stem))
        if active:
            raise LedgerError("ledger reset is refused while batches are active: " + ", ".join(map(str, active)))
        previous = self.pointer()
        generation = self._new_generation("reset")
        self._append_audit(generation, "reset", {
            "previous_generation": previous["generation"] if previous else None,
            "confirmation": "RESET",
        })
        pointer = self._select(generation)
        return {"version": pointer["version"], "generation": pointer["generation"], "reset": True}

    def write_immutable(self, path: Path, value: dict[str, Any], *, artifact: bool = False) -> None:
        generation, relative = self._selected_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(_canonical(value))
        except FileExistsError as exc:
            raise LedgerError(f"refusing to overwrite immutable record: {path.name}") from exc
        self._append_audit(generation, "immutable-artifact" if artifact else "immutable-record", {
            "path": relative, "sha256": _digest(path),
        })

    def write_artifact(self, path: Path, value: str) -> None:
        generation, relative = self._selected_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(value)
        except FileExistsError as exc:
            if path.read_text(encoding="utf-8") != value:
                raise LedgerError(f"refusing to overwrite immutable artifact: {path.name}") from exc
        self._append_audit(generation, "immutable-artifact", {"path": relative, "sha256": _digest(path)})

    def replace(self, path: Path, value: dict[str, Any]) -> None:
        """Atomically persist one allowed record transition and append its immutable audit event."""
        generation, relative = self._selected_path(path)
        if not path.is_file():
            raise LedgerError(f"ledger transition targets a missing record: {relative}")
        before = _read(path, "current lifecycle record")
        transition: dict[str, Any] = {"path": relative, "before_sha256": _digest(path)}
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
            raise LedgerError(f"ledger deletion targets a missing record: {relative}")
        previous = _digest(path)
        path.unlink()
        self._append_audit(generation, "deletion", {"path": relative, "sha256": previous, "reason": reason})

    def _selected_path(self, path: Path) -> tuple[Path, str]:
        generation = self.records_root()
        try:
            relative = path.resolve().relative_to(generation).as_posix()
        except ValueError as exc:
            raise LedgerError("lifecycle record escapes the selected generation") from exc
        if relative.startswith("audit/"):
            raise LedgerError("lifecycle audit records are append-only")
        return generation, relative

    @staticmethod
    def _atomic_write(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(_canonical(value), encoding="utf-8", newline="\n")
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _validate_batch_transition(self, generation: Path, before: dict[str, Any], after: dict[str, Any]) -> None:
        previous = before.get("state")
        target = after.get("state")
        allowed = {
            "planned": {"planned", "awaiting-approval", "blocked", "failed"},
            "awaiting-approval": {"awaiting-approval", "active", "completed", "blocked", "failed"},
            "active": {"active", "awaiting-approval", "blocked", "failed"},
            "blocked": {"blocked", "failed"},
            "failed": {"failed"},
            "completed": {"completed", "failed"},
        }
        if previous not in allowed or target not in allowed[previous]:
            raise LedgerError(f"ledger rejects batch transition {previous!r} -> {target!r}")
        if previous == "planned" and target == "awaiting-approval":
            approval = after.get("coordinator_approval")
            if not isinstance(approval, dict) or not all(isinstance(approval.get(key), str) and approval[key].strip() for key in ("approved_by", "approved_at")):
                raise LedgerError("ledger requires recorded coordinator approval before a batch awaits dispatch")
        if previous == "awaiting-approval" and target == "active":
            dispatches = after.get("dispatches")
            if not isinstance(dispatches, list) or not dispatches:
                raise LedgerError("ledger requires an approved dispatch before activating a batch")
            dispatch_id = dispatches[-1].get("dispatch_id") if isinstance(dispatches[-1], dict) else None
            if not isinstance(dispatch_id, str):
                raise LedgerError("ledger requires a valid approved dispatch ID before activating a batch")
            dispatch = _read(generation / "dispatches" / f"{dispatch_id}.json", "approved dispatch")
            if dispatch.get("state") != "approved" or not isinstance(dispatch.get("coordinator_approval"), dict):
                raise LedgerError("ledger requires an immutable approved dispatch before activating a batch")

    def _legacy_records_present(self) -> bool:
        return any((self.root / directory).exists() for directory in RECORD_DIRECTORIES if directory != "audit")

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

    def _select(self, generation: Path) -> dict[str, Any]:
        self._validate_generation(generation)
        pointer = {"version": LEDGER_VERSION, "generation": generation.name, "selected_at": _now()}
        temporary = self.pointer_path.with_name(f".{POINTER_NAME}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(_canonical(pointer), encoding="utf-8", newline="\n")
            os.replace(temporary, self.pointer_path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return pointer

    def _append_audit(self, generation: Path, action: str, details: dict[str, Any]) -> None:
        audit = {
            "audit_id": f"audit-{uuid.uuid4()}",
            "at": _now(),
            "action": action,
            "details": details,
        }
        audit["record_sha256"] = hashlib.sha256(_canonical(audit).encode("utf-8")).hexdigest()
        path = generation / "audit" / f"{audit['audit_id']}.json"
        path.write_text(_canonical(audit), encoding="utf-8", newline="\n")

    def _validate_legacy(self) -> None:
        for directory in RECORD_DIRECTORIES:
            source = self.root / directory
            if not source.exists():
                continue
            if not source.is_dir():
                raise LedgerError(f"legacy lifecycle path is not a directory: {directory}")
            self._validate_json_records(
                source, "legacy lifecycle record", allow_non_json=directory in {"qa-artifacts", "reports"}
            )
        self._validate_batch_plans(self.root)
        self._validate_record_graph(self.root)

    def _validate_generation(self, generation: Path, *, complete: bool = True) -> None:
        if not generation.is_dir():
            raise LedgerError("selected ledger generation is missing")
        for directory in RECORD_DIRECTORIES:
            path = generation / directory
            if not path.is_dir():
                raise LedgerError(f"ledger generation is missing {directory}")
            self._validate_json_records(path, "ledger record", allow_non_json=directory in {"qa-artifacts", "reports"})
            if directory == "audit":
                self._validate_audit(path)
        self._validate_batch_plans(generation)
        if complete:
            self._validate_record_graph(generation)

    @staticmethod
    def _validate_json_records(root: Path, label: str, *, allow_non_json: bool = False) -> None:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix != ".json":
                if allow_non_json:
                    continue
                raise LedgerError(f"{label} has an unsupported file: {path.name}")
            _read(path, label)

    @staticmethod
    def _validate_batch_plans(root: Path) -> None:
        batches = root / "batches"
        plans = root / "plans"
        for batch_path in batches.glob("*.json"):
            batch = _read(batch_path, "batch record")
            plan_path = plans / batch_path.name
            if not plan_path.is_file():
                raise LedgerError(f"batch has no immutable plan: {batch_path.name}")
            plan = _read(plan_path, "immutable batch plan")
            if any(batch.get(key) != value for key, value in plan.items()):
                raise LedgerError(f"batch does not match its immutable plan: {batch_path.name}")

    @staticmethod
    def _validate_record_graph(root: Path) -> None:
        """Reject syntactically-valid but incomplete or orphaned lifecycle evidence."""
        dispatches = {path.stem: path for path in (root / "dispatches").glob("*.json")}
        statuses = {path.stem: path for path in (root / "dispatch-status").glob("*.json")}
        referenced: set[str] = set()
        for batch_path in (root / "batches").glob("*.json"):
            batch = _read(batch_path, "batch record")
            entries = batch.get("dispatches", [])
            if not isinstance(entries, list):
                raise LedgerError(f"batch dispatches are invalid: {batch_path.name}")
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("dispatch_id"), str):
                    raise LedgerError(f"batch has an invalid dispatch entry: {batch_path.name}")
                dispatch_id = entry["dispatch_id"]
                referenced.add(dispatch_id)
                dispatch_path = dispatches.get(dispatch_id)
                status_path = statuses.get(dispatch_id)
                if dispatch_path is None or status_path is None:
                    raise LedgerError(f"batch dispatch evidence is incomplete: {dispatch_id}")
                dispatch = _read(dispatch_path, "dispatch record")
                status = _read(status_path, "dispatch status")
                if dispatch.get("dispatch_id") != dispatch_id or dispatch.get("batch_id") != batch.get("batch_id"):
                    raise LedgerError(f"dispatch does not belong to its batch: {dispatch_id}")
                if status.get("dispatch_id") != dispatch_id or not isinstance(status.get("state"), str):
                    raise LedgerError(f"dispatch status is invalid: {dispatch_id}")
                expected_brief = entry.get("brief_sha256")
                if expected_brief != hashlib.sha256(_canonical(dispatch).encode("utf-8")).hexdigest():
                    raise LedgerError(f"dispatch failed immutable brief integrity check: {dispatch_id}")
                if entry.get("state") == "reported":
                    report_name = entry.get("report")
                    if not isinstance(report_name, str) or Path(report_name).is_absolute() or ".." in Path(report_name).parts:
                        raise LedgerError(f"reported dispatch has an invalid report path: {dispatch_id}")
                    report_path = root / report_name
                    report = _read(report_path, "completion report")
                    if entry.get("report_sha256") != hashlib.sha256(_canonical(report).encode("utf-8")).hexdigest():
                        raise LedgerError(f"completion report failed immutable integrity check: {dispatch_id}")
        if set(dispatches) != referenced or set(statuses) != referenced:
            raise LedgerError("lifecycle state contains orphaned dispatch evidence")

    @staticmethod
    def _validate_audit(root: Path) -> None:
        for path in root.glob("*.json"):
            record = _read(path, "ledger audit record")
            checksum = record.pop("record_sha256", None)
            expected = hashlib.sha256(_canonical(record).encode("utf-8")).hexdigest()
            if checksum != expected:
                raise LedgerError(f"ledger audit record failed immutable integrity check: {path.name}")
