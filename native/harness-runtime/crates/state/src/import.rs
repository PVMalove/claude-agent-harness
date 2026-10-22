//! The import of the legacy state into a new store file (ADR 0020, section 5, step 4).
//!
//! The whole import is one transaction in a store that is not yet the authority
//! (`runtime.db.importing`). Nothing of the legacy state is changed: every file is read, and
//! every file goes to exactly one place (`route::classify`), so the parity check can prove that
//! nothing was lost.

use std::path::Path;

use rusqlite::{Connection, params};
use serde_json::{Value, json};

use crate::audit;
use crate::canonical::{canonical_json, sha256_hex};
use crate::error::Result;
use crate::legacy::{LegacyState, audit_record_is_valid};
use crate::repair::{self, Repair};
use crate::route::{Target, classify};
use crate::schema;

/// What the import knows once it is done.
#[derive(Debug)]
pub struct Imported {
    pub store_id: String,
    /// The repairs applied to the imported records, in path order.
    pub repairs: Vec<Repair>,
    /// The canonical bytes of the import manifest (`import-manifest.json` next to the snapshot).
    pub manifest_json: String,
    pub manifest_sha256: String,
}

/// The sorted `(path, sha256)` list of every legacy audit record, as ADR 0020, section 4 defines.
pub fn manifest_entries(legacy: &LegacyState) -> Vec<(String, String)> {
    legacy
        .audit_files()
        .into_iter()
        .map(|(path, bytes)| (path, sha256_hex(bytes)))
        .collect()
}

/// The canonical manifest file and its SHA-256.
pub fn manifest(legacy: &LegacyState) -> (String, String) {
    let entries: Vec<Value> = manifest_entries(legacy)
        .into_iter()
        .map(|(path, sha256)| json!({"path": path, "sha256": sha256}))
        .collect();
    let text = canonical_json(&json!({"entries": entries}));
    let checksum = sha256_hex(text.as_bytes());
    (text, checksum)
}

fn insert_typed(
    connection: &Connection,
    target: Target,
    path: &str,
    repairs: &mut Vec<Repair>,
) -> Result<()> {
    match target {
        Target::Typed {
            spec,
            id,
            mut record,
        } => {
            repairs.extend(repair::apply(spec, path, &mut record));
            let params = spec.split(&id, record);
            connection.execute(&spec.insert_sql(), rusqlite::params_from_iter(params))?;
        }
        Target::QaLease(record) => {
            connection.execute(
                "INSERT INTO qa_lane (id, body_json) VALUES (1, ?1)",
                params![Value::Object(record).to_string()],
            )?;
        }
        Target::QaQueue { id, record } => {
            connection.execute(
                "INSERT INTO qa_queue (id, body_json) VALUES (?1, ?2)",
                params![id, Value::Object(record).to_string()],
            )?;
        }
        Target::Audit | Target::Artifact | Target::Record { .. } => {}
    }
    Ok(())
}

fn insert_files(connection: &Connection, legacy: &LegacyState) -> Result<Vec<Repair>> {
    let selected = format!("generations/{}", legacy.pointer.generation);
    let mut repairs = Vec::new();
    for file in &legacy.files {
        match classify(&file.path, &file.bytes) {
            Target::Audit => {}
            Target::Record { collection, record } => {
                connection.execute(
                    "INSERT INTO record (path, collection, body_json) VALUES (?1, ?2, ?3)",
                    params![file.path, collection, Value::Object(record).to_string()],
                )?;
            }
            Target::Artifact => {
                connection.execute(
                    "INSERT INTO artifact (path, bytes, sha256) VALUES (?1, ?2, ?3)",
                    params![file.path, file.bytes, sha256_hex(&file.bytes)],
                )?;
            }
            typed => insert_typed(
                connection,
                typed,
                &format!("{selected}/{}", file.path),
                &mut repairs,
            )?,
        }
    }
    Ok(repairs)
}

fn insert_other_generations(connection: &Connection, legacy: &LegacyState) -> Result<()> {
    let audit_paths: std::collections::BTreeSet<String> = legacy
        .audit_files()
        .into_iter()
        .map(|(path, _)| path)
        .collect();
    for other in &legacy.other_generations {
        let full = format!("generations/{}/{}", other.generation, other.file.path);
        if audit_paths.contains(&full) {
            continue;
        }
        connection.execute(
            "INSERT INTO legacy_generation (generation, path, bytes, sha256) VALUES (?1, ?2, ?3, ?4)",
            params![
                other.generation,
                other.file.path,
                other.file.bytes,
                sha256_hex(&other.file.bytes)
            ],
        )?;
    }
    Ok(())
}

fn insert_audit(connection: &Connection, legacy: &LegacyState) -> Result<()> {
    for (path, bytes) in legacy.audit_files() {
        connection.execute(
            "INSERT INTO legacy_audit (path, bytes, sha256, record_sha256_valid) VALUES (?1, ?2, ?3, ?4)",
            params![
                path,
                bytes,
                sha256_hex(bytes),
                i64::from(audit_record_is_valid(bytes))
            ],
        )?;
    }
    for (path, sha256) in manifest_entries(legacy) {
        connection.execute(
            "INSERT INTO import_manifest (path, sha256) VALUES (?1, ?2)",
            params![path, sha256],
        )?;
    }
    Ok(())
}

/// Import `legacy` into a new store file at `db_path`. The file must not exist. The store is left
/// sealed in WAL mode and closed; the caller checks parity and moves it into place.
pub fn import(legacy: &LegacyState, db_path: &Path, store_id: &str, at: &str) -> Result<Imported> {
    let mut connection = schema::create(db_path)?;
    let (manifest_json, manifest_sha256) = manifest(legacy);
    let imported_repairs;
    {
        let transaction = connection.transaction()?;
        // The files come in path order, so a status can precede its dispatch: the foreign keys
        // are checked when the transaction commits.
        transaction.pragma_update(None, "defer_foreign_keys", true)?;
        for (key, value) in [
            ("store_id", store_id),
            ("selected_generation", legacy.pointer.generation.as_str()),
            ("manifest_sha256", manifest_sha256.as_str()),
        ] {
            transaction.execute(
                "INSERT INTO store_meta (key, value) VALUES (?1, ?2)",
                params![key, value],
            )?;
        }
        let repairs = insert_files(&transaction, legacy)?;
        insert_other_generations(&transaction, legacy)?;
        insert_audit(&transaction, legacy)?;
        audit::append(
            &transaction,
            at,
            "legacy-import",
            &json!({
                "generation": legacy.pointer.generation,
                "manifest_sha256": manifest_sha256,
                "files": manifest_entries(legacy).len(),
            }),
        )?;
        for repair in &repairs {
            audit::append(
                &transaction,
                at,
                "migration-repair",
                &json!({
                    "path": repair.path,
                    "rule": repair.rule,
                    "before_sha256": repair.before_sha256,
                    "after_sha256": repair.after_sha256,
                }),
            )?;
        }
        transaction.commit()?;
        imported_repairs = repairs;
    }
    schema::seal(connection)?;
    Ok(Imported {
        repairs: imported_repairs,
        store_id: store_id.to_owned(),
        manifest_json,
        manifest_sha256,
    })
}
