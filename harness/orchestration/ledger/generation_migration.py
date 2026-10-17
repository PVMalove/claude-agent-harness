"""The verified generation of a system-fields ledger migration.

``ledger migrate`` moves the selected generation to the canonical ``systemFields.v1`` without
rewriting history.  The new generation copies every record of its source byte for byte, so every
``brief_sha256``, ``report_sha256`` and audit checksum stays valid, and the source generation stays
as it was.  What the migration found lives beside the records, in ``migration/``:

- ``manifest.json`` (``ledgerMigration.v1``) lists each historical value an explicit rule maps,
  with the digest of the file that holds it, and each quarantined batch;
- ``quarantine/<batch>/`` holds, byte for byte, the record graph of each batch with a
  ``legacy-unknown`` or ``unreadable`` finding, and its ``legacy-unknown.json`` evidence.

A quarantined graph leaves the operational record directories, so no ordinary loader meets the
unknown value, and nothing writes a sentinel, a default or a success into its field.  The source
is read as raw bytes, never through the ledger's validating reader, which one unknown value stops.

This module works only on the ledger; ``runtime_migration`` binds it to the runtime state store.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

from harness.errors import INTERNAL_INVARIANT_REMEDY
from harness.json_types import JsonObject
from harness.orchestration.core.system_fields import CONTRACT_VERSION
from harness.orchestration.core.utils import _canonical
from harness.orchestration.ledger.field_migration import (
    COMPLETION_REPORTS,
    MAPPED,
    UNREADABLE,
    Finding,
    checked_registry,
    file_findings,
    rules_sha256,
)
from harness.orchestration.ledger.lifecycle import (
    GENERATIONS,
    MIGRATED_DIRECTORIES,
    MIGRATION_DIRECTORY,
    RECORD_DIRECTORIES,
    SYSTEM_FIELDS_MIGRATION,
    LedgerError,
    LifecycleLedger,
    verified_audit,
)

MANIFEST_CONTRACT = "ledgerMigration.v1"
MANIFEST = "manifest.json"
QUARANTINE = "quarantine"
EVIDENCE = "legacy-unknown.json"
# A generation under construction: never a `generation-` name, so no pointer can select it.
STAGING_PREFIX = ".migrating-"
BATCH_ID = re.compile(r"batch-[0-9a-f-]+")
_MANIFEST_KEYS = frozenset(
    {
        "contract",
        "registry_contract",
        "source_generation",
        "source_pointer_sha256",
        "rules_sha256",
        "audit_id",
        "mapped",
        "quarantine",
    }
)
REFUSAL_REMEDY = "inspect the named record with 'coordinator.py ledger inspect'; the migration changed nothing"


@dataclass(frozen=True)
class Survey:
    """What the migration finds in a generation's operational records: the findings and the
    owning batch of every graph file and completion report, by path relative to the generation."""

    findings: Mapping[str, tuple[Finding, ...]]
    owners: Mapping[str, str]
    digests: Mapping[str, str]

    def unowned(self) -> list[str]:
        """The files whose finding quarantines a batch, yet no batch owns them."""
        return sorted(
            path
            for path, found in self.findings.items()
            if path not in self.owners and _quarantines(found)
        )

    def quarantined(self) -> list[str]:
        """The batches with a ``legacy-unknown`` or ``unreadable`` finding."""
        return sorted(
            {
                self.owners[path]
                for path, found in self.findings.items()
                if path in self.owners and _quarantines(found)
            }
        )

    def graph(self, batch_id: str) -> list[str]:
        """The files a quarantine of ``batch_id`` moves: its record graph and every other file
        of it that is unreadable, which no ordinary loader could leave in place."""
        return sorted(
            path
            for path, owner in self.owners.items()
            if owner == batch_id
            and (
                path.split("/")[0] in MIGRATED_DIRECTORIES
                or any(f.disposition == UNREADABLE for f in self.findings.get(path, ()))
            )
        )


def _quarantines(found: tuple[Finding, ...]) -> bool:
    return any(finding.disposition != MAPPED for finding in found)


def _object(raw: bytes) -> JsonObject | None:
    try:
        value: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return cast(JsonObject, value) if isinstance(value, dict) else None


def _safe_relative(value: object) -> str | None:
    """A path inside the generation as the ledger's graph check accepts it, else ``None``."""
    if not isinstance(value, str) or not value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        return None
    return path.as_posix()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def survey(generation: Path) -> Survey:
    """Find every historical value of the bound locations of ``generation`` and the batch that
    owns each record, reading raw bytes only.  A batch's dispatches belong to it by the batch's
    own reference first, then by the dispatch's ``batch_id``; its reports by its references."""
    registry = checked_registry()
    raw: dict[str, bytes] = {}
    for directory in MIGRATED_DIRECTORIES:
        for file in sorted((generation / directory).glob("*.json")):
            # Read as the ledger's own loader reads it, through a link, so the migration
            # quarantines whatever that loader would stop on.
            if file.is_file():
                raw[f"{directory}/{file.name}"] = file.read_bytes()
    parsed = {path: _object(data) for path, data in raw.items()}
    owners: dict[str, str] = {}
    reports: dict[str, str] = {}
    for path, record in parsed.items():
        directory, name = path.split("/", 1)
        stem = name.removesuffix(".json")
        if directory in ("batches", "plans"):
            owners[path] = stem
        if directory != "batches" or record is None:
            continue
        entries = record.get("dispatches")
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            if isinstance(dispatch_id := entry.get("dispatch_id"), str):
                for kind in ("dispatches", "dispatch-status"):
                    owners.setdefault(f"{kind}/{dispatch_id}.json", stem)
            if (report := _safe_relative(entry.get("report"))) is not None:
                reports.setdefault(report, stem)
    for path, record in parsed.items():
        directory, name = path.split("/", 1)
        batch_id = None if record is None else record.get("batch_id")
        if directory == "dispatches" and isinstance(batch_id, str):
            owners.setdefault(path, batch_id)
            owners.setdefault(f"dispatch-status/{name}", batch_id)
    findings: dict[str, tuple[Finding, ...]] = {}
    for path, data in raw.items():
        if found := file_findings(registry, path.split("/")[0], data):
            findings[path] = tuple(found)
    for report, owner in reports.items():
        target = generation / report
        # A record names this path, so a link here could lead out of the generation.
        if target.is_file() and not target.is_symlink():
            data = raw[report] = target.read_bytes()
            owners.setdefault(report, owner)
            if found := file_findings(registry, COMPLETION_REPORTS, data):
                findings[report] = tuple(found)
    present = {path: owner for path, owner in owners.items() if path in raw}
    return Survey(
        findings,
        present,
        {path: digest(raw[path]) for path in {*findings, *present}},
    )


