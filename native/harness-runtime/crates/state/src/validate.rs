//! The verification of the store: what `state.validate` of the IPC answers and `coordinator.py
//! ledger validate [--full]` shows (ADR 0020, section 4).
//!
//! An ordinary check verifies the audit records after the checkpoint, as every command does. The
//! full check starts from nothing: it recomputes the whole audit chain from its genesis and
//! compares it with the import anchor, recomputes the manifest of the legacy audit from its rows,
//! checks every legacy audit record and generation byte against its hash, runs SQLite's own
//! integrity and foreign-key checks, and compares every row that a command wrote with the last
//! audit record of its path. It detects a deleted, an inserted and a reordered record, and a row
//! that changed outside the runtime. It never uses the checkpoint, and a failure writes nothing.
//! A success rebuilds the checkpoint in a short transaction, so a deleted or damaged checkpoint
//! is no error.

use std::collections::BTreeMap;
use std::path::Path;

use contracts::{ErrorBody, ErrorCode, FailureDomain};
use rusqlite::{Connection, OpenFlags, OptionalExtension};
use serde_json::{Value, json};

use crate::audit::{self, Broken, Verified};
use crate::authority::{Authority, Epoch, STORE_NAME, epoch};
use crate::canonical::sha256_hex;
use crate::import::manifest_of;
use crate::write::row_sha;

fn broken(check: &'static str, detail: impl Into<String>) -> Broken {
    Broken::new(None, check, detail)
}

fn not_attached(reason: &str) -> ErrorBody {
    ErrorBody::new(
        ErrorCode::NotFound,
        FailureDomain::Policy,
        false,
        format!("the runtime store is not the authority of this repository: {reason}"),
        "run `harness-runtime migrate --repo <repo>`, or use the installation that matches the state",
    )
}

fn meta(connection: &Connection, key: &str) -> Result<Option<String>, Broken> {
    Ok(connection
        .query_row(
            "SELECT value FROM store_meta WHERE key = ?1",
            [key],
            |row| row.get(0),
        )
        .optional()?)
}

/// The `legacy-import` record that anchors the chain: its details.
fn anchor_details(connection: &Connection) -> Result<Value, Broken> {
    let text: Option<String> = connection
        .query_row(
            "SELECT details_json FROM audit WHERE seq = 1 AND action = 'legacy-import'",
            [],
            |row| row.get(0),
        )
        .optional()?;
    text.and_then(|text| serde_json::from_str(&text).ok())
        .ok_or_else(|| {
            broken(
                "audit_anchor",
                "the chain has no legacy-import record at seq 1",
            )
        })
}

fn check_manifest(connection: &Connection, authority: &Authority) -> Result<(), Broken> {
    let anchor = anchor_details(connection)?;
    let anchored = anchor["manifest_sha256"].as_str().unwrap_or_default();
    let stored = meta(connection, "manifest_sha256")?.unwrap_or_default();
    if anchored != stored || anchored != authority.manifest_sha256 {
        return Err(broken(
            "import_anchor",
            "the manifest of the legacy-import record, of store_meta and of AUTHORITY.json differ",
        ));
    }
    let mut statement =
        connection.prepare("SELECT path, sha256 FROM import_manifest ORDER BY path")?;
    let entries: Vec<(String, String)> = statement
        .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?
        .collect::<Result<_, _>>()?;
    if manifest_of(&entries).1 != anchored {
        return Err(broken(
            "import_manifest",
            "the rows of import_manifest are not the manifest the import anchored",
        ));
    }
    Ok(())
}

