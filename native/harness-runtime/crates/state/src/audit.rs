//! The audit chain of the store (ADR 0020, section 4).
//!
//! Every record is `seq`, `at`, `action` and `details`. `record_sha256` hashes the canonical form
//! of `{action, at, details}`; `chain_sha256` is the SHA-256 of the previous `chain_sha256` and
//! this `record_sha256`, both as lower-case hex text. The first record chains from 64 zeros.

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
