//! The audit chain of the store (ADR 0020, section 4).
//!
//! Every record is `seq`, `at`, `action` and `details`. `record_sha256` hashes the canonical form
//! of `{action, at, details}`; `chain_sha256` is the SHA-256 of the previous `chain_sha256` and
//! this `record_sha256`, both as lower-case hex text. The first record chains from 64 zeros.

use contracts::{ErrorBody, ErrorCode, FailureDomain};
use rusqlite::{Connection, OptionalExtension};
use serde_json::{Value, json};

use crate::canonical::{canonical_json, sha256_hex};
use crate::error::Result;

/// The `prev_chain_sha256` of the first record.
pub const GENESIS_CHAIN: &str = "0000000000000000000000000000000000000000000000000000000000000000";

/// The `record_sha256` of an audit record.
pub fn record_sha256(at: &str, action: &str, details: &Value) -> String {
    let record = json!({"action": action, "at": at, "details": details});
    sha256_hex(canonical_json(&record).as_bytes())
}

/// The `chain_sha256` that follows `prev` and `record_sha256`.
pub fn chain_sha256(prev: &str, record_sha256: &str) -> String {
    sha256_hex(format!("{prev}{record_sha256}").as_bytes())
}

/// The actions a record of the chain may name: the closed vocabulary of ADR 0020, section 4.
pub const ACTIONS: [&str; 7] = [
    "legacy-import",
    "migration-repair",
    "schema-migrate",
    "immutable-record",
    "immutable-artifact",
    "transition",
    "deletion",
];

/// What a check of the chain found wrong, and where.
#[derive(Debug, PartialEq, Eq)]
pub struct Broken {
    /// The `seq` of the record, when one record is at fault.
    pub seq: Option<i64>,
    /// The name of the check that failed.
    pub check: &'static str,
    pub detail: String,
}

impl Broken {
    pub fn new(seq: Option<i64>, check: &'static str, detail: impl Into<String>) -> Self {
        Self {
            seq,
            check,
            detail: detail.into(),
        }
    }

    /// The typed error of the protocol for a store that fails its own verification.
    pub fn to_error(&self) -> ErrorBody {
        let mut error = ErrorBody::new(
            ErrorCode::InvariantViolation,
            FailureDomain::Infrastructure,
            false,
            format!("the store failed the check {}: {}", self.check, self.detail),
            "stop every writer, run `coordinator.py ledger validate --full`, and restore the state directory from the snapshot named in AUTHORITY.json",
        )
        .with_fact("check", self.check);
        if let Some(seq) = self.seq {
            error = error.with_fact("seq", seq);
        }
        error
    }
}

impl From<rusqlite::Error> for Broken {
    fn from(err: rusqlite::Error) -> Self {
        Self::new(None, "store", err.to_string())
    }
}

/// The end of a verified stretch of the chain.
#[derive(Debug, PartialEq, Eq)]
pub struct Verified {
    /// How many records the check read.
    pub checked: i64,
    /// The last record of the chain, `None` for an empty chain.
    pub last: Option<(i64, String)>,
}

