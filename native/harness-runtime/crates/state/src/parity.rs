//! The parity check (ADR 0020, section 5, step 5).
//!
//! It runs in Rust, without `serve` and without Python. For every file of the legacy state it
//! compares the SHA-256 of the canonical JSON of the legacy record with the SHA-256 of the
//! canonical JSON of the record that the imported rows project to; a byte-for-byte file
//! (an artifact, an audit record, a file of another generation) is compared by its bytes. The two
//! sets of paths must be equal, so a file that went missing, or a row that has no file, shows up
//! as well.
//!
//! A repaired record (ADR 0020, section 5, step 4) is the one allowed difference. It is accepted
//! only when the store holds a `migration-repair` audit record for the path whose `before_sha256`
//! is the hash of the legacy record and whose `after_sha256` is the hash of the row, and when the
//! legacy record and the row are equal outside the fields the named rule may change.

use std::collections::BTreeMap;
use std::path::Path;

use rusqlite::{Connection, OpenFlags};
use serde_json::{Map, Value};

use crate::canonical::{canonical_json, object_sha256, sha256_hex};
use crate::error::{Error, Result};
use crate::export::{Generation, ROLLBACK_ACTION};
use crate::import::manifest;
use crate::legacy::{LegacyState, Pointer, audit_record_is_valid, is_direct, parse_object};
use crate::project::SPECS;
use crate::repair::rule_fields;
use crate::route::{Target, classify};

/// The most differences the error text names.
const REPORTED: usize = 10;

/// One side of the comparison: the hash of a file and, for a JSON record, the record itself.
struct Entry {
    digest: String,
    record: Option<Map<String, Value>>,
}

impl Entry {
    fn record(record: Map<String, Value>) -> Self {
        Self {
            digest: object_sha256(&record),
            record: Some(record),
        }
    }

    fn bytes(bytes: &[u8]) -> Self {
        Self {
            digest: sha256_hex(bytes),
            record: None,
        }
    }
}

/// The entry of one file of a generation; `None` for an audit record, which is compared by the
/// manifest instead.
fn file_entry(path: &str, bytes: &[u8]) -> Option<Entry> {
    match classify(path, bytes) {
        Target::Audit => None,
        Target::Typed { record, .. }
        | Target::QaLease(record)
        | Target::QaQueue { record, .. }
        | Target::Record { record, .. } => Some(Entry::record(record)),
        Target::Artifact => Some(Entry::bytes(bytes)),
    }
}

/// The legacy side, keyed by the path relative to the state root.
fn legacy_side(legacy: &LegacyState) -> BTreeMap<String, Entry> {
    let selected = format!("generations/{}", legacy.pointer.generation);
    let mut found = BTreeMap::new();
    for file in &legacy.files {
        if let Some(entry) = file_entry(&file.path, &file.bytes) {
            found.insert(format!("{selected}/{}", file.path), entry);
        }
    }
    let audit = legacy.audit_files();
    for other in &legacy.other_generations {
        let path = format!("generations/{}/{}", other.generation, other.file.path);
        if audit
            .binary_search_by(|(known, _)| known.as_str().cmp(&path))
            .is_err()
        {
            found.insert(path, Entry::bytes(&other.file.bytes));
        }
    }
    for (path, bytes) in audit {
        found.insert(path, Entry::bytes(bytes));
    }
    found
}

