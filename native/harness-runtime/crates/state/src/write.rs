//! The write operation of the store: what `state.write` of the IPC does (ADR 0020, sections 2 to
//! 4; ADR 0021, sections 3 and 4).
//!
//! A request carries the mutations of one command: a record to create, to replace or to delete,
//! by its path in the legacy generation, with the revision the command read. The whole request is
//! one `BEGIN IMMEDIATE` transaction, so a reader never sees half of it and two writers never
//! interleave. Inside it the rows are touched in the fixed order `batch -> qa_lane -> generation`
//! (the batch rows, then the QA lane, then the audit chain and the operation row), which is the
//! order every writer uses, so two transactions cannot wait for each other.
//!
//! The policy (which transition is allowed) stays in the Python coordinator. The store enforces
//! what it can prove: the revision of every row a command read, the immutability of evidence, the
//! CHECK and foreign-key invariants of the schema, one batch per request, and the audit chain,
//! one record per mutation. The operation row makes a repeat of a request return its stored
//! result instead of a second effect.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;
use std::time::Duration;

use contracts::{ErrorBody, ErrorCode, FailureDomain, Request};
use rusqlite::types::Value as Sql;
use rusqlite::{Connection, OpenFlags, OptionalExtension, Transaction, TransactionBehavior};
use serde_json::{Map, Value, json};

use crate::audit;
use crate::authority::{Epoch, STORE_NAME, epoch};
use crate::canonical::{canonical_json, sha256_hex};
use crate::error::Error;
use crate::hooks;
use crate::project::{BATCH, DISPATCH, DISPATCH_STATUS, PLAN, Spec};
use crate::read::valid_path;
use crate::route::{Target, classify};
use crate::schema;
use crate::time::iso_now;

/// The record directories a generation holds outside the typed tables and the audit chain.
const FREE_COLLECTIONS: [&str; 3] = ["reports", "qa-lane", "qa-artifacts"];
/// The collections whose records are evidence: written once, never replaced or deleted.
const EVIDENCE_COLLECTIONS: [&str; 2] = ["reports", "qa-artifacts"];
const MAX_MUTATIONS: usize = 1_000;

/// The order of the rows a transaction touches: the class of a mutation.
const CLASS_BATCH: &str = "batch";
const CLASS_QA_LANE: &str = "qa_lane";
const CLASS_GENERATION: &str = "generation";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Op {
    Create,
    CreateArtifact,
    Replace,
    Delete,
}

#[derive(Debug)]
struct Mutation {
    op: Op,
    path: String,
    content: Option<String>,
    expected_revision: Option<i64>,
    decision: Option<Value>,
    reason: Option<String>,
}

/// The place of the row a path names.
#[derive(Debug)]
enum Place {
    Typed(&'static Spec, String),
    QaLease,
    QaQueue(String),
    Record(String),
    Artifact,
}

impl Place {
    const fn class(&self) -> &'static str {
        match self {
            Self::QaLease | Self::QaQueue(_) => CLASS_QA_LANE,
            _ => CLASS_BATCH,
        }
    }
}

/// A mutation whose path and content were checked.
struct Planned {
    mutation: Mutation,
    place: Place,
    record: Option<Map<String, Value>>,
}

/// What a committed request did.
#[derive(Debug)]
pub struct Written {
    /// The `seq` of the last audit record: the revision of the store after the request.
    pub revision: u64,
    /// `{"revisions": {path: revision}, "audit_seq": n}`.
    pub result: Value,
    /// The classes of the rows the transaction touched, in order (a test reads it).
    pub touched: Vec<&'static str>,
}

fn failure(
    code: ErrorCode,
    domain: FailureDomain,
    retryable: bool,
    message: impl Into<String>,
    remedy: &str,
) -> ErrorBody {
    ErrorBody::new(code, domain, retryable, message, remedy)
}

fn malformed(detail: impl AsRef<str>) -> ErrorBody {
    failure(
        ErrorCode::MalformedRequest,
        FailureDomain::Tooling,
        false,
        format!("the request is not valid: {}", detail.as_ref()),
        "send a request the protocol defines: a known envelope, no unknown fields",
    )
}

