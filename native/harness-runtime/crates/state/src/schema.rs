//! The schema of the store, version 2 (`PRAGMA user_version = 2`; ADR 0020, sections 3 and 4).
//!
//! Version 2 adds the `operations` table of the idempotent writes (ADR 0021, section 4), a
//! `revision` to every row that a command may change, the denormalised `execution_state` of a
//! dispatch with the CHECK of ADR 0021, section 6, and a singleton `audit_checkpoint`. A store of
//! version 1 is upgraded in place by `upgrade` when the runtime starts or before its first write.
//!
//! One table holds one kind of record. A column named like a field of the legacy record (`state`,
//! `batch_id`, the risk fields of a batch) carries that field when the field has the type the
//! column has; a field that is absent, JSON `null` or of another type stays in `body_json`, so a
//! column that is NULL means "the typed field is not there" and the record projects back to the
//! bytes it had. `id` is the file name of the legacy record without `.json`.

use contracts::ErrorCode;
use rusqlite::{Connection, OpenFlags, TransactionBehavior};
use serde_json::json;

use crate::audit;
use crate::authority::{Epoch, STORE_NAME, epoch};
use crate::error::{Error, Result};
use crate::time::iso_now;

/// The schema version this build writes and the newest it serves.
pub const SCHEMA_VERSION: i64 = 2;

macro_rules! audit_checkpoint_table {
    () => {
        "CREATE TABLE audit_checkpoint (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    seq          INTEGER NOT NULL,
    chain_sha256 TEXT NOT NULL
) STRICT;"
    };
}

macro_rules! operations_table {
    () => {
        "CREATE TABLE operations (
    operation_id   TEXT PRIMARY KEY,
    operation      TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    state          TEXT NOT NULL CHECK (state IN ('intent', 'committed', 'failed')),
    result_json    TEXT,
    created_at     TEXT NOT NULL,
    completed_at   TEXT
) STRICT;"
    };
}

/// The column that holds the state of the execution of a dispatch. Rust writes it with the
/// `executions` of the supervisor and never writes `DispatchState` (ADR 0021, section 6); SQLite
/// forbids a subquery in a CHECK, so the invariant sits beside `state` in the row. The same text
/// creates the column and adds it to a store of version 1.
macro_rules! execution_state_column {
    () => {
        "execution_state TEXT CHECK (execution_state IS NULL OR (
        execution_state IN ('launching', 'running', 'stopping', 'exited', 'lost', 'unknown')
        AND NOT (
            execution_state IN ('unknown', 'launching', 'running')
            AND state IN ('reported', 'failed', 'blocked', 'cancelled', 'abandoned')
        )))"
    };
}

/// The tables, in the order that satisfies the foreign keys.
pub const SCHEMA: &str = concat!(
    "
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
    revision  INTEGER NOT NULL DEFAULT 1,
    body_json TEXT NOT NULL,
    ",
    execution_state_column!(),
    "
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
    revision  INTEGER NOT NULL DEFAULT 1,
    body_json TEXT NOT NULL
) STRICT;

CREATE TABLE record (
    path       TEXT PRIMARY KEY,
    collection TEXT NOT NULL,
    revision   INTEGER NOT NULL DEFAULT 1,
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

-- One row: the last audit record that an ordinary command need not verify again.
",
    audit_checkpoint_table!(),
    "

-- The idempotent writes (ADR 0021, section 4): one row per operation id.
",
    operations_table!(),
    "
"
);

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

/// The statements that bring a store of version 1 to version 2. The audit checkpoint of version 1
/// was never written, so its table is replaced rather than altered.
const UPGRADE_1_TO_2: [&str; 7] = [
    "ALTER TABLE dispatch_status ADD COLUMN revision INTEGER NOT NULL DEFAULT 1",
    concat!(
        "ALTER TABLE dispatch_status ADD COLUMN ",
        execution_state_column!()
    ),
    "ALTER TABLE qa_queue ADD COLUMN revision INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE record ADD COLUMN revision INTEGER NOT NULL DEFAULT 1",
    "DROP TABLE audit_checkpoint",
    audit_checkpoint_table!(),
    operations_table!(),
];

fn user_version(connection: &Connection) -> Result<i64> {
    Ok(connection.pragma_query_value(None, "user_version", |row| row.get(0))?)
}

/// Bring the store behind `connection` to `SCHEMA_VERSION`, in one exclusive transaction that
/// also appends the audit record `schema-migrate`. Returns whether it changed anything. A store
/// of a newer version is refused without a write, and a version is never lowered.
///
/// # Errors
/// A refusal for a newer or an unknown version; an `Interrupted` error when the store fails.
pub fn upgrade(connection: &mut Connection) -> Result<bool> {
    check_version(user_version(connection)?)?;
    if user_version(connection)? == SCHEMA_VERSION {
        return Ok(false);
    }
    let transaction = connection.transaction_with_behavior(TransactionBehavior::Exclusive)?;
    // Another process may have upgraded the store while this one waited for the lock.
    let from = user_version(&transaction)?;
    check_version(from)?;
    if from == SCHEMA_VERSION {
        return Ok(false);
    }
    for statement in UPGRADE_1_TO_2 {
        transaction.execute_batch(statement)?;
    }
    audit::append(
        &transaction,
        &iso_now(),
        "schema-migrate",
        &json!({"from": from, "to": SCHEMA_VERSION}),
    )?;
    transaction.pragma_update(None, "user_version", SCHEMA_VERSION)?;
    transaction.commit()?;
    Ok(true)
}

fn check_version(version: i64) -> Result<()> {
    if version > SCHEMA_VERSION {
        return Err(Error::refused(
            ErrorCode::ProtocolUnsupported,
            format!(
                "the store has the schema version {version}; this runtime serves up to {SCHEMA_VERSION}"
            ),
            "update harness-runtime to the release that wrote the store; a schema version is never lowered",
        ));
    }
    if version < 1 {
        return Err(Error::refused(
            ErrorCode::Internal,
            format!("runtime.db has the schema version {version}, which no runtime release wrote"),
            "restore the state directory from the snapshot named in AUTHORITY.json",
        ));
    }
    Ok(())
}

/// Upgrade the store of `state` when it is the authority and older than this build. A state
/// directory that serves the file ledger has no store to upgrade.
///
/// # Errors
/// See [`upgrade`].
pub fn upgrade_store(state: &std::path::Path) -> Result<bool> {
    if !matches!(epoch(state), Epoch::Store(_)) {
        return Ok(false);
    }
    let mut connection = Connection::open_with_flags(
        state.join(STORE_NAME),
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    connection.busy_timeout(std::time::Duration::from_secs(5))?;
    connection.execute_batch("PRAGMA synchronous = FULL; PRAGMA foreign_keys = ON;")?;
    upgrade(&mut connection)
}
