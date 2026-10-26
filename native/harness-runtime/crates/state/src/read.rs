//! The read operation of the store: what `state.read` of the IPC answers (ADR 0020, section 3).
//!
//! A read opens the store read-only in one transaction, so it never sees half of a transition, and
//! answers records in the form the legacy file ledger had them: the same path (`batches/<id>.json`)
//! and the same JSON object, so the Python policy layer renders the same text from either epoch.
//! The revision of the answer is the sequence number of the last audit record: it moves with every
//! change of the store.

use std::path::Path;

use rusqlite::{Connection, OpenFlags, OptionalExtension};
use serde_json::{Map, Value};

use crate::authority::{Epoch, STORE_NAME, epoch};
use crate::project::{Spec, spec_of};

/// The largest answer: a frame holds 16 MiB, and the answer keeps room for the envelope.
pub const MAX_ANSWER_BYTES: usize = 15 * 1024 * 1024;
/// The most paths a request may name.
pub const MAX_PATHS: usize = 10_000;

/// What a read can refuse with.
#[derive(Debug, PartialEq, Eq)]
pub enum ReadError {
    /// The runtime store is not the authority of this repository (or its files disagree).
    NotAttached(String),
    /// The request is not valid.
    Malformed(String),
    /// The store moved on since the revision the request expected.
    RevisionConflict { expected: u64, actual: u64 },
    /// The answer would not fit one frame.
    TooLarge,
    /// The store could not be read; `busy` says the reason is a lock another process holds.
    Store { message: String, busy: bool },
}

impl From<rusqlite::Error> for ReadError {
    fn from(err: rusqlite::Error) -> Self {
        let busy = matches!(
            err.sqlite_error_code(),
            Some(rusqlite::ErrorCode::DatabaseBusy | rusqlite::ErrorCode::DatabaseLocked)
        );
        Self::Store {
            message: err.to_string(),
            busy,
        }
    }
}

/// The records of one answer.
#[derive(Debug)]
pub struct Answer {
    pub revision: u64,
    /// Legacy path (relative to the generation) to record.
    pub records: Map<String, Value>,
    /// The `revision` of every row that has one, by the same path: the value a write expects.
    pub revisions: Map<String, Value>,
}

/// The `store_id` of the store when it is the authority of the state directory.
pub fn store_id(state: &Path) -> Option<String> {
    match epoch(state) {
        Epoch::Store(authority) => Some(authority.store_id),
        Epoch::FileLedger | Epoch::Inconsistent(_) => None,
    }
}

fn names(payload: &Value, key: &str) -> Result<Vec<String>, ReadError> {
    match payload.get(key) {
        None => Ok(Vec::new()),
        Some(Value::Array(items)) => items
            .iter()
            .map(|item| {
                item.as_str()
                    .map(str::to_owned)
                    .ok_or_else(|| ReadError::Malformed(format!("{key} must hold strings only")))
            })
            .collect(),
        Some(_) => Err(ReadError::Malformed(format!("{key} must be a list"))),
    }
}

pub(crate) fn valid_path(path: &str) -> bool {
    !path.is_empty()
        && !path.starts_with('/')
        && !path.contains('\\')
        && !path.split('/').any(|part| part.is_empty() || part == "..")
}

fn parse(body: &str) -> Result<Map<String, Value>, ReadError> {
    match serde_json::from_str::<Value>(body) {
        Ok(Value::Object(record)) => Ok(record),
        _ => Err(ReadError::Store {
            message: "a stored record is not a JSON object".to_owned(),
            busy: false,
        }),
    }
}

/// The records of an answer by path, and the revision of each row that has one.
#[derive(Default)]
struct Found {
    records: Map<String, Value>,
    revisions: Map<String, Value>,
}

impl Found {
    fn add(&mut self, path: String, record: Map<String, Value>, revision: Option<i64>) {
        if let Some(revision) = revision {
            self.revisions.insert(path.clone(), Value::from(revision));
        }
        self.records.insert(path, Value::Object(record));
    }
}

fn typed_rows(
    connection: &Connection,
    spec: &Spec,
    only: Option<&str>,
    found: &mut Found,
) -> Result<(), ReadError> {
    let sql = match only {
        None => spec.select_sql(),
        Some(_) => spec
            .select_sql()
            .replace("ORDER BY id", "WHERE id = ?1 ORDER BY id"),
    };
    let mut statement = connection.prepare(&sql)?;
    let mut rows = match only {
        None => statement.query([])?,
        Some(id) => statement.query([id])?,
    };
    while let Some(row) = rows.next()? {
        let (id, record) = spec.join(row)?;
        let revision = if spec.revisioned() {
            Some(row.get::<_, i64>(spec.typed.len() + 2)?)
        } else {
            None
        };
        found.add(format!("{}/{id}.json", spec.directory), record, revision);
    }
    Ok(())
}