fn revision_conflict(path: &str, expected: i64, actual: i64) -> ErrorBody {
    failure(
        ErrorCode::RevisionConflict,
        FailureDomain::Infrastructure,
        true,
        format!("another command changed {path} after this command read it"),
        "repeat the command: it reads the store again and applies the policy to the current state",
    )
    .with_fact("path", path)
    .with_fact("expected_revision", expected)
    .with_fact("actual_revision", actual)
}

fn store_busy(detail: &str) -> ErrorBody {
    failure(
        ErrorCode::StoreBusy,
        FailureDomain::Infrastructure,
        true,
        format!("the runtime store stayed locked longer than the limit: {detail}"),
        "repeat the command",
    )
}

fn invariant(detail: impl AsRef<str>) -> ErrorBody {
    failure(
        ErrorCode::InvariantViolation,
        FailureDomain::Tooling,
        false,
        format!("the change breaks a store invariant: {}", detail.as_ref()),
        "nothing was written; run `coordinator.py ledger validate --full` and report the defect",
    )
}

fn not_found(path: &str) -> ErrorBody {
    failure(
        ErrorCode::NotFound,
        FailureDomain::Tooling,
        false,
        format!("the store has no record at {path}"),
        "read the store again; the record the command changes does not exist",
    )
    .with_fact("path", path)
}

fn internal(detail: impl AsRef<str>) -> ErrorBody {
    failure(
        ErrorCode::Internal,
        FailureDomain::Infrastructure,
        false,
        format!("the store failed: {}", detail.as_ref()),
        "repeat the command; if the store stays unreadable, restore the state directory from the snapshot named in AUTHORITY.json",
    )
}

fn from_sqlite(err: &rusqlite::Error) -> ErrorBody {
    match err.sqlite_error_code() {
        Some(rusqlite::ErrorCode::DatabaseBusy | rusqlite::ErrorCode::DatabaseLocked) => {
            store_busy(&err.to_string())
        }
        Some(rusqlite::ErrorCode::ConstraintViolation) => invariant(err.to_string()),
        _ => internal(err.to_string()),
    }
}

fn from_state(err: &Error) -> ErrorBody {
    match err {
        Error::Refused {
            code,
            message,
            remedy,
        } => failure(
            *code,
            FailureDomain::Tooling,
            false,
            message.clone(),
            remedy,
        ),
        other => internal(other.to_string()),
    }
}

fn sqlite<T>(result: rusqlite::Result<T>) -> Result<T, ErrorBody> {
    result.map_err(|err| from_sqlite(&err))
}

/// The SHA-256 of a request without what changes between two attempts of one operation: its
/// id, its deadline and its protocol version. Two requests with the same `operation_id` and this
/// digest are one request.
pub fn request_digest(request: &Request) -> String {
    let envelope = json!({
        "operation": request.operation,
        "batch_id": request.batch_id,
        "dispatch_id": request.dispatch_id,
        "candidate_sha": request.candidate_sha,
        "plan_digest": request.plan_digest,
        "expected_revision": request.expected_revision,
        "fencing_token": request.fencing_token,
        "payload": request.payload,
    });
    sha256_hex(canonical_json(&envelope).as_bytes())
}

fn busy_timeout() -> Duration {
    #[cfg(debug_assertions)]
    if let Some(millis) = std::env::var("HARNESS_RUNTIME_TEST_BUSY_TIMEOUT_MS")
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
    {
        return Duration::from_millis(millis);
    }
    Duration::from_secs(5)
}