fn rows<T>(
    connection: &Connection,
    sql: &str,
    mut map: impl FnMut(&rusqlite::Row<'_>) -> rusqlite::Result<T>,
) -> Result<Vec<T>> {
    let mut statement = connection.prepare(sql)?;
    let found = statement
        .query_map([], |row| map(row))?
        .collect::<rusqlite::Result<Vec<T>>>()?;
    Ok(found)
}

fn json_record(body: &str) -> rusqlite::Result<Map<String, Value>> {
    match serde_json::from_str::<Value>(body) {
        Ok(Value::Object(members)) => Ok(members),
        _ => Err(rusqlite::Error::InvalidColumnType(
            0,
            "body_json".to_owned(),
            rusqlite::types::Type::Text,
        )),
    }
}

/// What a row of the selected generation projects to.
enum Projected {
    /// A JSON record.
    Record(Map<String, Value>),
    /// A file kept byte for byte.
    Bytes(Vec<u8>),
}

/// Every row of the selected generation, keyed by the path, relative to the generation, that the
/// row projects to. The import compares it with the legacy files, and the rollback writes it.
fn projection(connection: &Connection) -> Result<Vec<(String, Projected)>> {
    let mut found = Vec::new();
    for spec in SPECS {
        for (id, record) in rows(connection, &spec.select_sql(), |row| spec.join(row))? {
            found.push((
                format!("{}/{id}.json", spec.directory),
                Projected::Record(record),
            ));
        }
    }
    for (body,) in rows(
        connection,
        "SELECT body_json FROM qa_lane WHERE owner_id IS NOT NULL",
        |row| Ok((row.get::<_, String>(0)?,)),
    )? {
        found.push((
            "qa-lane/lease.json".to_owned(),
            Projected::Record(json_record(&body)?),
        ));
    }
    for (id, body) in rows(
        connection,
        "SELECT id, body_json FROM qa_queue ORDER BY id",
        |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
    )? {
        found.push((
            format!("qa-lane/queue/{id}.json"),
            Projected::Record(json_record(&body)?),
        ));
    }
    for (path, body) in rows(
        connection,
        "SELECT path, body_json FROM record ORDER BY path",
        |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
    )? {
        found.push((path, Projected::Record(json_record(&body)?)));
    }
    for (path, bytes) in rows(
        connection,
        "SELECT path, bytes FROM artifact ORDER BY path",
        |row| Ok((row.get::<_, String>(0)?, row.get::<_, Vec<u8>>(1)?)),
    )? {
        found.push((path, Projected::Bytes(bytes)));
    }
    Ok(found)
}

impl From<Projected> for Entry {
    fn from(projected: Projected) -> Self {
        match projected {
            Projected::Record(record) => Self::record(record),
            Projected::Bytes(bytes) => Self::bytes(&bytes),
        }
    }
}

/// Every row of the store, keyed by the path the row projects to. The selected generation is
/// named `generation`.
fn store_side(connection: &Connection, generation: &str) -> Result<BTreeMap<String, Entry>> {
    let selected = format!("generations/{generation}");
    let mut found: BTreeMap<String, Entry> = projection(connection)?
        .into_iter()
        .map(|(path, projected)| (format!("{selected}/{path}"), projected.into()))
        .collect();
    for (path, bytes) in rows(connection, "SELECT path, bytes FROM legacy_audit", |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, Vec<u8>>(1)?))
    })? {
        found.insert(path, Entry::bytes(&bytes));
    }
    for (other, path, bytes) in rows(
        connection,
        "SELECT generation, path, bytes FROM legacy_generation",
        |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, Vec<u8>>(2)?,
            ))
        },
    )? {
        found.insert(format!("generations/{other}/{path}"), Entry::bytes(&bytes));
    }
    Ok(found)
}

/// The files of a generation of format v3 that holds the rows of the store: each path relative
/// to the generation, with the canonical JSON of a record or the bytes of an artifact.
///
/// # Errors
/// `Error::Interrupted` when the store cannot be read.
pub fn generation_files(connection: &Connection) -> Result<Vec<(String, Vec<u8>)>> {
    Ok(projection(connection)?
        .into_iter()
        .map(|(path, projected)| {
            let bytes = match projected {
                Projected::Record(record) => canonical_json(&Value::Object(record)).into_bytes(),
                Projected::Bytes(bytes) => bytes,
            };
            (path, bytes)
        })
        .collect())
}

/// The legacy audit records of the generation the store imported, as `audit/<name>` with their
/// bytes.
///
/// # Errors
/// `Error::Interrupted` when the store cannot be read.
pub fn imported_audit(connection: &Connection) -> Result<Vec<(String, Vec<u8>)>> {
    let imported: String = connection.query_row(
        "SELECT value FROM store_meta WHERE key = 'selected_generation'",
        [],
        |row| row.get(0),
    )?;
    let prefix = format!("generations/{imported}/");
    let mut found = Vec::new();
    for (path, bytes) in rows(
        connection,
        "SELECT path, bytes FROM legacy_audit ORDER BY path",
        |row| Ok((row.get::<_, String>(0)?, row.get::<_, Vec<u8>>(1)?)),
    )? {
        if let Some(relative) = path.strip_prefix(&prefix)
            && is_direct(relative, "audit")
        {
            found.push((relative.to_owned(), bytes));
        }
    }
    Ok(found)
}

/// A `migration-repair` audit record of the store.
struct RecordedRepair {
    rule: String,
    before_sha256: String,
    after_sha256: String,
}

fn recorded_repairs(connection: &Connection) -> Result<BTreeMap<String, RecordedRepair>> {
    let mut found = BTreeMap::new();
    for (details,) in rows(
        connection,
        "SELECT details_json FROM audit WHERE action = 'migration-repair' ORDER BY seq",
        |row| Ok((row.get::<_, String>(0)?,)),
    )? {
        let details = json_record(&details)?;
        let text = |key: &str| details.get(key).and_then(Value::as_str).map(str::to_owned);
        if let (Some(path), Some(rule), Some(before_sha256), Some(after_sha256)) = (
            text("path"),
            text("rule"),
            text("before_sha256"),
            text("after_sha256"),
        ) {
            found.insert(
                path,
                RecordedRepair {
                    rule,
                    before_sha256,
                    after_sha256,
                },
            );
        }
    }
    Ok(found)
}