def read_manifest(generation: Path) -> tuple[JsonObject, str] | None:
    """The migration manifest of ``generation`` and its digest, ``None`` when no migration
    selected it.  Every quarantined file and evidence must hold the bytes the manifest names, and
    nothing else may lie under ``migration/``; the quarantined records are never parsed."""
    directory = generation / MIGRATION_DIRECTORY
    if not directory.exists():
        return None
    path = directory / MANIFEST
    if directory.is_symlink() or not path.is_file() or path.is_symlink():
        raise _broken(generation, "has no readable migration manifest")
    data = path.read_bytes()
    manifest = _object(data)
    if (
        manifest is None
        or set(manifest) != _MANIFEST_KEYS
        or manifest["contract"] != MANIFEST_CONTRACT
        or not isinstance(manifest["mapped"], list)
        or not isinstance(manifest["quarantine"], list)
    ):
        raise _broken(
            generation, f"has a migration manifest that is not {MANIFEST_CONTRACT}"
        )
    expected = {MANIFEST: digest(data)}
    for entry in manifest["quarantine"]:
        expected.update(_entry_files(generation, entry))
    actual = {
        item.relative_to(directory).as_posix(): item
        for item in sorted(directory.rglob("*"))
        if item.is_file() or item.is_symlink()
    }
    if set(actual) != set(expected):
        extra = sorted(set(actual) ^ set(expected))
        raise _broken(
            generation, f"has migration files the manifest does not name: {extra}"
        )
    for relative, sha256 in expected.items():
        item = actual[relative]
        if item.is_symlink() or digest(item.read_bytes()) != sha256:
            raise _broken(generation, f"holds a changed migration file: {relative}")
    return manifest, expected[MANIFEST]