fn parse_mutation(index: usize, item: &Value) -> Result<Mutation, ErrorBody> {
    let at = |detail: &str| malformed(format!("mutation {index}: {detail}"));
    let Value::Object(members) = item else {
        return Err(at("must be an object"));
    };
    if let Some(unknown) = members.keys().find(|key| {
        !matches!(
            key.as_str(),
            "op" | "path" | "content" | "expected_revision" | "decision" | "reason"
        )
    }) {
        return Err(at(&format!("unknown field {unknown:?}")));
    }
    let text = |key: &str| match members.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(text)) => Ok(Some(text.clone())),
        Some(_) => Err(at(&format!("{key} must be a string"))),
    };
    let op = match members.get("op").and_then(Value::as_str) {
        Some("create") => Op::Create,
        Some("create_artifact") => Op::CreateArtifact,
        Some("replace") => Op::Replace,
        Some("delete") => Op::Delete,
        _ => {
            return Err(at("op must be create, create_artifact, replace or delete"));
        }
    };
    let Some(path) = text("path")? else {
        return Err(at("path is required"));
    };
    let content = text("content")?;
    let reason = text("reason")?;
    let expected_revision = match members.get("expected_revision") {
        None | Some(Value::Null) => None,
        Some(value) => Some(
            value
                .as_u64()
                .and_then(|revision| i64::try_from(revision).ok())
                .ok_or_else(|| at("expected_revision must be a non-negative integer"))?,
        ),
    };
    let decision = match members.get("decision") {
        None | Some(Value::Null) => None,
        Some(value) => Some(value.clone()),
    };
    match op {
        Op::Delete => {
            if content.is_some() || reason.is_none() {
                return Err(at("a delete has a reason and no content"));
            }
        }
        _ => {
            if content.is_none() {
                return Err(at("content is required"));
            }
        }
    }
    if decision.is_some() && op != Op::Replace {
        return Err(at("only a replace carries a decision"));
    }
    Ok(Mutation {
        op,
        path,
        content,
        expected_revision,
        decision,
        reason,
    })
}

fn parse_mutations(payload: Option<&Value>) -> Result<Vec<Mutation>, ErrorBody> {
    let Some(Value::Object(members)) = payload else {
        return Err(malformed("state.write needs a payload object"));
    };
    if let Some(unknown) = members.keys().find(|key| key.as_str() != "mutations") {
        return Err(malformed(format!("unknown payload field {unknown:?}")));
    }
    let Some(Value::Array(items)) = members.get("mutations") else {
        return Err(malformed("mutations must be a list"));
    };
    if items.is_empty() || items.len() > MAX_MUTATIONS {
        return Err(malformed(format!(
            "a request holds from 1 to {MAX_MUTATIONS} mutations"
        )));
    }
    items
        .iter()
        .enumerate()
        .map(|(index, item)| parse_mutation(index, item))
        .collect()
}

/// Check one mutation: the path, the canonical content and the row it names.
fn plan(mutation: Mutation) -> Result<Planned, ErrorBody> {
    let path = &mutation.path;
    if !valid_path(path) || path.split('/').count() < 2 {
        return Err(malformed(format!("{path:?} is not a record path")));
    }
    if path.starts_with("audit/") {
        return Err(malformed(
            "the audit chain is append-only: the store writes it, a command never does",
        ));
    }
    let json_path = path.ends_with(".json");
    let (place, record) = match (&mutation.content, json_path) {
        (Some(content), true) => {
            let record = match serde_json::from_str::<Value>(content) {
                Ok(Value::Object(record)) => record,
                _ => {
                    return Err(malformed(format!(
                        "the content of {path} is not a JSON object"
                    )));
                }
            };
            if canonical_json(&Value::Object(record.clone())) != *content {
                return Err(malformed(format!(
                    "the content of {path} is not the canonical JSON of the ledger"
                )));
            }
            let target = classify(path, content.as_bytes());
            (place_of(path, target)?, Some(record))
        }
        (None, true) => (place_of(path, classify(path, b"{}"))?, None),
        (_, false) => (place_of(path, Target::Artifact)?, None),
    };
    if mutation.op == Op::CreateArtifact && !matches!(place, Place::Record(_) | Place::Artifact) {
        return Err(malformed(format!("{path} is not a place for an artifact")));
    }
    Ok(Planned {
        mutation,
        place,
        record,
    })
}

fn place_of(path: &str, target: Target) -> Result<Place, ErrorBody> {
    let collection = path.split('/').next().unwrap_or_default();
    let free = |collection: &str| -> Result<(), ErrorBody> {
        if FREE_COLLECTIONS.contains(&collection) {
            Ok(())
        } else {
            Err(malformed(format!(
                "{path} is not in a record directory a command writes"
            )))
        }
    };
    match target {
        Target::Typed { spec, id, .. } => Ok(Place::Typed(spec, id)),
        Target::QaLease(_) => Ok(Place::QaLease),
        Target::QaQueue { id, .. } => Ok(Place::QaQueue(id)),
        Target::Record { collection, .. } => {
            free(&collection)?;
            Ok(Place::Record(collection))
        }
        Target::Artifact => {
            free(collection)?;
            Ok(Place::Artifact)
        }
        Target::Audit => Err(malformed("the audit chain is append-only")),
    }
}

