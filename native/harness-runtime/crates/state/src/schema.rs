//! The schema of the store, version 1 (`PRAGMA user_version = 1`; ADR 0020, sections 3 and 4).
//!
//! One table holds one kind of record. A column named like a field of the legacy record (`state`,
//! `batch_id`, the risk fields of a batch) carries that field when the field has the type the
//! column has; a field that is absent, JSON `null` or of another type stays in `body_json`, so a
//! column that is NULL means "the typed field is not there" and the record projects back to the
//! bytes it had. `id` is the file name of the legacy record without `.json`.

use rusqlite::Connection;

use crate::error::Result;

/// The schema version this build writes and the newest it serves.
pub const SCHEMA_VERSION: i64 = 1;

/// The tables, in the order that satisfies the foreign keys.
pub const SCHEMA: &str = "
CREATE TABLE store_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;

CREATE TABLE batch (
    id                             TEXT PRIMARY KEY,
    state                          TEXT NOT NULL,
    revision                       INTEGER NOT NULL DEFAULT 1,
    risk_reassessment_required     INTEGER,
    risk_reassessment_candidate    TEXT,
    risk_reassessment_pending_next INTEGER,
    body_json                      TEXT NOT NULL,
    -- Issue #781: a batch that owes a risk reassessment names its candidate or is pending.
    CHECK (NOT (
        risk_reassessment_required = 1
        AND COALESCE(risk_reassessment_candidate, '') = ''
        AND COALESCE(risk_reassessment_pending_next, 0) != 1
    ))
) STRICT;

CREATE TABLE plan (
    id        TEXT PRIMARY KEY,
    body_json TEXT NOT NULL
) STRICT;

CREATE TABLE dispatch (
    id        TEXT PRIMARY KEY,
    batch_id  TEXT NOT NULL REFERENCES batch (id),
    state     TEXT NOT NULL,
    revision  INTEGER NOT NULL DEFAULT 1,
    body_json TEXT NOT NULL
) STRICT;

CREATE TABLE dispatch_status (
    id        TEXT PRIMARY KEY REFERENCES dispatch (id),
    state     TEXT NOT NULL,
    body_json TEXT NOT NULL
) STRICT;

CREATE TABLE risk_assessment (
    id        TEXT PRIMARY KEY,
    body_json TEXT NOT NULL
) STRICT;

CREATE TABLE context_package (
    id        TEXT PRIMARY KEY,
    body_json TEXT NOT NULL
) STRICT;

CREATE TABLE checkpoint (
    id        TEXT PRIMARY KEY,
    body_json TEXT NOT NULL
) STRICT;

CREATE TABLE qa_lane (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    revision      INTEGER NOT NULL DEFAULT 1,
    fencing_token INTEGER NOT NULL DEFAULT 0,
    body_json     TEXT NOT NULL
) STRICT;

CREATE TABLE qa_queue (
    id        TEXT PRIMARY KEY,
    body_json TEXT NOT NULL
) STRICT;

CREATE TABLE record (
    path       TEXT PRIMARY KEY,
    collection TEXT NOT NULL,
    body_json  TEXT NOT NULL
) STRICT;

CREATE TABLE artifact (
    path   TEXT PRIMARY KEY,
    bytes  BLOB NOT NULL,
    sha256 TEXT NOT NULL
) STRICT;

CREATE TABLE legacy_generation (
    generation TEXT NOT NULL,
    path       TEXT NOT NULL,
    bytes      BLOB NOT NULL,
    sha256     TEXT NOT NULL,
    PRIMARY KEY (generation, path)
) STRICT;

CREATE TABLE legacy_audit (
    path                TEXT PRIMARY KEY,
    bytes               BLOB NOT NULL,
    sha256              TEXT NOT NULL,
    record_sha256_valid INTEGER NOT NULL
) STRICT;

CREATE TABLE import_manifest (
    path   TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL
) STRICT;

CREATE TABLE audit (
    seq               INTEGER PRIMARY KEY,
    at                TEXT NOT NULL,
    action            TEXT NOT NULL,
    details_json      TEXT NOT NULL,
    record_sha256     TEXT NOT NULL,
    prev_chain_sha256 TEXT NOT NULL,
    chain_sha256      TEXT NOT NULL
) STRICT;

CREATE TABLE audit_checkpoint (
    seq          INTEGER NOT NULL,
    chain_sha256 TEXT NOT NULL
) STRICT;
";

/// Open a new store file for the import: the schema is created and the version is set. The
/// journal mode stays `DELETE` until the import is complete (`seal`).
pub fn create(path: &std::path::Path) -> Result<Connection> {
    let connection = Connection::open(path)?;
    connection.execute_batch("PRAGMA synchronous = FULL; PRAGMA foreign_keys = ON;")?;
    connection.execute_batch(SCHEMA)?;
    connection.pragma_update(None, "user_version", SCHEMA_VERSION)?;
    Ok(connection)
}

/// Switch the finished store to WAL and close it. A store left in `DELETE` mode would be renamed
/// into place without its mode, and a WAL file left next to it would lose rows at the rename.
pub fn seal(connection: Connection) -> Result<()> {
    let mode: String =
        connection.pragma_update_and_check(None, "journal_mode", "WAL", |row| row.get(0))?;
    if !mode.eq_ignore_ascii_case("wal") {
        return Err(crate::error::Error::Interrupted(format!(
            "the store did not enter WAL mode: {mode}"
        )));
    }
    connection
        .close()
        .map_err(|(_, err)| crate::error::Error::from(err))
}