def _entry_files(generation: Path, entry: object) -> dict[str, str]:
    """The files of one quarantine entry, relative to ``migration/``, with their digests."""
    if not isinstance(entry, dict) or set(entry) != {"batch_id", "files", "evidence"}:
        raise _broken(generation, "has a malformed quarantine entry")
    batch_id, files, evidence = entry["batch_id"], entry["files"], entry["evidence"]
    base = f"{QUARANTINE}/{batch_id}"
    if (
        not isinstance(batch_id, str)
        or BATCH_ID.fullmatch(batch_id) is None
        or not isinstance(files, dict)
        or not isinstance(evidence, dict)
        or evidence.get("path") != f"{MIGRATION_DIRECTORY}/{base}/{EVIDENCE}"
        or not isinstance(evidence.get("sha256"), str)
    ):
        raise _broken(generation, "has a malformed quarantine entry")
    named: dict[str, str] = {f"{base}/{EVIDENCE}": evidence["sha256"]}
    for relative, sha256 in files.items():
        safe = _safe_relative(relative)
        if (
            safe is None
            or len(PurePosixPath(safe).parts) != 2
            or not isinstance(sha256, str)
        ):
            raise _broken(
                generation, f"names an invalid quarantined file of {batch_id}"
            )
        named[f"{base}/{safe}"] = sha256
    return named


def _broken(generation: Path, problem: str) -> LedgerError:
    return LedgerError(
        f"ledger generation {generation.name} {problem}",
        remedy=f"restore {generation / MIGRATION_DIRECTORY} from the source generation the "
        "manifest names, or select that source generation again",
    )


def migration_event(generation: Path, manifest: JsonObject) -> JsonObject:
    """The verified audit event that recorded ``manifest``'s migration in ``generation``."""
    audit_id = manifest["audit_id"]
    path = generation / "audit" / f"{audit_id}.json"
    if (
        not isinstance(audit_id, str)
        or path.parent != generation / "audit"
        or not path.is_file()
    ):
        raise _broken(generation, "lacks the audit event of its migration")
    return verified_audit(path)


def verify(ledger: LifecycleLedger, generation: Path, rules: str) -> JsonObject:
    """Check a migrated generation: every record and audit event as the ledger requires, every
    migration file as the manifest names it, and an audit event that records this manifest under
    the current rules.  Returns the manifest; any deviation is a ``LedgerError``."""
    ledger.validate_generation(generation)
    found = read_manifest(generation)
    if found is None:
        raise _broken(generation, "has no migration manifest")
    manifest, manifest_sha256 = found
    event = migration_event(generation, manifest)
    details = event.get("details")
    if (
        manifest["rules_sha256"] != rules
        or event.get("action") != SYSTEM_FIELDS_MIGRATION
        or not isinstance(details, dict)
        or details.get("manifest_sha256") != manifest_sha256
        or details.get("rules_sha256") != rules
    ):
        raise _broken(
            generation, "has an audit event that does not record its manifest"
        )
    return manifest


def current_manifest(ledger: LifecycleLedger, generation: str) -> JsonObject | None:
    """The manifest of the migration that produced ``generation`` when it is current for the
    shipped rules, else ``None``: a migrated generation needs no second migration."""
    path = ledger.root / GENERATIONS / generation
    found = read_manifest(path)
    if found is None:
        return None
    manifest, manifest_sha256 = found
    rules = rules_sha256(checked_registry())
    if manifest["rules_sha256"] != rules:
        return None
    event = migration_event(path, manifest)
    details = event.get("details")
    if (
        event.get("action") != SYSTEM_FIELDS_MIGRATION
        or not isinstance(details, dict)
        or details.get("manifest_sha256") != manifest_sha256
    ):
        raise _broken(path, "has an audit event that does not record its manifest")
    return manifest