/// The batch a row belongs to, when the row names one; `lookup` finds the batch of a dispatch.
fn batch_of(
    planned: &Planned,
    created: &BTreeMap<String, String>,
    tx: &Transaction<'_>,
) -> Result<Option<String>, ErrorBody> {
    let Place::Typed(spec, id) = &planned.place else {
        return Ok(None);
    };
    if spec.table == BATCH.table || spec.table == PLAN.table {
        return Ok(Some(id.clone()));
    }
    if spec.table == DISPATCH.table {
        return Ok(planned
            .record
            .as_ref()
            .and_then(|record| record.get("batch_id"))
            .and_then(Value::as_str)
            .map(str::to_owned));
    }
    if spec.table == DISPATCH_STATUS.table {
        if let Some(batch) = created.get(id) {
            return Ok(Some(batch.clone()));
        }
        return sqlite(
            tx.query_row("SELECT batch_id FROM dispatch WHERE id = ?1", [id], |row| {
                row.get(0)
            })
            .optional(),
        );
    }
    Ok(None)
}

/// One batch per request (ADR 0020, section 3).
fn check_one_batch(
    planned: &[Planned],
    request: &Request,
    tx: &Transaction<'_>,
) -> Result<(), ErrorBody> {
    let mut created = BTreeMap::new();
    for item in planned {
        if let (Place::Typed(spec, id), Op::Create) = (&item.place, item.mutation.op)
            && spec.table == DISPATCH.table
            && let Some(batch) = batch_of(item, &BTreeMap::new(), tx)?
        {
            created.insert(id.clone(), batch);
        }
    }
    let mut batches: BTreeSet<String> = BTreeSet::new();
    batches.extend(request.batch_id.clone());
    for item in planned {
        batches.extend(batch_of(item, &created, tx)?);
    }
    if batches.len() > 1 {
        return Err(malformed(format!(
            "a request writes the rows of one batch, and this one names {}",
            batches.len()
        )));
    }
    Ok(())
}

/// A row as the store holds it.
struct Stored {
    revision: i64,
    record: Map<String, Value>,
}

fn object(text: &str) -> Result<Map<String, Value>, ErrorBody> {
    match serde_json::from_str::<Value>(text) {
        Ok(Value::Object(record)) => Ok(record),
        _ => Err(invariant("a stored record is not a JSON object")),
    }
}

fn load(tx: &Transaction<'_>, place: &Place, path: &str) -> Result<Option<Stored>, ErrorBody> {
    let body_row = |sql: &str, key: Sql| -> Result<Option<Stored>, ErrorBody> {
        let found: Option<(String, i64)> = sqlite(
            tx.query_row(sql, [key], |row| Ok((row.get(0)?, row.get(1)?)))
                .optional(),
        )?;
        found
            .map(|(body, revision)| {
                Ok(Stored {
                    revision,
                    record: object(&body)?,
                })
            })
            .transpose()
    };
    match place {
        Place::Typed(spec, id) => {
            let sql = spec
                .select_sql()
                .replace("ORDER BY id", "WHERE id = ?1 ORDER BY id");
            let found = sqlite(
                tx.query_row(&sql, [id], |row| {
                    let (_, record) = spec.join(row)?;
                    let revision = if spec.revisioned() {
                        row.get::<_, i64>(spec.typed.len() + 2)?
                    } else {
                        1
                    };
                    Ok(Stored { revision, record })
                })
                .optional(),
            )?;
            Ok(found)
        }
        Place::QaLease => body_row(
            "SELECT body_json, revision FROM qa_lane WHERE id = ?1",
            Sql::Integer(1),
        ),
        Place::QaQueue(id) => body_row(
            "SELECT body_json, revision FROM qa_queue WHERE id = ?1",
            Sql::Text(id.clone()),
        ),
        Place::Record(_) => body_row(
            "SELECT body_json, revision FROM record WHERE path = ?1",
            Sql::Text(path.to_owned()),
        ),
        Place::Artifact => Ok(None),
    }
}

fn sha_of(record: &Map<String, Value>) -> String {
    sha256_hex(canonical_json(&Value::Object(record.clone())).as_bytes())
}