fn check_legacy(connection: &Connection) -> Result<(), Broken> {
    let mut manifest: BTreeMap<String, String> = BTreeMap::new();
    let mut statement = connection.prepare("SELECT path, sha256 FROM import_manifest")?;
    for row in statement.query_map([], |row| Ok((row.get(0)?, row.get(1)?)))? {
        let (path, sha256) = row?;
        manifest.insert(path, sha256);
    }
    let mut statement = connection.prepare("SELECT path, bytes, sha256 FROM legacy_audit")?;
    let mut rows = statement.query([])?;
    let mut seen = 0;
    while let Some(row) = rows.next()? {
        let path: String = row.get(0)?;
        let bytes: Vec<u8> = row.get(1)?;
        let column: String = row.get(2)?;
        let actual = sha256_hex(&bytes);
        if actual != column || manifest.get(&path) != Some(&actual) {
            return Err(broken(
                "legacy_audit",
                format!("the legacy audit record {path} is not the one the import stored"),
            ));
        }
        seen += 1;
    }
    if seen != manifest.len() {
        return Err(broken(
            "legacy_audit",
            "legacy_audit and import_manifest hold different records",
        ));
    }
    let mut statement =
        connection.prepare("SELECT generation, path, bytes, sha256 FROM legacy_generation")?;
    let mut rows = statement.query([])?;
    while let Some(row) = rows.next()? {
        let (generation, path): (String, String) = (row.get(0)?, row.get(1)?);
        let bytes: Vec<u8> = row.get(2)?;
        let column: String = row.get(3)?;
        if sha256_hex(&bytes) != column {
            return Err(broken(
                "legacy_generation",
                format!(
                    "the file {path} of the generation {generation} is not the one the import stored"
                ),
            ));
        }
    }
    Ok(())
}

fn check_sqlite(connection: &Connection) -> Result<(), Broken> {
    let mut statement = connection.prepare("PRAGMA integrity_check")?;
    let answers: Vec<String> = statement
        .query_map([], |row| row.get(0))?
        .collect::<Result<_, _>>()?;
    if answers != ["ok"] {
        return Err(broken(
            "integrity_check",
            answers.into_iter().take(3).collect::<Vec<_>>().join("; "),
        ));
    }
    let mut statement = connection.prepare("PRAGMA foreign_key_check")?;
    let mut rows = statement.query([])?;
    if let Some(row) = rows.next()? {
        let table: String = row.get(0)?;
        return Err(broken(
            "foreign_key_check",
            format!("a row of {table} names a row that does not exist"),
        ));
    }
    Ok(())
}

/// The path, the hash before and the hash after of an audit record that names a row.
struct Touch {
    seq: i64,
    action: String,
    path: String,
    before: Option<String>,
    after: Option<String>,
}

fn touches(connection: &Connection) -> Result<Vec<Touch>, Broken> {
    let mut statement = connection
        .prepare("SELECT seq, action, details_json FROM audit WHERE seq > 1 ORDER BY seq")?;
    let mut rows = statement.query([])?;
    let mut found = Vec::new();
    while let Some(row) = rows.next()? {
        let seq: i64 = row.get(0)?;
        let action: String = row.get(1)?;
        let details: Value = serde_json::from_str(&row.get::<_, String>(2)?).unwrap_or(Value::Null);
        let Some(path) = details["path"].as_str() else {
            continue;
        };
        let text = |key: &str| details[key].as_str().map(str::to_owned);
        let (before, after) = match action.as_str() {
            "transition" => (text("before_sha256"), text("sha256")),
            "deletion" => (text("sha256"), None),
            _ => (None, text("sha256")),
        };
        found.push(Touch {
            seq,
            action,
            path: path.to_owned(),
            before,
            after,
        });
    }
    Ok(found)
}

