//! The schema of the store, version 4 (`PRAGMA user_version = 4`; ADR 0020, sections 3, 4 and 7).
//!
//! Version 2 adds the `operations` table of the idempotent writes (ADR 0021, section 4), a
//! `revision` to every row that a command may change, the denormalised `execution_state` of a
//! dispatch with the CHECK of ADR 0021, section 6, and a singleton `audit_checkpoint`. Version 3
//! adds the singleton `generation` row, whose revision guards the decisions that read every batch
//! (ADR 0020, section 3), and makes the QA lane a singleton row that outlives its leases: the
//! owner of the lease and the identity of its process are columns, and the row stays free after
//! a release, so its `fencing_token` never goes back (ADR 0020, section 7). Version 4 adds the
//! `executions` of the process supervisor (ADR 0022, section 2): one row per launch of a
//! dispatch, its process identity, its receipt, its exit evidence and its lease, and a trigger
//! that allows only the transitions of `executions::allowed`. A store of version 1, 2 or 3 is
//! upgraded in place by `upgrade` when the runtime starts or before its first write.
//!
//! One table holds one kind of record. A column named like a field of the legacy record (`state`,
//! `batch_id`, the risk fields of a batch) carries that field when the field has the type the
//! column has; a field that is absent, JSON `null` or of another type stays in `body_json`, so a
//! column that is NULL means "the typed field is not there" and the record projects back to the
//! bytes it had. `id` is the file name of the legacy record without `.json`.

use contracts::ErrorCode;
use rusqlite::{Connection, OpenFlags, Transaction, TransactionBehavior};
use serde_json::{Map, Value, json};

use crate::audit;
use crate::authority::{Epoch, STORE_NAME, epoch};
use crate::error::{Error, Result};
use crate::time::iso_now;

/// The schema version this build writes and the newest it serves.
pub const SCHEMA_VERSION: i64 = 4;

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

/// The QA lane (ADR 0020, section 7): one row, `id = 1`, created by the first lease and kept
/// after every release. The owner columns are all set while a lease is held and all NULL while
/// the lane is free; the identity of the owner's process (`host`, `pid` and `start_time`, with
/// the `boot_id` and the `pid_namespace` the platform reveals) is NULL while the lane is free and
/// for a lease of version 2. `body_json` is the projection `qa-lane/lease.json` while a lease is
/// held.
macro_rules! qa_lane_table {
    ($name:literal) => {
        concat!(
            "CREATE TABLE ",
            $name,
            " (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    revision            INTEGER NOT NULL DEFAULT 1,
    fencing_token       INTEGER NOT NULL DEFAULT 0,
    body_json           TEXT NOT NULL,
    owner_kind          TEXT,
    owner_id            TEXT,
    acquired_at         TEXT,
    expires_at          TEXT,
    heartbeat_at        TEXT,
    host                TEXT,
    boot_id             TEXT,
    pid_namespace       TEXT,
    pid                 INTEGER,
    start_time          TEXT,
    owner_pid_namespace TEXT,
    CHECK ((owner_kind IS NULL) = (owner_id IS NULL)
       AND (owner_id IS NULL) = (acquired_at IS NULL)
       AND (owner_id IS NULL) = (expires_at IS NULL)
       AND (owner_id IS NULL) = (heartbeat_at IS NULL)),
    CHECK ((host IS NULL) = (pid IS NULL) AND (pid IS NULL) = (start_time IS NULL)),
    CHECK (owner_id IS NOT NULL OR (host IS NULL AND boot_id IS NULL AND pid_namespace IS NULL
       AND owner_pid_namespace IS NULL))
) STRICT;"
        )
    };
}