fn compact(record: &Map<String, Value>) -> String {
    Value::Object(record.clone()).to_string()
}

/// What a mutation did, for the audit chain and the answer.
struct Effect {
    action: &'static str,
    details: Value,
    /// The revision of the row after the mutation; `None` when the row is gone or has none.
    revision: Option<i64>,
}

struct Applying<'a> {
    tx: &'a Transaction<'a>,
    /// The paths this request touched already: a repeat touch carries no revision.
    seen: BTreeSet<String>,
}

impl Applying<'_> {
    fn check_revision(&self, item: &Planned, stored_revision: i64) -> Result<(), ErrorBody> {
        let path = &item.mutation.path;
        if self.seen.contains(path) {
            return Ok(());
        }
        match item.mutation.expected_revision {
            None => Err(malformed(format!(
                "the first touch of the existing row {path} needs its expected_revision"
            ))),
            Some(expected) if expected == stored_revision => Ok(()),
            Some(expected) => Err(revision_conflict(path, expected, stored_revision)),
        }
    }

    fn create(&self, item: &Planned) -> Result<Effect, ErrorBody> {
        let path = &item.mutation.path;
        let content = item.mutation.content.as_deref().unwrap_or_default();
        let artifact = item.mutation.op == Op::CreateArtifact;
        let action = if artifact {
            "immutable-artifact"
        } else {
            "immutable-record"
        };
        let sha256 = sha256_hex(content.as_bytes());
        let details = json!({"path": path, "sha256": sha256});
        if let Place::Artifact = item.place {
            return self.create_bytes(path, content, &sha256, details);
        }
        let existing = load(self.tx, &item.place, path)?;
        if let Some(stored) = existing {
            if artifact {
                // The same artifact twice is the same evidence; another content is a defect.
                if sha_of(&stored.record) == sha256 {
                    return Ok(Effect {
                        action,
                        details,
                        revision: Some(stored.revision),
                    });
                }
                return Err(invariant(format!(
                    "{path} is immutable evidence and holds other content"
                )));
            }
            return Err(revision_conflict(path, 0, stored.revision));
        }
        let record = item.record.clone().unwrap_or_default();
        match &item.place {
            Place::Typed(spec, id) => {
                let params = spec.split(id, record);
                sqlite(
                    self.tx
                        .execute(&spec.insert_sql(), rusqlite::params_from_iter(params)),
                )?;
            }
            Place::QaLease => {
                sqlite(self.tx.execute(
                    "INSERT INTO qa_lane (id, body_json) VALUES (1, ?1)",
                    [compact(&record)],
                ))?;
            }
            Place::QaQueue(id) => {
                sqlite(self.tx.execute(
                    "INSERT INTO qa_queue (id, body_json) VALUES (?1, ?2)",
                    rusqlite::params![id, compact(&record)],
                ))?;
            }
            Place::Record(collection) => {
                sqlite(self.tx.execute(
                    "INSERT INTO record (path, collection, body_json) VALUES (?1, ?2, ?3)",
                    rusqlite::params![path, collection, compact(&record)],
                ))?;
            }
            Place::Artifact => {}
        }
        Ok(Effect {
            action,
            details,
            revision: Some(1),
        })
    }

    fn create_bytes(
        &self,
        path: &str,
        content: &str,
        sha256: &str,
        details: Value,
    ) -> Result<Effect, ErrorBody> {
        let stored: Option<String> = sqlite(
            self.tx
                .query_row(
                    "SELECT sha256 FROM artifact WHERE path = ?1",
                    [path],
                    |row| row.get(0),
                )
                .optional(),
        )?;
        match stored {
            Some(found) if found == sha256 => {}
            Some(_) => {
                return Err(invariant(format!(
                    "{path} is immutable evidence and holds other content"
                )));
            }
            None => {
                sqlite(self.tx.execute(
                    "INSERT INTO artifact (path, bytes, sha256) VALUES (?1, ?2, ?3)",
                    rusqlite::params![path, content.as_bytes(), sha256],
                ))?;
            }
        }
        Ok(Effect {
            action: "immutable-artifact",
            details,
            revision: None,
        })
    }

    fn replace(&self, item: &Planned) -> Result<Effect, ErrorBody> {
        let path = &item.mutation.path;
        let immutable = match &item.place {
            Place::Typed(spec, _) => !spec.replaceable(),
            Place::Record(collection) => EVIDENCE_COLLECTIONS.contains(&collection.as_str()),
            Place::Artifact => true,
            Place::QaLease | Place::QaQueue(_) => false,
        };
        if immutable {
            return Err(invariant(format!("{path} is immutable evidence")));
        }
        let Some(stored) = load(self.tx, &item.place, path)? else {
            return Err(not_found(path));
        };
        self.check_revision(item, stored.revision)?;
        let record = item.record.clone().unwrap_or_default();
        match &item.place {
            Place::Typed(spec, id) => {
                let mut params = spec.split(id, record.clone());
                let id_param = params.remove(0);
                params.push(id_param);
                sqlite(
                    self.tx
                        .execute(&spec.update_sql(), rusqlite::params_from_iter(params)),
                )?;
            }
            Place::QaLease => {
                sqlite(self.tx.execute(
                    "UPDATE qa_lane SET body_json = ?1, revision = revision + 1 WHERE id = 1",
                    [compact(&record)],
                ))?;
            }
            Place::QaQueue(id) => {
                sqlite(self.tx.execute(
                    "UPDATE qa_queue SET body_json = ?1, revision = revision + 1 WHERE id = ?2",
                    rusqlite::params![compact(&record), id],
                ))?;
            }
            Place::Record(_) => {
                sqlite(self.tx.execute(
                    "UPDATE record SET body_json = ?1, revision = revision + 1 WHERE path = ?2",
                    rusqlite::params![compact(&record), path],
                ))?;
            }
            Place::Artifact => {}
        }
        let content = item.mutation.content.as_deref().unwrap_or_default();
        let mut details = Map::new();
        details.insert("path".to_owned(), json!(path));
        details.insert("before_sha256".to_owned(), json!(sha_of(&stored.record)));
        details.insert("sha256".to_owned(), json!(sha256_hex(content.as_bytes())));
        if path.starts_with("batches/") {
            details.insert(
                "from".to_owned(),
                stored.record.get("state").cloned().unwrap_or(Value::Null),
            );
            details.insert(
                "to".to_owned(),
                record.get("state").cloned().unwrap_or(Value::Null),
            );
        }
        if let Some(decision) = &item.mutation.decision {
            details.insert("decision".to_owned(), decision.clone());
        }
        Ok(Effect {
            action: "transition",
            details: Value::Object(details),
            revision: Some(stored.revision + 1),
        })
    }

    fn delete(&self, item: &Planned) -> Result<Effect, ErrorBody> {
        let path = &item.mutation.path;
        let deletable = match &item.place {
            Place::Record(collection) => !EVIDENCE_COLLECTIONS.contains(&collection.as_str()),
            Place::QaLease | Place::QaQueue(_) => true,
            Place::Typed(..) | Place::Artifact => false,
        };
        if !deletable {
            return Err(invariant(format!("{path} is immutable evidence")));
        }
        let Some(stored) = load(self.tx, &item.place, path)? else {
            return Err(not_found(path));
        };
        self.check_revision(item, stored.revision)?;
        match &item.place {
            Place::QaLease => {
                sqlite(self.tx.execute("DELETE FROM qa_lane WHERE id = 1", []))?;
            }
            Place::QaQueue(id) => {
                sqlite(self.tx.execute("DELETE FROM qa_queue WHERE id = ?1", [id]))?;
            }
            Place::Record(_) => {
                sqlite(
                    self.tx
                        .execute("DELETE FROM record WHERE path = ?1", [path]),
                )?;
            }
            Place::Typed(..) | Place::Artifact => {}
        }
        Ok(Effect {
            action: "deletion",
            details: json!({
                "path": path,
                "sha256": sha_of(&stored.record),
                "reason": item.mutation.reason,
            }),
            revision: None,
        })
    }

    fn apply(&mut self, item: &Planned) -> Result<Effect, ErrorBody> {
        let effect = match item.mutation.op {
            Op::Create | Op::CreateArtifact => self.create(item)?,
            Op::Replace => self.replace(item)?,
            Op::Delete => self.delete(item)?,
        };
        self.seen.insert(item.mutation.path.clone());
        Ok(effect)
    }
}