/// The rows of `sql`, each as `(path, body, revision)`: the third column is optional.
fn table_rows(
    connection: &Connection,
    sql: &str,
    argument: Option<&str>,
    mut accept: impl FnMut(String, String, Option<i64>) -> Result<(), ReadError>,
) -> Result<(), ReadError> {
    let mut statement = connection.prepare(sql)?;
    let has_revision = statement.column_count() > 2;
    let mut rows = match argument {
        None => statement.query([])?,
        Some(value) => statement.query([value])?,
    };
    while let Some(row) = rows.next()? {
        let revision = if has_revision {
            Some(row.get(2)?)
        } else {
            None
        };
        accept(row.get(0)?, row.get(1)?, revision)?;
    }
    Ok(())
}

/// The audit chain in the form of the legacy audit files: `startup` and recovery look for the
/// decision of a transition in `audit/*.json`, and the chain holds it after the cutover.
fn chain_name(seq: i64) -> String {
    format!("audit-chain-{seq:012}")
}

fn chain_rows(
    connection: &Connection,
    only: Option<i64>,
    found: &mut Found,
) -> Result<(), ReadError> {
    let sql = match only {
        None => "SELECT seq, at, action, details_json, record_sha256 FROM audit ORDER BY seq",
        Some(_) => "SELECT seq, at, action, details_json, record_sha256 FROM audit WHERE seq = ?1",
    };
    let mut statement = connection.prepare(sql)?;
    let mut rows = match only {
        None => statement.query([])?,
        Some(seq) => statement.query([seq])?,
    };
    while let Some(row) = rows.next()? {
        let seq: i64 = row.get(0)?;
        let details: String = row.get(3)?;
        let mut record = Map::new();
        record.insert("audit_id".to_owned(), Value::String(chain_name(seq)));
        record.insert("at".to_owned(), Value::String(row.get(1)?));
        record.insert("action".to_owned(), Value::String(row.get(2)?));
        record.insert(
            "details".to_owned(),
            serde_json::from_str(&details).unwrap_or(Value::Null),
        );
        record.insert("record_sha256".to_owned(), Value::String(row.get(4)?));
        found.add(format!("audit/{}.json", chain_name(seq)), record, None);
    }
    Ok(())
}

fn collection(
    connection: &Connection,
    name: &str,
    selected: &str,
    found: &mut Found,
) -> Result<(), ReadError> {
    if let Some(spec) = spec_of(name) {
        return typed_rows(connection, spec, None, found);
    }
    if name == "audit" {
        let prefix = format!("generations/{selected}/");
        table_rows(
            connection,
            "SELECT path, CAST(bytes AS TEXT) FROM legacy_audit ORDER BY path",
            None,
            |path, text, _| {
                if let Some(relative) = path.strip_prefix(&prefix)
                    && let Ok(Value::Object(record)) = serde_json::from_str::<Value>(&text)
                {
                    found.add(relative.to_owned(), record, None);
                }
                Ok(())
            },
        )?;
        return chain_rows(connection, None, found);
    }
    if name == "qa-lane" {
        table_rows(
            connection,
            "SELECT 'qa-lane/lease.json', body_json, revision FROM qa_lane",
            None,
            |path, body, revision| {
                found.add(path, parse(&body)?, revision);
                Ok(())
            },
        )?;
        table_rows(
            connection,
            "SELECT 'qa-lane/queue/' || id || '.json', body_json, revision FROM qa_queue ORDER BY id",
            None,
            |path, body, revision| {
                found.add(path, parse(&body)?, revision);
                Ok(())
            },
        )?;
    }
    table_rows(
        connection,
        "SELECT path, body_json, revision FROM record WHERE collection = ?1 ORDER BY path",
        Some(name),
        |path, body, revision| {
            found.add(path, parse(&body)?, revision);
            Ok(())
        },
    )
}