/// Verify the records after `from` (the whole chain for `None`): consecutive `seq`, the links
/// of `prev_chain_sha256`, the hashes of every record and the closed vocabulary of actions. The
/// first record of the chain is the `legacy-import` that anchors it.
///
/// # Errors
/// The first record that fails a check.
pub fn verify(
    connection: &Connection,
    from: Option<(i64, &str)>,
) -> std::result::Result<Verified, Broken> {
    let (mut expected_seq, mut prev) = match from {
        Some((seq, chain)) => (seq + 1, chain.to_owned()),
        None => (1, GENESIS_CHAIN.to_owned()),
    };
    let mut statement = connection.prepare(
        "SELECT seq, at, action, details_json, record_sha256, prev_chain_sha256, chain_sha256
         FROM audit WHERE seq > ?1 ORDER BY seq",
    )?;
    let mut rows = statement.query([expected_seq - 1])?;
    let mut checked = 0;
    let mut last = from.map(|(seq, chain)| (seq, chain.to_owned()));
    while let Some(row) = rows.next()? {
        let seq: i64 = row.get(0)?;
        let (at, action, details_json): (String, String, String) =
            (row.get(1)?, row.get(2)?, row.get(3)?);
        let (record, prev_stored, chain): (String, String, String) =
            (row.get(4)?, row.get(5)?, row.get(6)?);
        if seq != expected_seq {
            return Err(Broken::new(
                Some(seq),
                "audit_sequence",
                format!("the record {expected_seq} is missing or out of order: found {seq}"),
            ));
        }
        if !ACTIONS.contains(&action.as_str()) {
            return Err(Broken::new(
                Some(seq),
                "audit_action",
                format!("the action {action:?} is not in the vocabulary of the chain"),
            ));
        }
        if seq == 1 && action != "legacy-import" {
            return Err(Broken::new(
                Some(seq),
                "audit_anchor",
                "the first record of the chain is not the legacy-import",
            ));
        }
        let Ok(details) = serde_json::from_str::<Value>(&details_json) else {
            return Err(Broken::new(
                Some(seq),
                "audit_record",
                "the details of the record are not JSON",
            ));
        };
        if record_sha256(&at, &action, &details) != record {
            return Err(Broken::new(
                Some(seq),
                "audit_record",
                "record_sha256 is not the hash of the record",
            ));
        }
        if prev_stored != prev || chain_sha256(&prev, &record) != chain {
            return Err(Broken::new(
                Some(seq),
                "audit_chain",
                "the record does not continue the chain before it",
            ));
        }
        prev = chain.clone();
        last = Some((seq, chain));
        expected_seq += 1;
        checked += 1;
    }
    Ok(Verified { checked, last })
}

/// The point an ordinary command verifies from: the checkpoint, when the record it names is in
/// the chain with the same `chain_sha256`. A missing, damaged or foreign checkpoint is `None`,
/// and the command verifies the whole chain.
pub fn anchor(connection: &Connection) -> rusqlite::Result<Option<(i64, String)>> {
    connection
        .query_row(
            "SELECT c.seq, c.chain_sha256 FROM audit_checkpoint c
             JOIN audit a ON a.seq = c.seq AND a.chain_sha256 = c.chain_sha256
             WHERE c.id = 1",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()
}

/// What an ordinary command verifies: the records after the checkpoint.
///
/// # Errors
/// See [`verify`].
pub fn verify_tail(connection: &Connection) -> std::result::Result<Verified, Broken> {
    let anchor = anchor(connection)?;
    verify(
        connection,
        anchor.as_ref().map(|(seq, chain)| (*seq, chain.as_str())),
    )
}

/// Move the checkpoint to `seq`, the last record of a chain that was verified.
pub fn move_checkpoint(connection: &Connection, seq: i64, chain: &str) -> rusqlite::Result<()> {
    connection
        .execute(
            "INSERT INTO audit_checkpoint (id, seq, chain_sha256) VALUES (1, ?1, ?2)
             ON CONFLICT (id) DO UPDATE SET seq = excluded.seq, chain_sha256 = excluded.chain_sha256",
            rusqlite::params![seq, chain],
        )
        .map(|_| ())
}

/// The last record of the chain.
pub fn tail(connection: &Connection) -> rusqlite::Result<Option<(i64, String)>> {
    connection
        .query_row(
            "SELECT seq, chain_sha256 FROM audit ORDER BY seq DESC LIMIT 1",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()
}

/// Append one record to the chain and return its `seq`.
pub fn append(connection: &Connection, at: &str, action: &str, details: &Value) -> Result<i64> {
    let last: Option<(i64, String)> = connection
        .query_row(
            "SELECT seq, chain_sha256 FROM audit ORDER BY seq DESC LIMIT 1",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()?;
    let (seq, prev) = match last {
        Some((seq, chain)) => (seq + 1, chain),
        None => (1, GENESIS_CHAIN.to_owned()),
    };
    let record = record_sha256(at, action, details);
    let chain = chain_sha256(&prev, &record);
    connection.execute(
        "INSERT INTO audit (seq, at, action, details_json, record_sha256, prev_chain_sha256, chain_sha256)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        rusqlite::params![seq, at, action, details.to_string(), record, prev, chain],
    )?;
    Ok(seq)
}
