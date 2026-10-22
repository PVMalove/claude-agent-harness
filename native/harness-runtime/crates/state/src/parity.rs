//! The parity check (ADR 0020, section 5, step 5).
//!
//! It runs in Rust, without `serve` and without Python. For every file of the legacy state it
//! compares the SHA-256 of the canonical JSON of the legacy record with the SHA-256 of the
//! canonical JSON of the record that the imported rows project to; a byte-for-byte file
//! (an artifact, an audit record, a file of another generation) is compared by its bytes. The two
//! sets of paths must be equal, so a file that went missing, or a row that has no file, shows up
//! as well.

use std::collections::BTreeMap;
use std::path::Path;

use rusqlite::{Connection, OpenFlags};
use serde_json::{Map, Value};

use crate::canonical::{object_sha256, sha256_hex};
use crate::error::{Error, Result};
use crate::import::manifest;
use crate::legacy::LegacyState;
use crate::project::SPECS;
use crate::repair;
use crate::route::{Target, classify};

/// The most differences the error text names.
const REPORTED: usize = 10;

/// The SHA-256 of every legacy file, keyed by its path relative to the state root.
fn legacy_side(legacy: &LegacyState) -> BTreeMap<String, String> {
    let selected = format!("generations/{}", legacy.pointer.generation);
    let mut found = BTreeMap::new();
    for file in &legacy.files {
        let digest = match classify(&file.path, &file.bytes) {
            Target::Audit => continue,
            Target::Typed {
                spec, mut record, ..
            } => {
                // The store holds the repaired record, so the legacy record is repaired first.
                repair::apply(spec, "", &mut record);
                object_sha256(&record)
            }
            Target::QaLease(record)
            | Target::QaQueue { record, .. }
            | Target::Record { record, .. } => object_sha256(&record),
            Target::Artifact => sha256_hex(&file.bytes),
        };
        found.insert(format!("{selected}/{}", file.path), digest);
    }
    let audit = legacy.audit_files();
    for other in &legacy.other_generations {
        let path = format!("generations/{}/{}", other.generation, other.file.path);
        if audit
            .binary_search_by(|(known, _)| known.as_str().cmp(&path))
            .is_err()
        {
            found.insert(path, sha256_hex(&other.file.bytes));
        }
    }
    for (path, bytes) in audit {
        found.insert(path, sha256_hex(bytes));
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

/// The SHA-256 of every row of the store, keyed by the path the row projects to. The selected
/// generation is named `generation`.
fn store_side(connection: &Connection, generation: &str) -> Result<BTreeMap<String, String>> {
    let selected = format!("generations/{generation}");
    let mut found = BTreeMap::new();
    for spec in SPECS {
        for (id, record) in rows(connection, &spec.select_sql(), |row| spec.join(row))? {
            found.insert(
                format!("{selected}/{}/{id}.json", spec.directory),
                object_sha256(&record),
            );
        }
    }
    for (body,) in rows(connection, "SELECT body_json FROM qa_lane", |row| {
        Ok((row.get::<_, String>(0)?,))
    })? {
        found.insert(
            format!("{selected}/qa-lane/lease.json"),
            object_sha256(&json_record(&body)?),
        );
    }
    for (id, body) in rows(connection, "SELECT id, body_json FROM qa_queue", |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })? {
        found.insert(
            format!("{selected}/qa-lane/queue/{id}.json"),
            object_sha256(&json_record(&body)?),
        );
    }
    for (path, body) in rows(connection, "SELECT path, body_json FROM record", |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })? {
        found.insert(
            format!("{selected}/{path}"),
            object_sha256(&json_record(&body)?),
        );
    }
    for (path, bytes) in rows(connection, "SELECT path, bytes FROM artifact", |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, Vec<u8>>(1)?))
    })? {
        found.insert(format!("{selected}/{path}"), sha256_hex(&bytes));
    }
    for (path, bytes) in rows(connection, "SELECT path, bytes FROM legacy_audit", |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, Vec<u8>>(1)?))
    })? {
        found.insert(path, sha256_hex(&bytes));
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
        found.insert(format!("generations/{other}/{path}"), sha256_hex(&bytes));
    }
    Ok(found)
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
    for (path, digest) in &expected {
        match actual.get(path) {
            None => differences.push(format!("missing in the store: {path}")),
            Some(found) if found != digest => differences.push(format!("differs: {path}")),
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
    if differences.is_empty() {
        return Ok(());
    }
    let total = differences.len();
    differences.truncate(REPORTED);
    Err(Error::invariant(format!(
        "parity check failed, {total} difference(s): {}",
        differences.join("; ")
    )))
}
