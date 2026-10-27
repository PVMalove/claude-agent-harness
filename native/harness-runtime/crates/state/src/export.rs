//! The export of the store for `rollback --to-legacy` (ADR 0020, section 5).
//!
//! `write_generation` writes a new legacy generation of format v3 from the rows of the store: every
//! record directory (empty ones too), the typed rows projected back to their records, the free
//! records, the artifact bytes, a byte copy of the legacy audit of the imported generation, and one
//! v3 audit record `runtime-rollback`, written as the Python ledger writes an audit record. The
//! Rust runtime is the only writer of this format in 2.0.0 (ADR 0020, section 1).
//!
//! `archive` collects what format v3 cannot hold (the audit chain of the store, the operations,
//! the revisions and the states of the executions) into a deterministic ustar archive, which
//! `write_archive` installs once as `state/runtime-export/<store_id>.tar`.

use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

use rusqlite::Connection;
use serde_json::{Map, Value, json};

use crate::archive::{self, Member};
use crate::canonical::{canonical_json, sha256_hex};
use crate::error::{Error, Result};
use crate::legacy::{GENERATIONS, RECORD_DIRECTORIES};
use crate::parity::{generation_files, imported_audit};
use crate::platform;

/// The directory of the archives, relative to the state root.
pub const EXPORT_DIRECTORY: &str = "runtime-export";
/// The audit action of the rollback, in the v3 generation and in the audit chain of the store.
pub const ROLLBACK_ACTION: &str = "runtime-rollback";

/// A generation the export wrote.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Generation {
    /// `generation-<hex>`.
    pub name: String,
    /// The `runtime-rollback` audit record, relative to the generation.
    pub audit_file: String,
}

/// An installed archive.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Export {
    /// Relative to the state root: `runtime-export/<store_id>.tar`.
    pub path: String,
    pub sha256: String,
}

/// A new generation directory under `generations/`, named as the Python ledger names one.
fn new_generation(state: &Path) -> Result<(String, PathBuf)> {
    let generations = state.join(GENERATIONS);
    fs::create_dir_all(&generations)?;
    loop {
        let name = format!("generation-{}", &platform::random_hex()?[..8]);
        let path = generations.join(&name);
        match fs::create_dir(&path) {
            Ok(()) => return Ok((name, path)),
            Err(err) if err.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(err) => return Err(err.into()),
        }
    }
}

/// The v3 audit record `runtime-rollback`, hashed like `_append_audit` of the Python ledger.
fn audit_record(at: &str, details: &Value) -> Result<(String, String)> {
    let audit_id = format!("audit-{}", platform::uuid4()?);
    let mut record = json!({
        "audit_id": audit_id,
        "at": at,
        "action": ROLLBACK_ACTION,
        "details": details,
    });
    record["record_sha256"] = Value::String(sha256_hex(canonical_json(&record).as_bytes()));
    Ok((format!("audit/{audit_id}.json"), canonical_json(&record)))
}

/// Write the files, then flush every directory they are in.
fn write_files(root: &Path, files: &[(String, Vec<u8>)]) -> Result<()> {
    let mut directories: BTreeSet<PathBuf> = BTreeSet::new();
    for (path, bytes) in files {
        let target = root.join(path);
        let parent = target.parent().unwrap_or(root).to_path_buf();
        fs::create_dir_all(&parent)?;
        platform::write_durable(&target, bytes)?;
        directories.insert(parent);
    }
    for directory in directories {
        platform::sync_dir(&directory)?;
    }
    Ok(())
}

/// Write a new generation of format v3 from the store behind `connection` into `state`, with
/// the `runtime-rollback` audit record of `details` at `at`. A failure removes what it wrote.
///
/// # Errors
/// `Error::Interrupted` when the store cannot be read or a file cannot be written.
pub fn write_generation(
    connection: &Connection,
    state: &Path,
    at: &str,
    details: &Value,
) -> Result<Generation> {
    let mut files: Vec<(String, Vec<u8>)> = generation_files(connection)?;
    files.extend(imported_audit(connection)?);
    let (audit_file, audit_text) = audit_record(at, details)?;
    files.push((audit_file.clone(), audit_text.into_bytes()));
    let (name, root) = new_generation(state)?;
    let written = (|| -> Result<()> {
        for directory in RECORD_DIRECTORIES {
            fs::create_dir_all(root.join(directory))?;
        }
        write_files(&root, &files)?;
        platform::sync_dir(&root)?;
        platform::sync_dir(&state.join(GENERATIONS))?;
        Ok(())
    })();
    if let Err(err) = written {
        let _ = fs::remove_dir_all(&root);
        return Err(err);
    }
    Ok(Generation { name, audit_file })
}

fn json_lines(rows: Vec<Value>) -> Vec<u8> {
    let mut out = String::new();
    for row in rows {
        out.push_str(&row.to_string());
        out.push('\n');
    }
    out.into_bytes()
}

