//! The executions of the process supervisor in the store (ADR 0021, section 6; ADR 0022,
//! section 2).
//!
//! An execution is one launch of a dispatch: its row is written by the runtime only, and the
//! denormalised `dispatch_status.execution_state` follows it in the same transaction. Rust never
//! writes a `DispatchState`; the coordinator chooses one from the observed execution.
//!
//! The states are `launching -> running -> stopping -> exited | lost | unknown`. Only `exited` and
//! `lost` are terminal; `unknown` is not, and only a repeated observation moves it to `running`,
//! `exited` or `lost`. `allowed` is the matrix; the trigger `executions_transition` of the schema
//! holds the same one. Every transition is one row update with a compare-and-set on `revision`,
//! the new `execution_state` of the dispatch, one audit record `execution-<state>` with the facts
//! of the transition, and the checkpoint moved to the new tail of the chain. The lease of an
//! execution is released only in the transaction that makes it terminal.

use std::path::Path;
use std::time::Duration;

use contracts::{ErrorBody, ErrorCode, ExecutionState, FailureDomain};
use rusqlite::{Connection, OpenFlags, OptionalExtension, Row, Transaction, TransactionBehavior};
use serde_json::{Map, Value, json};

use crate::audit;
use crate::authority::{Epoch, STORE_NAME, epoch};
use crate::identity::{Identity, Verdict};
use crate::schema;
use crate::write::{failure, sqlite};

/// Whether no transition leaves `state`.
pub const fn is_terminal(state: ExecutionState) -> bool {
    matches!(state, ExecutionState::Exited | ExecutionState::Lost)
}

/// Whether an execution may move from `from` to `to`.
pub const fn allowed(from: ExecutionState, to: ExecutionState) -> bool {
    use ExecutionState::{Exited, Launching, Lost, Running, Stopping, Unknown};
    matches!(
        (from, to),
        (Launching, Running | Exited | Lost | Unknown)
            | (Running, Stopping | Exited | Lost | Unknown)
            | (Stopping, Exited | Lost | Unknown)
            | (Unknown, Running | Exited | Lost)
    )
}

/// The state an observation of the recorded process means. `exit_evidence` says whether this
/// runtime holds the proof of the exit: the wait status of its own child, or the stop it sent.
/// A dead process without that proof is `lost`; a reused pid proves the recorded process gone,
/// and the process that holds the pid now is never touched. Another boot, PID namespace or host,
/// or an observation that failed, is `unknown`: alive for every decision.
pub const fn observed(verdict: Verdict, exit_evidence: bool) -> ExecutionState {
    match verdict {
        Verdict::Alive => ExecutionState::Running,
        Verdict::Dead if exit_evidence => ExecutionState::Exited,
        Verdict::Dead | Verdict::PidReused => ExecutionState::Lost,
        Verdict::OtherEnvironment | Verdict::OnAnotherHost | Verdict::Unknown => {
            ExecutionState::Unknown
        }
    }
}

/// The state of a stored text, or `None` outside the vocabulary.
pub fn parse_state(text: &str) -> Option<ExecutionState> {
    ExecutionState::ALL
        .into_iter()
        .find(|state| state.as_str() == text)
}

/// One row of `executions`.
#[derive(Clone, Debug, PartialEq)]
pub struct Execution {
    pub id: String,
    pub dispatch_id: String,
    pub attempt: i64,
    pub state: ExecutionState,
    pub revision: i64,
    /// `process`, `container` or `qa`.
    pub kind: String,
    /// The engine of a container or QA execution: every engine command of the row uses it.
    pub engine: Option<String>,
    pub container_id: Option<String>,
    pub launch: Value,
    pub execution_deadline: String,
    pub stop_grace_seconds: i64,
    pub poll_interval_seconds: i64,
    pub identity: Option<Identity>,
    pub pgid: Option<i64>,
    pub receipt: Option<Value>,
    pub exit: Option<Value>,
    pub observation: Option<Value>,
    pub lease_owner: Value,
    pub lease_released_at: Option<String>,
    pub created_at: String,
    pub updated_at: String,
}

const COLUMNS: &str = "id, dispatch_id, attempt, state, revision, launch_json, execution_deadline,
    stop_grace_seconds, poll_interval_seconds, host, boot_id, pid_namespace, pid, start_time, pgid,
    receipt_json, exit_json, observation_json, lease_owner_json, lease_released_at, created_at,
    updated_at, kind, engine, container_id";

/// The kind of an execution of a host process.
pub const KIND_PROCESS: &str = "process";
/// The kind of an execution of a worker container.
pub const KIND_CONTAINER: &str = "container";
/// The kind of one attempt of a QA run: a container per command (ADR 0022, section 3).
pub const KIND_QA: &str = "qa";