fn one_path(
    connection: &Connection,
    path: &str,
    selected: &str,
    found: &mut Found,
) -> Result<(), ReadError> {
    let parts: Vec<&str> = path.split('/').collect();
    let stem = |name: &str| {
        name.strip_suffix(".json")
            .filter(|stem| !stem.is_empty())
            .map(str::to_owned)
    };
    match parts[..] {
        [directory, name] if spec_of(directory).is_some() => {
            if let (Some(spec), Some(id)) = (spec_of(directory), stem(name)) {
                typed_rows(connection, spec, Some(&id), found)?;
            }
            return Ok(());
        }
        ["qa-lane", "lease.json"] => {
            return table_rows(
                connection,
                "SELECT 'qa-lane/lease.json', body_json, revision FROM qa_lane",
                None,
                |path, body, revision| {
                    found.add(path, parse(&body)?, revision);
                    Ok(())
                },
            );
        }
        ["qa-lane", "queue", name] => {
            if let Some(id) = stem(name) {
                return table_rows(
                    connection,
                    "SELECT 'qa-lane/queue/' || id || '.json', body_json, revision FROM qa_queue WHERE id = ?1",
                    Some(&id),
                    |path, body, revision| {
                        found.add(path, parse(&body)?, revision);
                        Ok(())
                    },
                );
            }
        }
        ["audit", name] if name.starts_with("audit-chain-") => {
            let seq = name
                .strip_prefix("audit-chain-")
                .and_then(|rest| rest.strip_suffix(".json"))
                .and_then(|digits| digits.parse::<i64>().ok());
            if let Some(seq) = seq {
                chain_rows(connection, Some(seq), found)?;
            }
            return Ok(());
        }
        ["audit", name] if stem(name).is_some() => {
            let stored = format!("generations/{selected}/{path}");
            let text: Option<String> = connection
                .query_row(
                    "SELECT CAST(bytes AS TEXT) FROM legacy_audit WHERE path = ?1",
                    [&stored],
                    |row| row.get(0),
                )
                .optional()?;
            if let Some(text) = text
                && let Ok(Value::Object(record)) = serde_json::from_str::<Value>(&text)
            {
                found.add(path.to_owned(), record, None);
            }
            return Ok(());
        }
        _ => {}
    }
    table_rows(
        connection,
        "SELECT path, body_json, revision FROM record WHERE path = ?1",
        Some(path),
        |path, body, revision| {
            found.add(path, parse(&body)?, revision);
            Ok(())
        },
    )
}

fn meta(connection: &Connection, key: &str) -> Result<String, ReadError> {
    connection
        .query_row(
            "SELECT value FROM store_meta WHERE key = ?1",
            [key],
            |row| row.get(0),
        )
        .map_err(ReadError::from)
}

/// Answer one `state.read` request for the store of `state`.
///
/// The payload is `{"collections": [...], "paths": [...]}`: a collection is a record directory of the
/// legacy generation (`batches`, `dispatch-status`, `qa-lane`, `audit`, `reports`, ...), and a path
/// is one record file relative to the generation. A path that holds no record is left out.
///
/// # Errors
/// See [`ReadError`].
pub fn read(
    state: &Path,
    payload: Option<&Value>,
    expected_revision: Option<u64>,
) -> Result<Answer, ReadError> {
    match epoch(state) {
        Epoch::Store(_) => {}
        Epoch::FileLedger => {
            return Err(ReadError::NotAttached(
                "the file ledger is the authority of this repository".to_owned(),
            ));
        }
        Epoch::Inconsistent(reason) => return Err(ReadError::NotAttached(reason)),
    }
    let empty = Value::Object(Map::new());
    let payload = payload.unwrap_or(&empty);
    let Value::Object(members) = payload else {
        return Err(ReadError::Malformed(
            "the payload must be an object".to_owned(),
        ));
    };
    if let Some(unknown) = members
        .keys()
        .find(|key| !matches!(key.as_str(), "collections" | "paths"))
    {
        return Err(ReadError::Malformed(format!(
            "unknown payload field {unknown:?}"
        )));
    }
    let collections = names(payload, "collections")?;
    let paths = names(payload, "paths")?;
    if paths.len() > MAX_PATHS {
        return Err(ReadError::Malformed(format!(
            "at most {MAX_PATHS} paths per request"
        )));
    }
    if let Some(bad) = paths.iter().find(|path| !valid_path(path)) {
        return Err(ReadError::Malformed(format!(
            "{bad:?} is not a record path"
        )));
    }
    let connection = Connection::open_with_flags(
        state.join(STORE_NAME),
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    connection.busy_timeout(std::time::Duration::from_secs(5))?;
    connection.execute_batch("BEGIN")?;
    let revision: i64 =
        connection.query_row("SELECT COALESCE(MAX(seq), 0) FROM audit", [], |row| {
            row.get(0)
        })?;
    let revision = u64::try_from(revision).unwrap_or(0);
    if let Some(expected) = expected_revision
        && expected != revision
    {
        return Err(ReadError::RevisionConflict {
            expected,
            actual: revision,
        });
    }
    let selected = meta(&connection, "selected_generation")?;
    let mut found = Found::default();
    for name in &collections {
        collection(&connection, name, &selected, &mut found)?;
    }
    for path in &paths {
        one_path(&connection, path, &selected, &mut found)?;
    }
    connection.execute_batch("COMMIT")?;
    let Found { records, revisions } = found;
    let size: usize = records
        .iter()
        .map(|(path, record)| path.len() + record.to_string().len())
        .sum();
    if size > MAX_ANSWER_BYTES {
        return Err(ReadError::TooLarge);
    }
    Ok(Answer {
        revision,
        records,
        revisions,
    })
}