/// The revision of the shared state (ADR 0020, section 3): a request that creates a batch or
/// changes the state of one moves it, and a request that decided over every batch expects it.
macro_rules! generation_table {
    () => {
        "CREATE TABLE generation (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    revision INTEGER NOT NULL
) STRICT;
INSERT INTO generation (id, revision) VALUES (1, 1);"
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

/// The executions of the supervisor (ADR 0021, section 6; ADR 0022, section 2): `id` is the
/// operation id of the launch. The identity columns (`host` to `start_time`, and `pgid`) are set
/// once the process is known; a state that names a process needs them. Only `exited` and `lost`
/// are terminal: a terminal row never changes again, and only a terminal row releases its lease.
/// The trigger allows the edges of `executions::allowed` and nothing else.
macro_rules! executions_table {
    () => {
        "CREATE TABLE executions (
    id                    TEXT PRIMARY KEY,
    dispatch_id           TEXT NOT NULL REFERENCES dispatch (id),
    attempt               INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    state                 TEXT NOT NULL CHECK (state IN
        ('launching', 'running', 'stopping', 'exited', 'lost', 'unknown')),
    revision              INTEGER NOT NULL DEFAULT 1,
    launch_json           TEXT NOT NULL,
    execution_deadline    TEXT NOT NULL,
    stop_grace_seconds    INTEGER NOT NULL,
    poll_interval_seconds INTEGER NOT NULL,
    host                  TEXT,
    boot_id               TEXT,
    pid_namespace         TEXT,
    pid                   INTEGER,
    start_time            TEXT,
    pgid                  INTEGER,
    receipt_json          TEXT,
    exit_json             TEXT,
    observation_json      TEXT,
    lease_owner_json      TEXT NOT NULL,
    lease_released_at     TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    UNIQUE (dispatch_id, attempt),
    CHECK ((pid IS NULL) = (start_time IS NULL)),
    CHECK (state NOT IN ('running', 'stopping', 'exited') OR pid IS NOT NULL),
    CHECK (state <> 'exited' OR exit_json IS NOT NULL),
    CHECK (lease_released_at IS NULL OR state IN ('exited', 'lost'))
) STRICT;
CREATE TRIGGER executions_terminal BEFORE UPDATE ON executions
WHEN OLD.state IN ('exited', 'lost')
BEGIN SELECT RAISE(ABORT, 'a terminal execution never changes'); END;
CREATE TRIGGER executions_transition BEFORE UPDATE OF state ON executions
WHEN OLD.state <> NEW.state AND NOT (
       (OLD.state = 'launching' AND NEW.state IN ('running', 'exited', 'lost', 'unknown'))
    OR (OLD.state = 'running' AND NEW.state IN ('stopping', 'exited', 'lost', 'unknown'))
    OR (OLD.state = 'stopping' AND NEW.state IN ('exited', 'lost', 'unknown'))
    OR (OLD.state = 'unknown' AND NEW.state IN ('running', 'exited', 'lost')))
BEGIN SELECT RAISE(ABORT, 'the execution state does not allow this transition'); END;"
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

",
    qa_lane_table!("qa_lane"),
    "

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

-- The singleton revision of the shared state (ADR 0020, section 3).
",
    generation_table!(),
    "

-- The executions of the process supervisor (ADR 0022, section 2).
",
    executions_table!(),
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

/// The owner of a lease body that a command wrote with a generic mutation, or that the import
/// read from the legacy lane: `owner_kind` (a legacy lease names a `dispatch_id` only),
/// `owner_id`, `acquired_at` and `expires_at`. A field the body lacks is an empty text, so the
/// owner columns stay all set while the lease is held.
pub(crate) fn lease_owner(record: &Map<String, Value>) -> [String; 4] {
    let text = |key: &str| record.get(key).and_then(Value::as_str).map(str::to_owned);
    [
        text("owner_kind").unwrap_or_else(|| "dispatch".to_owned()),
        text("owner_id")
            .or_else(|| text("dispatch_id"))
            .unwrap_or_default(),
        text("acquired_at").unwrap_or_default(),
        text("expires_at").unwrap_or_default(),
    ]
}

/// Bring a store of version 2 to version 3: the generation row, and the QA lane rebuilt with its
/// owner columns. A held lease keeps its body, its owner and its expiry; its identity is unknown
/// (NULL), so only an operator clears it.
fn upgrade_2_to_3(transaction: &Transaction<'_>) -> Result<()> {
    transaction.execute_batch(generation_table!())?;
    transaction.execute_batch(qa_lane_table!("qa_lane_v3"))?;
    let rows: Vec<(i64, i64, i64, String)> = transaction
        .prepare("SELECT id, revision, fencing_token, body_json FROM qa_lane")?
        .query_map([], |row| {
            Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?))
        })?
        .collect::<rusqlite::Result<_>>()?;
    for (id, revision, fencing_token, body) in rows {
        let record = match serde_json::from_str::<Value>(&body) {
            Ok(Value::Object(record)) => record,
            _ => Map::new(),
        };
        let [kind, owner, acquired_at, expires_at] = lease_owner(&record);
        transaction.execute(
            "INSERT INTO qa_lane_v3 (id, revision, fencing_token, body_json, owner_kind, owner_id,
                                     acquired_at, expires_at, heartbeat_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?7)",
            rusqlite::params![
                id,
                revision,
                fencing_token,
                body,
                kind,
                owner,
                acquired_at,
                expires_at
            ],
        )?;
    }
    transaction.execute_batch("DROP TABLE qa_lane; ALTER TABLE qa_lane_v3 RENAME TO qa_lane;")?;
    Ok(())
}

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
    if from < 2 {
        for statement in UPGRADE_1_TO_2 {
            transaction.execute_batch(statement)?;
        }
    }
    if from < 3 {
        upgrade_2_to_3(&transaction)?;
    }
    // Version 4: the executions of the supervisor; no row of an older store changes.
    transaction.execute_batch(executions_table!())?;
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