fn json_column(text: Option<String>) -> Option<Value> {
    text.and_then(|text| serde_json::from_str(&text).ok())
}

impl Execution {
    fn from_row(row: &Row<'_>) -> rusqlite::Result<Self> {
        let state: String = row.get(3)?;
        let host: Option<String> = row.get(9)?;
        let pid: Option<i64> = row.get(12)?;
        let start_time: Option<String> = row.get(13)?;
        let identity = match (host, pid, start_time) {
            (Some(host), Some(pid), Some(start_time)) => Some(Identity {
                host,
                boot_id: row.get(10)?,
                pid_namespace: row.get(11)?,
                pid,
                start_time,
            }),
            _ => None,
        };
        Ok(Self {
            id: row.get(0)?,
            dispatch_id: row.get(1)?,
            attempt: row.get(2)?,
            state: parse_state(&state).ok_or_else(|| {
                rusqlite::Error::FromSqlConversionFailure(
                    3,
                    rusqlite::types::Type::Text,
                    format!("unknown execution state {state:?}").into(),
                )
            })?,
            revision: row.get(4)?,
            kind: row.get(22)?,
            engine: row.get(23)?,
            container_id: row.get(24)?,
            launch: json_column(row.get(5)?).unwrap_or(Value::Null),
            execution_deadline: row.get(6)?,
            stop_grace_seconds: row.get(7)?,
            poll_interval_seconds: row.get(8)?,
            identity,
            pgid: row.get(14)?,
            receipt: json_column(row.get(15)?),
            exit: json_column(row.get(16)?),
            observation: json_column(row.get(17)?),
            lease_owner: json_column(row.get(18)?).unwrap_or(Value::Null),
            lease_released_at: row.get(19)?,
            created_at: row.get(20)?,
            updated_at: row.get(21)?,
        })
    }

    /// The row as the answer of `execution.observe` and `dispatch status` shows it.
    pub fn to_json(&self) -> Value {
        json!({
            "execution_id": self.id,
            "dispatch_id": self.dispatch_id,
            "attempt": self.attempt,
            "state": self.state.as_str(),
            "revision": self.revision,
            "kind": self.kind,
            "engine": self.engine,
            "container_id": self.container_id,
            "launch": self.launch,
            "execution_deadline": self.execution_deadline,
            "stop_grace_seconds": self.stop_grace_seconds,
            "poll_interval_seconds": self.poll_interval_seconds,
            "identity": self.identity.as_ref().map(Identity::to_json),
            "pgid": self.pgid,
            "receipt": self.receipt,
            "exit": self.exit,
            "observation": self.observation,
            "lease": {"owner": self.lease_owner, "released_at": self.lease_released_at},
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        })
    }
}

fn one(connection: &Connection, filter: &str, key: &str) -> Result<Option<Execution>, ErrorBody> {
    sqlite(
        connection
            .query_row(
                &format!("SELECT {COLUMNS} FROM executions WHERE {filter}"),
                [key],
                Execution::from_row,
            )
            .optional(),
    )
}

/// The execution with the id `id`.
///
/// # Errors
/// A store that cannot be read.
pub fn load(connection: &Connection, id: &str) -> Result<Option<Execution>, ErrorBody> {
    one(connection, "id = ?1", id)
}

/// The latest execution of the dispatch `dispatch_id`: its highest attempt. A worker dispatch has
/// only attempt 1; a QA dispatch has one execution per attempt.
///
/// # Errors
/// A store that cannot be read.
pub fn of_dispatch(
    connection: &Connection,
    dispatch_id: &str,
) -> Result<Option<Execution>, ErrorBody> {
    one(
        connection,
        "dispatch_id = ?1 ORDER BY attempt DESC LIMIT 1",
        dispatch_id,
    )
}

/// Every execution that is not terminal, by id.
///
/// # Errors
/// A store that cannot be read.
pub fn unfinished(connection: &Connection) -> Result<Vec<Execution>, ErrorBody> {
    let mut statement = sqlite(connection.prepare(&format!(
        "SELECT {COLUMNS} FROM executions WHERE state NOT IN ('exited', 'lost') ORDER BY id"
    )))?;
    let rows = sqlite(statement.query_map([], Execution::from_row))?;
    sqlite(rows.collect())
}

/// A new execution in the state `launching`: the intent of a launch.
pub struct NewExecution<'a> {
    pub id: &'a str,
    pub dispatch_id: &'a str,
    /// The attempt of the dispatch: 1 for a worker, the attempt of a QA run.
    pub attempt: i64,
    /// `process`, `container` or `qa`.
    pub kind: &'a str,
    /// The engine of a container or QA run; `None` for a process.
    pub engine: Option<&'a str>,
    pub launch: &'a Value,
    pub execution_deadline: &'a str,
    pub stop_grace_seconds: i64,
    pub poll_interval_seconds: i64,
    pub lease_owner: &'a Value,
}

