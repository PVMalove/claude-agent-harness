"""The strict read boundary of the ledger's machine fields.

An automatic decision reads a generation only when every value at a location bound to a
``systemFields.v1`` field is canonical, or the current migration of that generation vouches for it
as a mapped historical value of the very file that holds it.  Anything else stops the read before
the decision: unmigrated history raises ``LedgerUnmigrated``, whose message and remedy name
``ledger migrate``; a value that appeared after the migration, a changed manifest, quarantine or
migration event raises ``LedgerError``.  The boundary writes nothing, normalizes nothing and
substitutes no value: ``ledger inspect``, ``ledger export`` and ``ledger migrate`` read the
generation without it.

A generation without a finding is canonical without a manifest: a generation that ``ensure`` or
``reset`` created, and every one written only through the ledger's write boundary, has none.
"""

from __future__ import annotations

from pathlib import Path

from harness.json_types import JsonObject
from harness.orchestration.ledger.field_migration import (
    UNREADABLE,
    Finding,
    checked_registry,
    rules_sha256,
)
from harness.orchestration.ledger.generation_migration import (
    Survey,
    migration_event,
    read_manifest,
    survey,
)
from harness.orchestration.ledger.lifecycle import (
    LEDGER_CLEAN,
    LEDGER_EXPORT,
    LEDGER_INSPECT,
    LEDGER_MIGRATE,
    LEDGER_MIGRATE_ROUTE,
    MIGRATION_DIRECTORY,
    SYSTEM_FIELDS_MIGRATION,
    LedgerError,
    LedgerUnmigrated,
)

# A shown value keeps a refusal short enough that its route survives a sanitized diagnostic.
_SHOWN_VALUE = 60


def unvouched(found: Survey, manifest: JsonObject | None) -> list[tuple[str, Finding]]:
    """Every finding of ``found`` that ``manifest`` does not vouch for, in path order.  Only a
    mapped entry that names exactly the file's digest, the location, the field and both values
    vouches for a finding; ``None`` vouches for nothing."""
    mapped = [] if manifest is None else manifest["mapped"]
    entries = mapped if isinstance(mapped, list) else []
    return [
        (path, finding)
        for path in sorted(found.findings)
        for finding in found.findings[path]
        if _entry(found, path, finding) not in entries
    ]


def _entry(found: Survey, path: str, finding: Finding) -> JsonObject:
    """The manifest entry that would vouch for ``finding`` of ``path``."""
    return {
        "path": path,
        "sha256": found.digests[path],
        "location": finding.location,
        "field_id": finding.field_id,
        "legacy_value": finding.value,
        "canonical_value": finding.canonical,
        "disposition": finding.disposition,
    }


def require_canonical(generation: Path) -> None:
    """Refuse ``generation`` unless each value at a bound location is canonical or vouched for
    by its current migration; read-only, so a refusal changes nothing."""
    migration = _migration(generation)
    found = survey(generation)
    current = migration is not None and migration[1]
    left = unvouched(found, migration[0] if migration is not None and current else None)
    if not left:
        return
    path, finding = left[0]
    shown = _shown(path, finding)
    if current:
        raise LedgerError(
            f"ledger generation {generation.name} holds a value its migration does not vouch "
            f"for: {shown}",
            remedy=f"read the record with {LEDGER_INSPECT} and {LEDGER_EXPORT}, then "
            "restore it from the source generation the migration manifest names; the ledger "
            "rewrites nothing",
        )
    why = (
        f"the migration rules changed since generation {generation.name} was migrated"
        if migration is not None
        else f"generation {generation.name} holds history no migration vouches for"
    )
    clean = (
        ""
        if path in found.owners
        else f", {LEDGER_CLEAN} to remove the dispatch evidence no batch owns"
    )
    raise LedgerUnmigrated(
        f"{LEDGER_MIGRATE_ROUTE}: {why}: {shown}",
        remedy=f"run {LEDGER_INSPECT} to see the value{clean}, then {LEDGER_MIGRATE}",
    )


def _migration(generation: Path) -> tuple[JsonObject, bool] | None:
    """The verified manifest of the migration that produced ``generation`` and whether it is
    current for the shipped rules; ``None`` when no migration produced it.  A changed manifest,
    quarantine or migration event is a ``LedgerError``."""
    found = read_manifest(generation)
    if found is None:
        return None
    manifest, manifest_sha256 = found
    event = migration_event(generation, manifest)
    details = event.get("details")
    if (
        event.get("action") != SYSTEM_FIELDS_MIGRATION
        or not isinstance(details, dict)
        or details.get("manifest_sha256") != manifest_sha256
        or details.get("rules_sha256") != manifest["rules_sha256"]
    ):
        raise LedgerError(
            f"ledger generation {generation.name} has a migration event that does not record "
            "its manifest",
            remedy=f"restore {generation / MIGRATION_DIRECTORY} from the source generation the "
            f"manifest names, or select that source generation again; read it with {LEDGER_INSPECT}",
        )
    return manifest, manifest["rules_sha256"] == rules_sha256(checked_registry())


def _shown(path: str, finding: Finding) -> str:
    """The first finding as a refusal names it: its file, location and a bounded value."""
    if finding.disposition == UNREADABLE:
        return f"{path} ({finding.reason})"
    value = repr(finding.value)
    if len(value) > _SHOWN_VALUE:
        value = value[: _SHOWN_VALUE - 3] + "..."
    return f"{path} {finding.location} = {value} ({finding.disposition})"