/// How a request ended inside its transaction.
enum Step {
    /// The request was applied; the transaction must commit.
    Done(Written),
    /// An earlier attempt of the same request was committed; nothing changed.
    Replayed(Written),
}

fn stored_result(result_json: &str) -> Result<Written, ErrorBody> {
    let result: Value = serde_json::from_str(result_json)
        .map_err(|err| internal(format!("a stored result is not JSON: {err}")))?;
    let revision = result
        .get("audit_seq")
        .and_then(Value::as_u64)
        .ok_or_else(|| internal("a stored result has no audit_seq"))?;
    Ok(Written {
        revision,
        result,
        touched: Vec::new(),
    })
}

/// The operation row of an earlier attempt: its stored result, its stored failure, or the
/// conflict of another request that used the same id.
fn replay(
    tx: &Transaction<'_>,
    operation_id: &str,
    digest: &str,
) -> Result<Option<Written>, ErrorBody> {
    let found: Option<(String, String, Option<String>)> = sqlite(
        tx.query_row(
            "SELECT request_digest, state, result_json FROM operations WHERE operation_id = ?1",
            [operation_id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .optional(),
    )?;
    let Some((stored_digest, state, result_json)) = found else {
        return Ok(None);
    };
    if stored_digest != digest {
        return Err(failure(
            ErrorCode::IdempotencyConflict,
            FailureDomain::Tooling,
            false,
            format!("the operation id {operation_id} was used for another request"),
            "this operation id was used for another request; repeat the command, it derives a new id from the current revision",
        ));
    }
    let result_json = result_json.unwrap_or_default();
    match state.as_str() {
        "committed" => stored_result(&result_json).map(Some),
        "failed" => Err(serde_json::from_str::<ErrorBody>(&result_json)
            .map_err(|err| internal(format!("a stored failure is not an error: {err}")))?),
        _ => Err(store_busy("the operation is still being applied")),
    }
}

fn run(
    tx: &Transaction<'_>,
    request: &Request,
    digest: &str,
    mutations: Vec<Mutation>,
) -> Result<Step, ErrorBody> {
    if let Some(written) = replay(tx, &request.operation_id, digest)? {
        return Ok(Step::Replayed(written));
    }
    if let (Some(expected), Some(batch)) = (request.expected_revision, &request.batch_id) {
        let path = format!("batches/{batch}.json");
        let actual: i64 = sqlite(
            tx.query_row("SELECT revision FROM batch WHERE id = ?1", [batch], |row| {
                row.get(0)
            })
            .optional(),
        )?
        .unwrap_or(0);
        let expected = i64::try_from(expected).unwrap_or(i64::MAX);
        if expected != actual {
            return Err(revision_conflict(&path, expected, actual));
        }
    }
    let planned = mutations
        .into_iter()
        .map(plan)
        .collect::<Result<Vec<_>, _>>()?;
    check_one_batch(&planned, request, tx)?;

    // The rows are touched class by class; inside a class the order of the request holds.
    let mut order: Vec<usize> = (0..planned.len()).collect();
    order.sort_by_key(|index| u8::from(planned[*index].place.class() == CLASS_QA_LANE));
    let mut applying = Applying {
        tx,
        seen: BTreeSet::new(),
    };
    let mut effects: Vec<Option<Effect>> = planned.iter().map(|_| None).collect();
    let mut touched: Vec<&'static str> = Vec::new();
    let mut revisions = Map::new();
    for index in order {
        let item = &planned[index];
        let effect = applying.apply(item)?;
        touched.push(item.place.class());
        match effect.revision {
            Some(revision) => {
                revisions.insert(item.mutation.path.clone(), json!(revision));
            }
            None => {
                revisions.remove(&item.mutation.path);
            }
        }
        effects[index] = Some(effect);
    }

    // The generation class: one audit record per mutation, in the order of the request.
    let at = iso_now();
    let mut audit_seq = 0;
    for effect in effects.into_iter().flatten() {
        audit_seq = sqlite_audit(tx, &at, &effect)?;
    }
    touched.push(CLASS_GENERATION);
    let result = json!({"revisions": revisions, "audit_seq": audit_seq});
    sqlite(tx.execute(
        "INSERT INTO operations (operation_id, operation, request_digest, state, result_json, created_at, completed_at)
         VALUES (?1, ?2, ?3, 'committed', ?4, ?5, ?5)",
        rusqlite::params![request.operation_id, request.operation, digest, result.to_string(), at],
    ))?;
    Ok(Step::Done(Written {
        revision: u64::try_from(audit_seq).unwrap_or(0),
        result,
        touched,
    }))
}

fn sqlite_audit(tx: &Transaction<'_>, at: &str, effect: &Effect) -> Result<i64, ErrorBody> {
    audit::append(tx, at, effect.action, &effect.details).map_err(|err| match err {
        Error::Interrupted(message) => internal(message),
        other => from_state(&other),
    })
}

fn open(state: &Path) -> Result<Connection, ErrorBody> {
    let mut connection = sqlite(Connection::open_with_flags(
        state.join(STORE_NAME),
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    ))?;
    sqlite(connection.busy_timeout(busy_timeout()))?;
    sqlite(connection.execute_batch("PRAGMA synchronous = FULL; PRAGMA foreign_keys = ON;"))?;
    schema::upgrade(&mut connection).map_err(|err| from_state(&err))?;
    Ok(connection)
}

/// Remember a deterministic refusal, so a repeat of the same operation gets the same error. The
/// record is best effort: a repeat that finds none is refused by the same checks again.
fn record_failure(connection: &mut Connection, request: &Request, digest: &str, error: &ErrorBody) {
    let Ok(body) = serde_json::to_string(error) else {
        return;
    };
    let at = iso_now();
    let Ok(tx) = connection.transaction_with_behavior(TransactionBehavior::Immediate) else {
        return;
    };
    let inserted = tx.execute(
        "INSERT OR IGNORE INTO operations (operation_id, operation, request_digest, state, result_json, created_at, completed_at)
         VALUES (?1, ?2, ?3, 'failed', ?4, ?5, ?5)",
        rusqlite::params![request.operation_id, request.operation, digest, body, at],
    );
    if inserted.is_ok() {
        let _ = tx.commit();
    }
}

fn is_recorded(code: ErrorCode) -> bool {
    matches!(
        code,
        ErrorCode::RevisionConflict | ErrorCode::InvariantViolation
    )
}

/// Apply one `state.write` request to the store of `state`.
///
/// # Errors
/// The typed error the response carries: `malformed_request`, `not_found`, `revision_conflict`,
/// `idempotency_conflict`, `store_busy`, `invariant_violation` or `internal`.
pub fn write(state: &Path, request: &Request) -> Result<Written, ErrorBody> {
    match epoch(state) {
        Epoch::Store(_) => {}
        Epoch::FileLedger => {
            return Err(not_attached(
                "the file ledger is the authority of this repository",
            ));
        }
        Epoch::Inconsistent(reason) => return Err(not_attached(&reason)),
    }
    let mutations = parse_mutations(request.payload.as_ref())?;
    let digest = request_digest(request);
    hooks::reach_once("state.write:before-begin").map_err(|err| internal(err.to_string()))?;
    let mut connection = open(state)?;
    let outcome = transact(&mut connection, request, &digest, mutations);
    // The transaction is rolled back by now; a refusal that a repeat would meet again is kept.
    if let Err(error) = &outcome
        && is_recorded(error.code)
    {
        record_failure(&mut connection, request, &digest, error);
    }
    outcome
}

fn transact(
    connection: &mut Connection,
    request: &Request,
    digest: &str,
    mutations: Vec<Mutation>,
) -> Result<Written, ErrorBody> {
    let tx = sqlite(connection.transaction_with_behavior(TransactionBehavior::Immediate))?;
    hooks::reach_once("state.write:in-transaction").map_err(|err| internal(err.to_string()))?;
    match run(&tx, request, digest, mutations)? {
        Step::Done(written) => {
            sqlite(tx.commit())?;
            Ok(written)
        }
        Step::Replayed(written) => Ok(written),
    }
}

fn not_attached(reason: &str) -> ErrorBody {
    failure(
        ErrorCode::NotFound,
        FailureDomain::Policy,
        false,
        format!("the runtime store is not the authority of this repository: {reason}"),
        "run `harness-runtime migrate --repo <repo>`, or use the installation that matches the state",
    )
}