/// What a transition writes besides the state. A field that is `None` keeps the stored value.
#[derive(Clone, Debug, Default)]
pub struct Change {
    pub identity: Option<Identity>,
    /// The id of the worker container of a container execution.
    pub container_id: Option<String>,
    pub pgid: Option<i64>,
    pub receipt: Option<Value>,
    pub exit: Option<Value>,
    pub observation: Option<Value>,
    pub lease_owner: Option<Value>,
    /// What the audit record says about the transition; `null` when there is nothing to add.
    pub facts: Value,
}

fn invariant(message: impl Into<String>) -> ErrorBody {
    failure(
        ErrorCode::InvariantViolation,
        FailureDomain::Tooling,
        false,
        message,
        "nothing was written; run `coordinator.py ledger validate --full` and report the defect",
    )
}

/// One audit record of a transition, and the checkpoint moved to it: the transaction verified
/// the chain up to its old tail before it appended.
fn record(
    tx: &Connection,
    at: &str,
    execution: &Execution,
    from: Option<ExecutionState>,
    facts: &Value,
) -> Result<(), ErrorBody> {
    let to = execution.state;
    let details = json!({
        "execution_id": execution.id,
        "dispatch_id": execution.dispatch_id,
        "from": from.map(ExecutionState::as_str),
        "to": to.as_str(),
        "facts": facts,
    });
    let seq = audit::append(tx, at, &format!("execution-{}", to.as_str()), &details)
        .map_err(|err| invariant(format!("the audit record cannot be written: {err}")))?;
    let chain: String = sqlite(tx.query_row(
        "SELECT chain_sha256 FROM audit WHERE seq = ?1",
        [seq],
        |row| row.get(0),
    ))?;
    sqlite(audit::move_checkpoint(tx, seq, &chain))?;
    sqlite(tx.execute(
        "UPDATE dispatch_status SET execution_state = ?1 WHERE id = ?2",
        [to.as_str(), execution.dispatch_id.as_str()],
    ))?;
    Ok(())
}

/// Write the intent of a launch: the row in `launching`, the `execution_state` of its dispatch
/// and the audit record `execution-launching`.
///
/// # Errors
/// `invariant_violation` for a row the schema refuses (a second execution of the dispatch, an
/// unknown dispatch); a store that fails.
pub fn insert(tx: &Connection, new: &NewExecution<'_>, at: &str) -> Result<Execution, ErrorBody> {
    sqlite(tx.execute(
        "INSERT INTO executions (id, dispatch_id, attempt, state, launch_json, execution_deadline,
             stop_grace_seconds, poll_interval_seconds, lease_owner_json, created_at, updated_at,
             kind, engine)
         VALUES (?1, ?2, ?11, 'launching', ?3, ?4, ?5, ?6, ?7, ?8, ?8, ?9, ?10)",
        rusqlite::params![
            new.id,
            new.dispatch_id,
            new.launch.to_string(),
            new.execution_deadline,
            new.stop_grace_seconds,
            new.poll_interval_seconds,
            new.lease_owner.to_string(),
            at,
            new.kind,
            new.engine,
            new.attempt
        ],
    ))?;
    let execution =
        load(tx, new.id)?.ok_or_else(|| invariant("the new execution is not stored"))?;
    record(tx, at, &execution, None, &Value::Null)?;
    Ok(execution)
}