def build(
    ledger: LifecycleLedger,
    source: str,
    staging: Path,
    *,
    source_pointer_sha256: str,
    audit_ids: tuple[str, str],
) -> JsonObject:
    """Build and check the migration of generation ``source`` in ``staging``; returns its
    manifest.  ``audit_ids`` name the ``generation-created`` and migration events.  A refusal
    removes ``staging`` and changes nothing else."""
    registry = checked_registry()
    rules = rules_sha256(registry)
    origin = ledger.root / GENERATIONS / source
    previous = read_manifest(origin)
    found = survey(origin)
    if unowned := found.unowned():
        raise LedgerError(
            f"the ledger migration cannot assign {unowned[0]} to a batch",
            remedy=REFUSAL_REMEDY,
        )
    quarantined = found.quarantined()
    carried = (
        [] if previous is None else cast(list[JsonObject], previous[0]["quarantine"])
    )
    for batch_id in quarantined:
        if BATCH_ID.fullmatch(batch_id) is None or batch_id in {
            entry["batch_id"] for entry in carried
        }:
            raise LedgerError(
                f"the ledger migration cannot quarantine {batch_id}: not a coordinator batch "
                "ID or already quarantined",
                remedy=REFUSAL_REMEDY,
            )
    staging.mkdir(parents=True)
    try:
        (staging / "audit").mkdir()
        ledger.append_generation_audit(
            staging,
            "generation-created",
            {"purpose": SYSTEM_FIELDS_MIGRATION},
            audit_id=audit_ids[0],
        )
        for directory in RECORD_DIRECTORIES:
            if (origin / directory).is_dir():
                shutil.copytree(
                    origin / directory, staging / directory, dirs_exist_ok=True
                )
            (staging / directory).mkdir(exist_ok=True)
        for entry in carried:
            relative = f"{MIGRATION_DIRECTORY}/{QUARANTINE}/{entry['batch_id']}"
            shutil.copytree(origin / relative, staging / relative)
        entries = carried + [
            _quarantine(staging, found, source, batch_id) for batch_id in quarantined
        ]
        manifest: JsonObject = {
            "contract": MANIFEST_CONTRACT,
            "registry_contract": CONTRACT_VERSION,
            "source_generation": source,
            "source_pointer_sha256": source_pointer_sha256,
            "rules_sha256": rules,
            "audit_id": audit_ids[1],
            "mapped": _mapped(found, quarantined),
            "quarantine": sorted(entries, key=lambda entry: str(entry["batch_id"])),
        }
        data = _canonical(manifest).encode("utf-8")
        (staging / MIGRATION_DIRECTORY).mkdir(exist_ok=True)
        (staging / MIGRATION_DIRECTORY / MANIFEST).write_bytes(data)
        ledger.append_generation_audit(
            staging,
            SYSTEM_FIELDS_MIGRATION,
            {
                "source_generation": source,
                "source_pointer_sha256": source_pointer_sha256,
                "rules_sha256": rules,
                "manifest_sha256": digest(data),
                "mapped": len(manifest["mapped"]),
                "quarantined": [entry["batch_id"] for entry in manifest["quarantine"]],
            },
            audit_id=audit_ids[1],
        )
        verify(ledger, staging, rules)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def _mapped(found: Survey, quarantined: list[str]) -> list[JsonObject]:
    """The mapped values of the records that stay operational, in path and location order."""
    return [
        {
            "path": path,
            "sha256": found.digests[path],
            "location": finding.location,
            "field_id": finding.field_id,
            "legacy_value": cast(str, finding.value),
            "canonical_value": finding.canonical,
            "disposition": MAPPED,
        }
        for path in sorted(found.findings)
        if found.owners.get(path) not in quarantined
        for finding in found.findings[path]
    ]


def _quarantine(staging: Path, found: Survey, source: str, batch_id: str) -> JsonObject:
    """Move the graph of ``batch_id`` into its quarantine, byte for byte, beside its evidence;
    returns its manifest entry."""
    base = staging / MIGRATION_DIRECTORY / QUARANTINE / batch_id
    files: dict[str, str] = {}
    for relative in found.graph(batch_id):
        target = base / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        (staging / relative).replace(target)
        files[relative] = found.digests[relative]
    evidence: JsonObject = {
        "contract": MANIFEST_CONTRACT,
        "batch_id": batch_id,
        "source_generation": source,
        "findings": [
            {
                "path": path,
                "sha256": found.digests[path],
                "location": finding.location,
                "field_id": finding.field_id,
                "value": finding.value,
                "disposition": finding.disposition,
                "reason": finding.reason,
            }
            for path in sorted(found.findings)
            if found.owners.get(path) == batch_id
            for finding in found.findings[path]
        ],
    }
    data = _canonical(evidence).encode("utf-8")
    (base / EVIDENCE).write_bytes(data)
    return {
        "batch_id": batch_id,
        "files": files,
        "evidence": {
            "path": f"{MIGRATION_DIRECTORY}/{QUARANTINE}/{batch_id}/{EVIDENCE}",
            "sha256": digest(data),
        },
    }