/// Whether the difference between `legacy` and `row` is the repair the audit recorded for it.
fn is_recorded_repair(repair: Option<&RecordedRepair>, legacy: &Entry, row: &Entry) -> bool {
    let (Some(repair), Some(before), Some(after)) = (repair, &legacy.record, &row.record) else {
        return false;
    };
    let Some(fields) = rule_fields(&repair.rule) else {
        return false;
    };
    let outside = |record: &Map<String, Value>| {
        record
            .iter()
            .filter(|(key, _)| !fields.contains(&key.as_str()))
            .map(|(key, value)| (key.clone(), value.clone()))
            .collect::<BTreeMap<String, Value>>()
    };
    repair.before_sha256 == legacy.digest
        && repair.after_sha256 == row.digest
        && outside(before) == outside(after)
}

/// Compare the store at `db_path` with `legacy`.
///
/// # Errors
/// `Error::Invariant` names the paths that differ; an unreadable store is `Error::Interrupted`.
pub fn check(legacy: &LegacyState, db_path: &Path) -> Result<()> {
    let connection = Connection::open_with_flags(
        db_path,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    connection.pragma_update(None, "query_only", true)?;
    let mut differences = Vec::new();
    let expected = legacy_side(legacy);
    let actual = store_side(&connection, &legacy.pointer.generation)?;
    let repairs = recorded_repairs(&connection)?;
    for (path, entry) in &expected {
        match actual.get(path) {
            None => differences.push(format!("missing in the store: {path}")),
            Some(found)
                if found.digest != entry.digest
                    && !is_recorded_repair(repairs.get(path), entry, found) =>
            {
                differences.push(format!("differs: {path}"));
            }
            Some(_) => {}
        }
    }
    for path in actual.keys().filter(|path| !expected.contains_key(*path)) {
        differences.push(format!("not in the legacy state: {path}"));
    }
    let (_, manifest_sha256) = manifest(legacy);
    let stored: Option<String> = connection
        .query_row(
            "SELECT value FROM store_meta WHERE key = 'manifest_sha256'",
            [],
            |row| row.get(0),
        )
        .ok();
    if stored.as_deref() != Some(manifest_sha256.as_str()) {
        differences.push(
            "the manifest_sha256 of the store is not the manifest of the legacy audit".to_owned(),
        );
    }
    report(differences, "parity check")
}

fn report(mut differences: Vec<String>, what: &str) -> Result<()> {
    if differences.is_empty() {
        return Ok(());
    }
    let total = differences.len();
    differences.truncate(REPORTED);
    Err(Error::invariant(format!(
        "{what} failed, {total} difference(s): {}",
        differences.join("; ")
    )))
}

/// Compare the generation the rollback wrote into `state` with the store behind `connection`,
/// by the rule of the import: the canonical-JSON SHA-256 of every record, the bytes of every
/// artifact and of every legacy audit record of the imported generation, and equal sets of paths.
/// The `runtime-rollback` audit record of the generation is the only file the store does not hold.
///
/// # Errors
/// `Error::Invariant` names the paths that differ; an unreadable generation is `Error::Refused`
/// and an unreadable store `Error::Interrupted`.
pub fn check_export(connection: &Connection, state: &Path, generation: &Generation) -> Result<()> {
    let written = LegacyState::read(
        state,
        Pointer {
            version: crate::legacy::LEGACY_VERSION,
            generation: generation.name.clone(),
            selected_at: String::new(),
        },
    )?;
    let mut expected: BTreeMap<String, Entry> = projection(connection)?
        .into_iter()
        .map(|(path, projected)| (path, projected.into()))
        .collect();
    for (path, bytes) in imported_audit(connection)? {
        expected.insert(path, Entry::bytes(&bytes));
    }
    let mut differences = Vec::new();
    let mut rollback_record = false;
    for file in &written.files {
        if file.path == generation.audit_file {
            let record = parse_object(&file.bytes);
            rollback_record = audit_record_is_valid(&file.bytes)
                && record
                    .as_ref()
                    .and_then(|record| record.get("action"))
                    .and_then(Value::as_str)
                    == Some(ROLLBACK_ACTION);
            continue;
        }
        let actual =
            file_entry(&file.path, &file.bytes).unwrap_or_else(|| Entry::bytes(&file.bytes));
        match expected.remove(&file.path) {
            None => differences.push(format!("not in the store: {}", file.path)),
            Some(entry) if entry.digest != actual.digest => {
                differences.push(format!("differs: {}", file.path));
            }
            Some(_) => {}
        }
    }
    for path in expected.keys() {
        differences.push(format!("missing in the generation: {path}"));
    }
    if !rollback_record {
        differences.push(format!(
            "the {ROLLBACK_ACTION} audit record {} is missing or invalid",
            generation.audit_file
        ));
    }
    report(differences, "the parity check of the export")
}