fn query(
    connection: &Connection,
    sql: &str,
    map: impl FnMut(&rusqlite::Row<'_>) -> rusqlite::Result<Value>,
) -> Result<Vec<Value>> {
    let mut statement = connection.prepare(sql)?;
    let rows = statement
        .query_map([], map)?
        .collect::<rusqlite::Result<Vec<Value>>>()?;
    Ok(rows)
}

fn store_meta(connection: &Connection) -> Result<Vec<u8>> {
    let version: i64 = connection.pragma_query_value(None, "user_version", |row| row.get(0))?;
    let mut meta = Map::new();
    for row in query(
        connection,
        "SELECT key, value FROM store_meta ORDER BY key",
        |row| Ok(json!([row.get::<_, String>(0)?, row.get::<_, String>(1)?])),
    )? {
        meta.insert(
            row[0].as_str().unwrap_or_default().to_owned(),
            row[1].clone(),
        );
    }
    Ok(canonical_json(&json!({"store_meta": meta, "user_version": version})).into_bytes())
}

fn revisions(connection: &Connection) -> Result<Vec<u8>> {
    let mut rows = Vec::new();
    for (directory, table, execution) in [
        ("batches", "batch", "NULL"),
        ("dispatches", "dispatch", "NULL"),
        ("dispatch-status", "dispatch_status", "execution_state"),
    ] {
        rows.extend(query(
            connection,
            &format!("SELECT id, revision, {execution} FROM {table} ORDER BY id"),
            |row| {
                Ok(json!({
                    "path": format!("{directory}/{}.json", row.get::<_, String>(0)?),
                    "revision": row.get::<_, i64>(1)?,
                    "execution_state": row.get::<_, Option<String>>(2)?,
                }))
            },
        )?);
    }
    rows.extend(query(
        connection,
        "SELECT id, revision FROM qa_queue ORDER BY id",
        |row| {
            Ok(json!({
                "path": format!("qa-lane/queue/{}.json", row.get::<_, String>(0)?),
                "revision": row.get::<_, i64>(1)?,
                "execution_state": null,
            }))
        },
    )?);
    rows.extend(query(
        connection,
        "SELECT path, revision FROM record ORDER BY path",
        |row| {
            Ok(json!({
                "path": row.get::<_, String>(0)?,
                "revision": row.get::<_, i64>(1)?,
                "execution_state": null,
            }))
        },
    )?);
    Ok(json_lines(rows))
}

/// The members of the archive of the store behind `connection`, without the manifest.
fn members(connection: &Connection) -> Result<Vec<Member>> {
    let audit = query(
        connection,
        "SELECT seq, at, action, details_json, record_sha256, prev_chain_sha256, chain_sha256
         FROM audit ORDER BY seq",
        |row| {
            Ok(json!({
                "seq": row.get::<_, i64>(0)?,
                "at": row.get::<_, String>(1)?,
                "action": row.get::<_, String>(2)?,
                "details_json": row.get::<_, String>(3)?,
                "record_sha256": row.get::<_, String>(4)?,
                "prev_chain_sha256": row.get::<_, String>(5)?,
                "chain_sha256": row.get::<_, String>(6)?,
            }))
        },
    )?;
    let operations = query(
        connection,
        "SELECT operation_id, operation, request_digest, state, result_json, created_at,
                completed_at
         FROM operations ORDER BY operation_id",
        |row| {
            Ok(json!({
                "operation_id": row.get::<_, String>(0)?,
                "operation": row.get::<_, String>(1)?,
                "request_digest": row.get::<_, String>(2)?,
                "state": row.get::<_, String>(3)?,
                "result_json": row.get::<_, Option<String>>(4)?,
                "created_at": row.get::<_, String>(5)?,
                "completed_at": row.get::<_, Option<String>>(6)?,
            }))
        },
    )?;
    let mut found = vec![
        Member {
            name: "store_meta.json".to_owned(),
            bytes: store_meta(connection)?,
        },
        Member {
            name: "audit.jsonl".to_owned(),
            bytes: json_lines(audit),
        },
        Member {
            name: "operations.jsonl".to_owned(),
            bytes: json_lines(operations),
        },
        Member {
            name: "revisions.jsonl".to_owned(),
            bytes: revisions(connection)?,
        },
    ];
    let lane = query(
        connection,
        "SELECT revision, fencing_token, body_json FROM qa_lane",
        |row| {
            Ok(json!({
                "revision": row.get::<_, i64>(0)?,
                "fencing_token": row.get::<_, i64>(1)?,
                "body_json": row.get::<_, String>(2)?,
            }))
        },
    )?;
    if let Some(lane) = lane.first() {
        found.push(Member {
            name: "qa_lane.json".to_owned(),
            bytes: canonical_json(lane).into_bytes(),
        });
    }
    Ok(found)
}

/// The archive of what format v3 cannot hold: `store_meta.json`, `audit.jsonl` (the whole audit
/// chain of the store), `operations.jsonl`, `revisions.jsonl`, `qa_lane.json` when the lane has
/// a row, and `MANIFEST.json` with the SHA-256 of each of them. The same store gives the same
/// bytes.
///
/// # Errors
/// `Error::Interrupted` when the store cannot be read.
pub fn archive(connection: &Connection) -> Result<Vec<u8>> {
    let mut found = members(connection)?;
    found.sort_by(|a, b| a.name.cmp(&b.name));
    let manifest: Vec<Value> = found
        .iter()
        .map(|member| json!({"name": member.name, "sha256": sha256_hex(&member.bytes)}))
        .collect();
    found.push(Member {
        name: "MANIFEST.json".to_owned(),
        bytes: canonical_json(&json!({"members": manifest})).into_bytes(),
    });
    Ok(archive::ustar(&found))
}

/// Install the archive `bytes` of the store `store_id` as `state/runtime-export/<store_id>.tar`.
///
/// # Errors
/// `Error::Invariant` when that file already exists with other bytes.
pub fn write_archive(state: &Path, store_id: &str, bytes: &[u8]) -> Result<Export> {
    if store_id.is_empty() || store_id.contains(['/', '\\']) || store_id.starts_with('.') {
        return Err(Error::invariant(format!(
            "the store_id {store_id:?} cannot name an archive file"
        )));
    }
    let path = format!("{EXPORT_DIRECTORY}/{store_id}.tar");
    archive::install(&state.join(&path), bytes)?;
    Ok(Export {
        path,
        sha256: sha256_hex(bytes),
    })
}