def discard(ledger: LifecycleLedger, name: str) -> None:
    """Remove the unselected staging or target directory ``name`` of an unfinished migration;
    the selected generation is never removed."""
    pointer = (
        _object(ledger.pointer_path.read_bytes())
        if ledger.pointer_path.is_file()
        else None
    )
    selected = None if pointer is None else pointer.get("generation")
    if name == selected or "/" in name or "\\" in name or name in ("", ".", ".."):
        raise LedgerError(
            f"refusing to remove the ledger generation {name!r}",
            remedy=INTERNAL_INVARIANT_REMEDY,
        )
    shutil.rmtree(ledger.root / GENERATIONS / name, ignore_errors=True)


def inspection(generation: Path) -> JsonObject:
    """A read-only view of ``generation`` that a bad record never stops: every batch with its
    area (``operational`` or ``quarantine``) and findings, the migration that produced the
    generation, and the files whose finding no batch owns.  Values are returned as found."""
    found = survey(generation)
    manifest: JsonObject | None = None
    problem: str | None = None
    try:
        if (recorded := read_manifest(generation)) is not None:
            manifest = recorded[0]
    except LedgerError as exc:
        problem = exc.message
    batches: dict[str, JsonObject] = {
        owner: {"batch_id": owner, "area": "operational", "findings": []}
        for owner in found.owners.values()
    }
    for path in sorted(found.findings):
        if (owner := found.owners.get(path)) is not None:
            batches[owner]["findings"] += [
                _shown(path, f) for f in found.findings[path]
            ]
    quarantine = generation / MIGRATION_DIRECTORY / QUARANTINE
    for directory in sorted(quarantine.iterdir() if quarantine.is_dir() else []):
        # Evidence is shown only once the manifest vouched for its bytes.
        evidence = (
            _object((directory / EVIDENCE).read_bytes())
            if manifest is not None and (directory / EVIDENCE).is_file()
            else None
        )
        batches[directory.name] = {
            "batch_id": directory.name,
            "area": QUARANTINE,
            "findings": [] if evidence is None else evidence.get("findings", []),
        }
    rules = rules_sha256(checked_registry())
    migration: JsonObject | None = None
    if manifest is not None:
        migration = {
            "source_generation": manifest["source_generation"],
            "rules_sha256": manifest["rules_sha256"],
            "current": manifest["rules_sha256"] == rules,
            "problem": None,
        }
    elif problem is not None:
        migration = {
            "source_generation": None,
            "rules_sha256": None,
            "current": False,
            "problem": problem,
        }
    return {
        "generation": generation.name,
        "rules_sha256": rules,
        "migration": migration,
        "unowned": found.unowned(),
        "batches": [batches[batch_id] for batch_id in sorted(batches)],
    }


def _shown(path: str, finding: Finding) -> JsonObject:
    return {
        "path": path,
        "location": finding.location,
        "field_id": finding.field_id,
        "value": finding.value,
        "disposition": finding.disposition,
        "reason": finding.reason,
    }


def export(generation: Path, batch_id: str) -> JsonObject | None:
    """Every file of ``batch_id`` in ``generation``, byte for byte: its quarantined graph and
    evidence, or its operational graph, and the reports of its dispatches.  ``None`` when the
    generation holds no such batch.  Nothing is parsed through the ledger and nothing written."""
    base = generation / MIGRATION_DIRECTORY / QUARANTINE / batch_id
    if base.is_dir() and not base.is_symlink():
        area = QUARANTINE
        files = [
            path.relative_to(generation).as_posix()
            for path in sorted(base.rglob("*"))
            if path.is_file() and not path.is_symlink()
        ]
        dispatches = [
            path.stem for path in sorted((base / "dispatches").glob("*.json"))
        ]
    else:
        found = survey(generation)
        files = sorted(
            path for path, owner in found.owners.items() if owner == batch_id
        )
        if not files:
            return None
        area = "operational"
        dispatches = [
            PurePosixPath(path).stem for path in files if path.startswith("dispatches/")
        ]
    for dispatch_id in dispatches:
        for suffix in (".json", ".md"):
            report = f"reports/{dispatch_id}{suffix}"
            path = generation / report
            if path.is_file() and not path.is_symlink() and report not in files:
                files.append(report)
    return {
        "batch_id": batch_id,
        "generation": generation.name,
        "area": area,
        "files": [_exported(generation, relative) for relative in files],
    }


def _exported(generation: Path, relative: str) -> JsonObject:
    data = (generation / relative).read_bytes()
    try:
        content, encoding = data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        content, encoding = base64.b64encode(data).decode("ascii"), "base64"
    return {
        "path": relative,
        "sha256": digest(data),
        "encoding": encoding,
        "content": content,
    }