/// Move `current` to `to` with `change`, if nobody changed the row since `current` was read. A
/// terminal state releases the lease in the same update.
///
/// # Errors
/// `invariant_violation` for a transition the matrix or the schema refuses; `revision_conflict`
/// when the row is no longer at the revision of `current`; a store that fails.
pub fn transition(
    tx: &Connection,
    current: &Execution,
    to: ExecutionState,
    change: Change,
    at: &str,
) -> Result<Execution, ErrorBody> {
    if !allowed(current.state, to) {
        return Err(invariant(format!(
            "the execution {} cannot move from {} to {}",
            current.id,
            current.state.as_str(),
            to.as_str()
        )));
    }
    let identity = change.identity.as_ref();
    let text = |value: Option<&Value>| value.map(Value::to_string);
    let changed = sqlite(tx.execute(
        "UPDATE executions SET state = ?3, revision = revision + 1,
             host = COALESCE(?4, host), boot_id = CASE WHEN ?4 IS NULL THEN boot_id ELSE ?5 END,
             pid_namespace = CASE WHEN ?4 IS NULL THEN pid_namespace ELSE ?6 END,
             pid = COALESCE(?7, pid), start_time = COALESCE(?8, start_time),
             pgid = COALESCE(?9, pgid), receipt_json = COALESCE(?10, receipt_json),
             exit_json = COALESCE(?11, exit_json),
             observation_json = COALESCE(?12, observation_json),
             lease_owner_json = COALESCE(?13, lease_owner_json),
             lease_released_at = ?14, updated_at = ?15,
             container_id = COALESCE(?16, container_id)
         WHERE id = ?1 AND revision = ?2",
        rusqlite::params![
            current.id,
            current.revision,
            to.as_str(),
            identity.map(|identity| identity.host.clone()),
            identity.and_then(|identity| identity.boot_id.clone()),
            identity.and_then(|identity| identity.pid_namespace.clone()),
            identity.map(|identity| identity.pid),
            identity.map(|identity| identity.start_time.clone()),
            change.pgid,
            text(change.receipt.as_ref()),
            text(change.exit.as_ref()),
            text(change.observation.as_ref()),
            text(change.lease_owner.as_ref()),
            is_terminal(to).then_some(at),
            at,
            change.container_id
        ],
    ))?;
    if changed == 0 {
        let actual = load(tx, &current.id)?.map_or(0, |stored| stored.revision);
        return Err(failure(
            ErrorCode::RevisionConflict,
            FailureDomain::Infrastructure,
            true,
            format!("another writer changed the execution {}", current.id),
            "repeat the command: it observes the execution again",
        )
        .with_fact("expected_revision", current.revision)
        .with_fact("actual_revision", actual));
    }
    let execution =
        load(tx, &current.id)?.ok_or_else(|| invariant("the execution is no longer stored"))?;
    record(tx, at, &execution, Some(current.state), &change.facts)?;
    Ok(execution)
}

/// A legacy record: a JSON object.
pub type Record = Map<String, Value>;

/// The stored brief `dispatches/<id>.json` and status `dispatch-status/<id>.json` of a dispatch,
/// as the legacy records; `None` when the store has either not.
///
/// # Errors
/// A store that cannot be read.
pub fn dispatch_records(
    connection: &Connection,
    dispatch_id: &str,
) -> Result<Option<(Record, Record)>, ErrorBody> {
    let record = |spec: &crate::project::Spec| -> Result<Option<Record>, ErrorBody> {
        let sql = spec
            .select_sql()
            .replace("ORDER BY id", "WHERE id = ?1 ORDER BY id");
        sqlite(
            connection
                .query_row(&sql, [dispatch_id], |row| spec.join(row))
                .optional(),
        )
        .map(|found| found.map(|(_, record)| record))
    };
    let brief = record(&crate::project::DISPATCH)?;
    let status = record(&crate::project::DISPATCH_STATUS)?;
    Ok(brief.zip(status))
}

/// Open the store of `state` for the supervisor: the store must be the authority; an older schema
/// is upgraded, and a store without a checkpoint gets one.
///
/// # Errors
/// `not_found` when the file ledger is the authority; a refusal of the schema; a broken chain; a
/// store that fails.
pub fn connect(state: &Path) -> Result<Connection, ErrorBody> {
    if !matches!(epoch(state), Epoch::Store(_)) {
        return Err(failure(
            ErrorCode::NotFound,
            FailureDomain::Policy,
            false,
            "the runtime store is not the authority of this repository",
            "run `harness-runtime migrate --repo <repo>`, or use the installation that matches the state",
        ));
    }
    let mut connection = sqlite(Connection::open_with_flags(
        state.join(STORE_NAME),
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    ))?;
    sqlite(connection.busy_timeout(Duration::from_secs(5)))?;
    sqlite(connection.execute_batch("PRAGMA synchronous = FULL; PRAGMA foreign_keys = ON;"))?;
    schema::upgrade(&mut connection).map_err(|err| match err {
        crate::Error::Refused {
            code,
            message,
            remedy,
        } => failure(code, FailureDomain::Tooling, false, message, &remedy),
        other => invariant(other.to_string()),
    })?;
    audit::ensure_checkpoint(&mut connection).map_err(|broken| broken.to_error())?;
    Ok(connection)
}

/// Begin the write transaction of one transition: `BEGIN IMMEDIATE`, then the audit records
/// after the checkpoint are verified, as every ordinary command does (ADR 0020, section 4).
///
/// # Errors
/// `store_busy` past the busy timeout; a broken chain.
pub fn begin(connection: &mut Connection) -> Result<Transaction<'_>, ErrorBody> {
    let tx = sqlite(connection.transaction_with_behavior(TransactionBehavior::Immediate))?;
    audit::verify_tail(&tx).map_err(|broken| broken.to_error())?;
    Ok(tx)
}