/// Every row a command wrote is what its last audit record says, and the records of one path link:
/// each transition starts from the hash the record before it ended with.
fn check_rows(connection: &Connection) -> Result<(), Broken> {
    let mut last: BTreeMap<String, (i64, Option<String>)> = BTreeMap::new();
    for touch in touches(connection)? {
        if let Some((previous_seq, previous)) = last.get(&touch.path) {
            let expected = match touch.action.as_str() {
                "transition" | "deletion" => touch.before.as_ref(),
                _ => None,
            };
            if let Some(expected) = expected
                && previous.as_ref() != Some(expected)
            {
                return Err(Broken::new(
                    Some(touch.seq),
                    "audit_link",
                    format!(
                        "{} does not continue the record {previous_seq} of {}",
                        touch.action, touch.path
                    ),
                ));
            }
        }
        last.insert(touch.path, (touch.seq, touch.after));
    }
    for (path, (seq, expected)) in last {
        let actual = row_sha(connection, &path)
            .map_err(|error| Broken::new(Some(seq), "row", format!("{path}: {}", error.message)))?;
        if actual != expected {
            return Err(Broken::new(
                Some(seq),
                "row",
                format!(
                    "{path} is not what its last audit record says: expected {}, found {}",
                    expected.as_deref().unwrap_or("no row"),
                    actual.as_deref().unwrap_or("no row")
                ),
            ));
        }
    }
    Ok(())
}

fn full_check(connection: &Connection, authority: &Authority) -> Result<Verified, Broken> {
    let verified = audit::verify(connection, None)?;
    if verified.last.is_none() {
        return Err(broken("audit_anchor", "the audit chain is empty"));
    }
    check_manifest(connection, authority)?;
    check_legacy(connection)?;
    check_sqlite(connection)?;
    check_rows(connection)?;
    Ok(verified)
}

/// Rebuild the checkpoint after a check that passed. A busy or failing store leaves it as it
/// is: the check itself succeeded, and the next write moves the checkpoint anyway.
fn rebuild_checkpoint(state: &Path, last: &(i64, String)) {
    let Ok(connection) = Connection::open_with_flags(
        state.join(STORE_NAME),
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    ) else {
        return;
    };
    if connection
        .busy_timeout(std::time::Duration::from_millis(500))
        .is_err()
    {
        return;
    }
    // Only a store of the current schema has the table; an older one is upgraded by the next write.
    let version: i64 = connection
        .pragma_query_value(None, "user_version", |row| row.get(0))
        .unwrap_or(0);
    if version < crate::schema::SCHEMA_VERSION {
        return;
    }
    let _ = audit::move_checkpoint(&connection, last.0, &last.1);
}

/// Verify the store of `state`: the whole chain and everything it anchors when `full`, else the
/// records after the checkpoint.
///
/// # Errors
/// `not_found` when the store is not the authority; `invariant_violation` naming the first check
/// that failed; `store_busy` or `internal` when the store cannot be read.
pub fn validate(state: &Path, full: bool) -> Result<Value, ErrorBody> {
    let authority = match epoch(state) {
        Epoch::Store(authority) => authority,
        Epoch::FileLedger => {
            return Err(not_attached(
                "the file ledger is the authority of this repository",
            ));
        }
        Epoch::Inconsistent(reason) => return Err(not_attached(&reason)),
    };
    // SQLite's own integrity check does not look at the CHECK constraints through a connection
    // that was opened read-only, so the file is opened for writing and `query_only` keeps the
    // check from changing anything.
    let connection = Connection::open_with_flags(
        state.join(STORE_NAME),
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|err| Broken::from(err).to_error())?;
    connection
        .busy_timeout(std::time::Duration::from_secs(5))
        .map_err(|err| Broken::from(err).to_error())?;
    connection
        .execute_batch("PRAGMA query_only = ON;")
        .map_err(|err| Broken::from(err).to_error())?;
    let result = (|| -> Result<Verified, Broken> {
        connection.execute_batch("BEGIN")?;
        let verified = if full {
            full_check(&connection, &authority)
        } else {
            audit::verify_tail(&connection)
        };
        let _ = connection.execute_batch("COMMIT");
        verified
    })();
    let verified = result.map_err(|broken: Broken| broken.to_error())?;
    if let Some(last) = &verified.last {
        rebuild_checkpoint(state, last);
    }
    Ok(json!({
        "valid": true,
        "mode": if full { "full" } else { "incremental" },
        "audit_records": verified.last.as_ref().map_or(0, |(seq, _)| *seq),
        "checked": verified.checked,
    }))
}
