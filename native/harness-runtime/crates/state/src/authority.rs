//! Who is the authority over the operational state (ADR 0020, section 1).
//!
//! Before the cutover the file ledger is: `ledger.json` has `version = 3`. After it the runtime
//! store is: `ledger.json` has `version = 4`, `state/AUTHORITY.json` says `runtime-store`, and the
//! `store_id` and `manifest_sha256` in that file are the ones in `runtime.db`. `migrate` and the
//! `state.read` of the runtime both ask `epoch`, so they cannot disagree about it.

use std::fs;
use std::io;
use std::path::Path;

use rusqlite::{Connection, OpenFlags};
use serde_json::{Value, json};

use crate::canonical::canonical_json;
use crate::legacy::read_pointer;

pub const AUTHORITY_NAME: &str = "AUTHORITY.json";
pub const STORE_NAME: &str = "runtime.db";
pub const IMPORTING_NAME: &str = "runtime.db.importing";
pub const RUNTIME_STORE: &str = "runtime-store";
/// The pointer version of the runtime-store epoch.
pub const STORE_VERSION: i64 = 4;

/// `state/AUTHORITY.json`.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Authority {
    pub store_id: String,
    pub manifest_sha256: String,
    /// The snapshot directory, relative to the repository root.
    pub snapshot: String,
    pub cutover_at: String,
}

impl Authority {
    /// The canonical text of `AUTHORITY.json`.
    pub fn to_json(&self) -> String {
        canonical_json(&json!({
            "authority": RUNTIME_STORE,
            "store_id": self.store_id,
            "manifest_sha256": self.manifest_sha256,
            "snapshot": self.snapshot,
            "cutover_at": self.cutover_at,
        }))
    }
}

/// What the state directory says about its authority.
#[derive(Debug, PartialEq, Eq)]
pub enum Epoch {
    /// `ledger.json` is the authority (or there is none): the file ledger.
    FileLedger,
    /// The store is the authority and the three files agree.
    Store(Authority),
    /// `ledger.json` says version 4 but the store or `AUTHORITY.json` does not match: an operator
    /// must look.
    Inconsistent(String),
}

fn text(record: &serde_json::Map<String, Value>, key: &str) -> Option<String> {
    record.get(key).and_then(Value::as_str).map(str::to_owned)
}

/// Read `AUTHORITY.json`; `Ok(None)` when it does not exist.
pub fn read(state: &Path) -> io::Result<Option<(String, Authority)>> {
    let bytes = match fs::read(state.join(AUTHORITY_NAME)) {
        Ok(bytes) => bytes,
        Err(err) if err.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(err) => return Err(err),
    };
    let Ok(Value::Object(record)) = serde_json::from_slice(&bytes) else {
        return Ok(Some((String::new(), Authority::default())));
    };
    let authority = (
        text(&record, "authority").unwrap_or_default(),
        Authority {
            store_id: text(&record, "store_id").unwrap_or_default(),
            manifest_sha256: text(&record, "manifest_sha256").unwrap_or_default(),
            snapshot: text(&record, "snapshot").unwrap_or_default(),
            cutover_at: text(&record, "cutover_at").unwrap_or_default(),
        },
    );
    Ok(Some(authority))
}

fn meta(connection: &Connection, key: &str) -> Option<String> {
    connection
        .query_row(
            "SELECT value FROM store_meta WHERE key = ?1",
            [key],
            |row| row.get(0),
        )
        .ok()
}

/// The epoch of the state directory at `state`.
pub fn epoch(state: &Path) -> Epoch {
    let pointer = match read_pointer(state) {
        Ok(Some(pointer)) => pointer,
        Ok(None) => return Epoch::FileLedger,
        Err(_) => return Epoch::FileLedger,
    };
    if pointer.version != STORE_VERSION {
        return Epoch::FileLedger;
    }
    let inconsistent = |reason: &str| Epoch::Inconsistent(reason.to_owned());
    let (authority, record) = match read(state) {
        Ok(Some(found)) => found,
        Ok(None) => {
            return inconsistent("ledger.json says version 4 but AUTHORITY.json is missing");
        }
        Err(err) => return Epoch::Inconsistent(format!("AUTHORITY.json is unreadable: {err}")),
    };
    if authority != RUNTIME_STORE {
        return inconsistent("AUTHORITY.json does not name the runtime store as the authority");
    }
    let connection = match Connection::open_with_flags(
        state.join(STORE_NAME),
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    ) {
        Ok(connection) => connection,
        Err(err) => return Epoch::Inconsistent(format!("runtime.db cannot be opened: {err}")),
    };
    if meta(&connection, "store_id").as_deref() != Some(record.store_id.as_str())
        || meta(&connection, "manifest_sha256").as_deref() != Some(record.manifest_sha256.as_str())
    {
        return inconsistent(
            "the store_id or manifest_sha256 of runtime.db differs from AUTHORITY.json",
        );
    }
    Epoch::Store(record)
}
