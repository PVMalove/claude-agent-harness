//! The Linux launch, adoption and reconcile of the supervisor.
//!
//! Serialisation: one mutex per operation id. A launch holds it from the intent to the receipt;
//! reconcile, `execution.observe` and the watcher of a child take it too, and every row write is a
//! compare-and-set on the revision of the row. A second request with the same id waits for the
//! mutex until its `request_deadline`, then answers what the first one stored.
//!
//! Debug-build hooks of a launch, in order (`HARNESS_RUNTIME_TEST_PAUSE_AT` and
//! `HARNESS_RUNTIME_TEST_FAIL_AT`, once per process): `execution.launch:before-begin` (nothing is
//! written), `execution.launch:in-transaction` (the intent is rolled back),
//! `execution.launch:intent` (the intent is committed, no process), `execution.launch:launched`
//! (the process runs, no receipt), `execution.launch:receipt` (the receipt write fails) and
//! `execution.launch:respond` (in the IPC server: the receipt is committed, the answer dropped).

use std::collections::{HashMap, HashSet};
use std::os::unix::fs::DirBuilderExt;
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::Child;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError, TryLockError};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use contracts::{ErrorBody, ErrorCode, ExecutionState, FailureDomain, Request};
use rusqlite::{Connection, OptionalExtension};
use serde_json::{Map, Value, json};
use state::canonical::{canonical_json, object_sha256, sha256_hex};
use state::executions::{self, Change, Execution, NewExecution, allowed, is_terminal};
use state::hooks;
use state::identity::{self, Identity};
use state::time::{iso_from_micros, iso_now, now_micros};

use crate::Supervisor;
use crate::process::{self, Spec};

/// How often a watcher looks at its child.
const WAIT_POLL: Duration = Duration::from_millis(100);
/// The periodic reconcile when no execution pins its own interval.
const DEFAULT_POLL_SECONDS: i64 = 1;

/// What the supervisor holds in memory.
#[derive(Debug, Default)]
pub(crate) struct Inner {
    locks: Mutex<HashMap<String, Arc<Mutex<()>>>>,
    /// Children spawned by this runtime whose receipt is not written yet.
    children: Mutex<HashMap<String, Child>>,
    /// Executions whose child a watcher thread of this runtime waits for.
    watched: Mutex<HashSet<String>>,
}

fn guard<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| {
            u64::try_from(elapsed.as_millis()).unwrap_or(u64::MAX)
        })
}

pub(crate) fn sql<T>(result: rusqlite::Result<T>) -> Result<T, ErrorBody> {
    result.map_err(|err| match err.sqlite_error_code() {
        Some(rusqlite::ErrorCode::DatabaseBusy | rusqlite::ErrorCode::DatabaseLocked) => {
            store_busy(&err.to_string())
        }
        _ => ErrorBody::new(
            ErrorCode::Internal,
            FailureDomain::Infrastructure,
            false,
            format!("the store failed: {err}"),
            "repeat the command; if the store stays unreadable, restore the state directory from the snapshot named in AUTHORITY.json",
        ),
    })
}

fn store_busy(detail: &str) -> ErrorBody {
    ErrorBody::new(
        ErrorCode::StoreBusy,
        FailureDomain::Infrastructure,
        true,
        format!("the execution is busy: {detail}"),
        "repeat the command; a launch is never repeated, a repeat answers what it stored",
    )
}

fn injected(err: &state::Error) -> ErrorBody {
    ErrorBody::new(
        ErrorCode::Internal,
        FailureDomain::Infrastructure,
        true,
        err.to_string(),
        "repeat the command; a launch is never repeated, a repeat answers what it stored",
    )
}

fn malformed(detail: &str) -> ErrorBody {
    ErrorBody::new(
        ErrorCode::MalformedRequest,
        FailureDomain::Tooling,
        false,
        format!("the request is not valid: {detail}"),
        "send {\"dispatch_id\": <id>} as the payload of the execution operation",
    )
}

fn denied(detail: impl Into<String>) -> ErrorBody {
    ErrorBody::new(
        ErrorCode::PolicyDenied,
        FailureDomain::Policy,
        false,
        detail,
        "launch only a dispatched external dispatch whose approved brief pins `execution` and whose status names its launch; nothing was written",
    )
}

/// The `dispatch_id` of the payload `{"dispatch_id": <text>}`.
fn dispatch_id_of(request: &Request) -> Result<String, ErrorBody> {
    match request.payload.as_ref() {
        Some(Value::Object(members)) if members.len() == 1 => members
            .get("dispatch_id")
            .and_then(Value::as_str)
            .filter(|id| !id.is_empty())
            .map(str::to_owned)
            .ok_or_else(|| malformed("the payload names no dispatch_id")),
        _ => Err(malformed("the payload is {\"dispatch_id\": <id>}")),
    }
}

/// The deterministic operation id of the first launch of a brief (ADR 0021, section 4).
pub fn operation_id(dispatch_id: &str, brief_sha256: &str) -> String {
    sha256_hex(canonical_json(&json!([dispatch_id, brief_sha256, 1])).as_bytes())
}

/// The launch spec of a status, in its canonical form, or `None` when it names none.
fn launch_spec(status: &Map<String, Value>) -> Option<Value> {
    let launch = status.get("launch")?.as_object()?;
    let argv: Vec<String> = launch
        .get("argv")?
        .as_array()?
        .iter()
        .map(|item| item.as_str().map(str::to_owned))
        .collect::<Option<_>>()?;
    let cwd = launch.get("cwd")?.as_str()?;
    (!argv.is_empty() && Path::new(cwd).is_absolute()).then(|| json!({"argv": argv, "cwd": cwd}))
}

/// The limits the approved brief pins.
struct Limits {
    timeout_seconds: i64,
    stop_grace_seconds: i64,
    poll_interval_seconds: i64,
}

fn limits(brief: &Map<String, Value>) -> Option<Limits> {
    let execution = brief.get("execution")?.as_object()?;
    if execution.get("supervisor")?.as_str()? != "harness-runtime" {
        return None;
    }
    let number = |key: &str| execution.get(key).and_then(Value::as_i64);
    let limits = Limits {
        timeout_seconds: number("timeout_seconds")?,
        stop_grace_seconds: number("stop_grace_seconds")?,
        poll_interval_seconds: number("poll_interval_seconds")?,
    };
    (limits.timeout_seconds > 0
        && limits.stop_grace_seconds >= 0
        && limits.poll_interval_seconds > 0)
        .then_some(limits)
}

fn text<'a>(record: &'a Map<String, Value>, key: &str) -> Option<&'a str> {
    record.get(key).and_then(Value::as_str)
}

/// The refusals of step 1: nothing is written for any of them.
fn admit(
    brief: &Map<String, Value>,
    status: &Map<String, Value>,
    request: &Request,
    dispatch_id: &str,
) -> Result<Limits, ErrorBody> {
    if text(status, "state") != Some("dispatched") {
        return Err(denied(format!(
            "the dispatch {dispatch_id} is not dispatched"
        )));
    }
    if text(brief, "resolved_transport") != Some("external") {
        return Err(denied(
            "only an external dispatch is launched by the runtime",
        ));
    }
    if text(brief, "role") == Some("qa") || text(brief, "purpose") == Some("publish") {
        return Err(denied(
            "a QA or publish dispatch never runs through the supervisor",
        ));
    }
    let limits = limits(brief).ok_or_else(|| {
        denied("the approved brief pins no execution of the supervisor harness-runtime")
    })?;
    let expected = operation_id(dispatch_id, &object_sha256(brief));
    if request.operation_id != expected {
        return Err(denied(
            "the operation id is not sha256([dispatch_id, brief_sha256, 1]) of the stored brief",
        )
        .with_fact("expected_operation_id", expected));
    }
    Ok(limits)
}

/// The stored row of an operation: its digest, state and result.
fn stored_operation(
    connection: &Connection,
    id: &str,
) -> Result<Option<(String, String, Option<String>)>, ErrorBody> {
    sql(connection
        .query_row(
            "SELECT request_digest, state, result_json FROM operations WHERE operation_id = ?1",
            [id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .optional())
}

/// The answer a stored operation gives a repeat: its result, its failure, or `None` while it is
/// an intent.
fn replay(
    stored: (String, String, Option<String>),
    digest: &str,
    id: &str,
) -> Option<Result<Value, ErrorBody>> {
    let (stored_digest, state, result) = stored;
    if stored_digest != digest {
        return Some(Err(ErrorBody::new(
            ErrorCode::IdempotencyConflict,
            FailureDomain::Tooling,
            false,
            format!("the operation id {id} was used for another request or another launch spec"),
            "the launch spec of the dispatch changed under its intent; inspect the dispatch status",
        )));
    }
    let parsed = result.and_then(|text| serde_json::from_str::<Value>(&text).ok());
    match state.as_str() {
        "committed" => Some(Ok(parsed.unwrap_or(Value::Null))),
        "failed" => Some(Err(parsed
            .and_then(|value| serde_json::from_value::<ErrorBody>(value).ok())
            .unwrap_or_else(|| store_busy("the stored failure cannot be read")))),
        _ => None,
    }
}

impl Supervisor {
    fn state(&self) -> Result<&Path, ErrorBody> {
        self.state_dir.as_deref().ok_or_else(|| {
            ErrorBody::new(
                ErrorCode::NotFound,
                FailureDomain::Policy,
                false,
                "this runtime serves no state",
                "start the runtime with `harness-runtime serve --repo <repo>` of the repository",
            )
        })
    }

    fn lock_of(&self, id: &str) -> Arc<Mutex<()>> {
        Arc::clone(guard(&self.inner.locks).entry(id.to_owned()).or_default())
    }

    fn logs(&self, id: &str) -> Result<(PathBuf, PathBuf), ErrorBody> {
        let directory = self.state()?.join("executions").join(id);
        Ok((directory.join("stdout.log"), directory.join("stderr.log")))
    }

    fn is_watched(&self, id: &str) -> bool {
        guard(&self.inner.watched).contains(id)
    }

    /// `execution.launch`: launch the execution of the dispatch of the payload once.
    ///
    /// # Errors
    /// The typed error of the response; a launch the OS refused is the stored
    /// `engine_unavailable` of the operation.
    pub fn launch(self: &Arc<Self>, request: &Request) -> Result<Value, ErrorBody> {
        let state = self.state()?.to_path_buf();
        let dispatch_id = dispatch_id_of(request)?;
        self.reconcile_all();
        let lock = self.lock_of(&request.operation_id);
        let _held = acquire(&lock, request.request_deadline)?;
        hooks::reach_once("execution.launch:before-begin").map_err(|err| injected(&err))?;
        let mut connection = executions::connect(&state)?;
        let (brief, status) =
            executions::dispatch_records(&connection, &dispatch_id)?.ok_or_else(|| {
                ErrorBody::new(
                    ErrorCode::NotFound,
                    FailureDomain::Tooling,
                    false,
                    format!("the store has no dispatch {dispatch_id}"),
                    "name a dispatch the store holds",
                )
            })?;
        let spec = launch_spec(&status);
        let digest = sha256_hex(
            canonical_json(&json!([state::write::request_digest(request), spec])).as_bytes(),
        );
        if let Some(stored) = stored_operation(&connection, &request.operation_id)? {
            if let Some(answer) = replay(stored, &digest, &request.operation_id) {
                return answer;
            }
            // An intent that no launch of this runtime holds: a runtime died after it.
            if let Some(execution) = executions::load(&connection, &request.operation_id)? {
                self.settle(&mut connection, execution)?;
            }
            let stored = stored_operation(&connection, &request.operation_id)?;
            return stored
                .and_then(|stored| replay(stored, &digest, &request.operation_id))
                .unwrap_or_else(|| Err(store_busy("the launch is not settled yet")));
        }
        let limits = admit(&brief, &status, request, &dispatch_id)?;
        let spec = spec.ok_or_else(|| denied("the dispatch status names no launch spec"))?;
        let execution = self.intent(
            &mut connection,
            request,
            &dispatch_id,
            &digest,
            &spec,
            &limits,
        )?;
        hooks::reach_once("execution.launch:intent").map_err(|err| injected(&err))?;

        let (stdout, stderr) = self.logs(&execution.id)?;
        let argv: Vec<String> = spec["argv"]
            .as_array()
            .map(|items| {
                items
                    .iter()
                    .filter_map(|item| item.as_str().map(str::to_owned))
                    .collect()
            })
            .unwrap_or_default();
        let cwd = PathBuf::from(spec["cwd"].as_str().unwrap_or_default());
        let spawned = std::fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(stdout.parent().unwrap_or(&state))
            .and_then(|()| {
                process::spawn(&Spec {
                    argv: &argv,
                    cwd: &cwd,
                    operation_id: &execution.id,
                    dispatch_id: &dispatch_id,
                    stdout: &stdout,
                    stderr: &stderr,
                })
            });
        let child = match spawned {
            Ok(child) => child,
            Err(err) => {
                let error = ErrorBody::new(
                    ErrorCode::EngineUnavailable,
                    FailureDomain::Tooling,
                    false,
                    format!("the OS refused to start the execution: {err}"),
                    "repair the launch command of the dispatch; nothing started, and a new launch needs a new dispatch",
                )
                .with_fact("spawn_error", err.to_string())
                .with_fact("errno", err.raw_os_error())
                .with_fact("execution_state", ExecutionState::Lost.as_str());
                let facts = json!({"spawn_error": err.to_string(), "errno": err.raw_os_error()});
                self.fail(&mut connection, &execution, &error, facts)?;
                return Err(error);
            }
        };
        let pid = i64::from(child.id());
        guard(&self.inner.children).insert(execution.id.clone(), child);
        hooks::reach_once("execution.launch:launched").map_err(|err| injected(&err))?;
        self.receipt(&mut connection, &execution, pid, false)
    }

    /// Step 1: the operation intent, the execution in `launching` and its audit record, in one
    /// transaction.
    fn intent(
        &self,
        connection: &mut Connection,
        request: &Request,
        dispatch_id: &str,
        digest: &str,
        spec: &Value,
        limits: &Limits,
    ) -> Result<Execution, ErrorBody> {
        let (stdout, stderr) = self.logs(&request.operation_id)?;
        let launch = json!({
            "argv": spec["argv"],
            "cwd": spec["cwd"],
            "labels": {
                process::LABEL_OPERATION: request.operation_id,
                process::LABEL_DISPATCH: dispatch_id,
            },
            "logs": {"stdout": stdout, "stderr": stderr},
        });
        let at = iso_now();
        let deadline = iso_from_micros(now_micros() + limits.timeout_seconds * 1_000_000);
        let tx = executions::begin(connection)?;
        hooks::reach_once("execution.launch:in-transaction").map_err(|err| injected(&err))?;
        sql(tx.execute(
            "INSERT INTO operations (operation_id, operation, request_digest, state, created_at)
             VALUES (?1, ?2, ?3, 'intent', ?4)",
            rusqlite::params![request.operation_id, request.operation, digest, at],
        ))?;
        let execution = executions::insert(
            &tx,
            &NewExecution {
                id: &request.operation_id,
                dispatch_id,
                launch: &launch,
                execution_deadline: &deadline,
                stop_grace_seconds: limits.stop_grace_seconds,
                poll_interval_seconds: limits.poll_interval_seconds,
                lease_owner: &owner(),
            },
            &at,
        )?;
        sql(tx.commit())?;
        Ok(execution)
    }

    /// Step 3, or the adoption of a labelled process: the identity, `running`, the receipt and
    /// the committed operation, in one transaction. A child of this runtime gets its watcher.
    fn receipt(
        self: &Arc<Self>,
        connection: &mut Connection,
        execution: &Execution,
        pid: i64,
        adopted: bool,
    ) -> Result<Value, ErrorBody> {
        let observer = identity::observer();
        let (identity, pgid) = process::identity_of(pid, &observer)
            .ok_or_else(|| store_busy("the identity of the launched process cannot be read yet"))?;
        let at = iso_now();
        let tx = executions::begin(connection)?;
        hooks::reach_once("execution.launch:receipt")
            .map_err(|err| store_busy(&format!("the receipt was not written: {err}")))?;
        let current = executions::load(&tx, &execution.id)?
            .filter(|current| current.identity.is_none() && !is_terminal(current.state))
            .ok_or_else(|| store_busy("the execution changed before its receipt"))?;
        let receipt = json!({
            "execution_id": current.id,
            "dispatch_id": current.dispatch_id,
            "state": ExecutionState::Running.as_str(),
            "identity": identity.to_json(),
            "pgid": pgid,
            "accounting": "process-group",
            "launched_at": at,
            "execution_deadline": current.execution_deadline,
            "logs": current.launch.get("logs").cloned().unwrap_or(Value::Null),
            "adopted": adopted,
        });
        executions::transition(
            &tx,
            &current,
            ExecutionState::Running,
            Change {
                identity: Some(identity),
                pgid: Some(pgid),
                receipt: Some(receipt.clone()),
                lease_owner: adopted.then(owner),
                facts: json!({"adopted": adopted}),
                ..Change::default()
            },
            &at,
        )?;
        sql(tx.execute(
            "UPDATE operations SET state = 'committed', result_json = ?2, completed_at = ?3
             WHERE operation_id = ?1 AND state = 'intent'",
            rusqlite::params![current.id, receipt.to_string(), at],
        ))?;
        sql(tx.commit())?;
        let child = guard(&self.inner.children).remove(&current.id);
        if let Some(child) = child {
            self.watch(current.id.clone(), child);
        }
        Ok(receipt)
    }

    /// The execution becomes `lost` and its operation `failed` with `error`, in one transaction.
    fn fail(
        &self,
        connection: &mut Connection,
        execution: &Execution,
        error: &ErrorBody,
        facts: Value,
    ) -> Result<(), ErrorBody> {
        let at = iso_now();
        let tx = executions::begin(connection)?;
        let Some(current) = executions::load(&tx, &execution.id)? else {
            return Ok(());
        };
        if allowed(current.state, ExecutionState::Lost) {
            executions::transition(
                &tx,
                &current,
                ExecutionState::Lost,
                Change {
                    facts,
                    ..Change::default()
                },
                &at,
            )?;
        }
        let body = serde_json::to_string(error).unwrap_or_default();
        sql(tx.execute(
            "UPDATE operations SET state = 'failed', result_json = ?2, completed_at = ?3
             WHERE operation_id = ?1 AND state = 'intent'",
            rusqlite::params![current.id, body, at],
        ))?;
        sql(tx.commit())
    }

    /// One transition of a row this thread holds the mutex of, from the state it was read in.
    fn move_to(
        connection: &mut Connection,
        execution: &Execution,
        to: ExecutionState,
        change: Change,
    ) -> Result<(), ErrorBody> {
        let tx = executions::begin(connection)?;
        let current = executions::load(&tx, &execution.id)?;
        if let Some(current) = current.filter(|current| current.state == execution.state) {
            executions::transition(&tx, &current, to, change, &iso_now())?;
            sql(tx.commit())?;
        }
        Ok(())
    }

    /// Reconcile one execution whose mutex this thread holds, and return it as stored after.
    fn settle(
        self: &Arc<Self>,
        connection: &mut Connection,
        execution: Execution,
    ) -> Result<Execution, ErrorBody> {
        if is_terminal(execution.state) || self.is_watched(&execution.id) {
            return Ok(execution);
        }
        match &execution.identity {
            None => {
                let own = guard(&self.inner.children)
                    .get(&execution.id)
                    .map(|child| i64::from(child.id()));
                match own.map_or_else(|| process::scan(&execution.id), |pid| Ok(Some(pid))) {
                    Ok(Some(pid)) => {
                        self.receipt(connection, &execution, pid, true)?;
                    }
                    Ok(None) => {
                        let reason = "no labelled process; never relaunched";
                        let error = ErrorBody::new(
                            ErrorCode::EngineUnavailable,
                            FailureDomain::Infrastructure,
                            false,
                            format!("the launch of the execution {} left {reason}", execution.id),
                            "the execution is lost: a new launch needs a new dispatch (ADR 0021, section 4)",
                        )
                        .with_fact("execution_state", ExecutionState::Lost.as_str());
                        self.fail(connection, &execution, &error, json!({"reason": reason}))?;
                    }
                    Err(err) if execution.state != ExecutionState::Unknown => {
                        Self::move_to(
                            connection,
                            &execution,
                            ExecutionState::Unknown,
                            Change {
                                facts: json!({"scan_error": err.to_string()}),
                                ..Change::default()
                            },
                        )?;
                    }
                    Err(_) => {}
                }
            }
            Some(recorded) => self.observe_identity(connection, &execution, recorded)?,
        }
        Ok(executions::load(connection, &execution.id)?.unwrap_or(execution))
    }

    /// Map the verdict on the recorded identity to a state and record a change of state.
    fn observe_identity(
        &self,
        connection: &mut Connection,
        execution: &Execution,
        recorded: &Identity,
    ) -> Result<(), ErrorBody> {
        let observer = identity::observer();
        let verdict = identity::verdict(Some(recorded), &observer, &identity::probe(recorded.pid));
        let to = executions::observed(verdict, false);
        if to == execution.state || !allowed(execution.state, to) {
            return Ok(());
        }
        let observation = json!({"verdict": verdict.as_str(), "observed_at": iso_now()});
        Self::move_to(
            connection,
            execution,
            to,
            Change {
                observation: Some(observation),
                facts: json!({"verdict": verdict.as_str()}),
                ..Change::default()
            },
        )
    }

    /// `execution.observe`: reconcile the execution of the dispatch of the payload, then answer
    /// it, or `null` for a dispatch the supervisor never launched.
    ///
    /// # Errors
    /// The typed error of the response.
    pub fn observe(self: &Arc<Self>, request: &Request) -> Result<Value, ErrorBody> {
        let state = self.state()?.to_path_buf();
        let dispatch_id = dispatch_id_of(request)?;
        self.reconcile_all();
        let mut connection = executions::connect(&state)?;
        let Some(execution) = executions::of_dispatch(&connection, &dispatch_id)? else {
            return Ok(json!({"execution": null}));
        };
        let lock = self.lock_of(&execution.id);
        let _held = acquire(&lock, request.request_deadline)?;
        let execution = executions::load(&connection, &execution.id)?.unwrap_or(execution);
        let execution = self.settle(&mut connection, execution)?;
        Ok(json!({"execution": execution.to_json()}))
    }

    /// Reconcile every execution that is not terminal and that no other thread holds now.
    pub fn reconcile_all(self: &Arc<Self>) {
        self.reconcile();
    }

    /// Reconcile, and return the smallest poll interval of the executions still live.
    fn reconcile(self: &Arc<Self>) -> Option<i64> {
        let state = self.state_dir.as_deref()?;
        let mut connection = executions::connect(state).ok()?;
        let rows = executions::unfinished(&connection).ok()?;
        let mut interval = None;
        for row in rows {
            let lock = self.lock_of(&row.id);
            let _held = match lock.try_lock() {
                Ok(held) => held,
                Err(TryLockError::Poisoned(poisoned)) => poisoned.into_inner(),
                Err(TryLockError::WouldBlock) => continue,
            };
            let Ok(Some(current)) = executions::load(&connection, &row.id) else {
                continue;
            };
            if let Ok(settled) = self.settle(&mut connection, current)
                && !is_terminal(settled.state)
            {
                let poll = settled.poll_interval_seconds;
                interval = Some(interval.map_or(poll, |found: i64| found.min(poll)));
            }
        }
        interval
    }

    /// Start the periodic reconcile: at the smallest pinned poll interval of the live executions,
    /// until `stop` is set.
    pub fn start(self: &Arc<Self>, stop: Arc<AtomicBool>) {
        let supervisor = Arc::clone(self);
        thread::spawn(move || {
            let mut interval = DEFAULT_POLL_SECONDS;
            while !stop.load(Ordering::Relaxed) {
                let until = now_ms() + u64::try_from(interval.max(1)).unwrap_or(1) * 1_000;
                while now_ms() < until && !stop.load(Ordering::Relaxed) {
                    thread::sleep(Duration::from_millis(50));
                }
                interval = supervisor.reconcile().unwrap_or(DEFAULT_POLL_SECONDS);
            }
        });
    }

    /// Wait for a child of this runtime on its own thread.
    fn watch(self: &Arc<Self>, id: String, child: Child) {
        guard(&self.inner.watched).insert(id.clone());
        let supervisor = Arc::clone(self);
        thread::spawn(move || supervisor.wait_for(&id, child));
    }

    fn wait_for(&self, id: &str, mut child: Child) {
        let status = loop {
            match child.try_wait() {
                Ok(Some(status)) => break Some(status),
                Ok(None) => thread::sleep(WAIT_POLL),
                Err(_) => break None,
            }
        };
        if let Some(status) = status {
            let exit = json!({
                "exit_code": status.code(),
                "signal": status.signal(),
                "source": "wait-status",
                "ended_at": iso_now(),
            });
            // A busy store delays the evidence; it is never dropped while the runtime lives.
            while self.record_exit(id, &exit).is_err() {
                thread::sleep(Duration::from_millis(500));
            }
        }
        guard(&self.inner.watched).remove(id);
    }

    /// Record the exit evidence of a child: `exited`, and the lease released with it.
    fn record_exit(&self, id: &str, exit: &Value) -> Result<(), ErrorBody> {
        let lock = self.lock_of(id);
        let _held = guard(&lock);
        let mut connection = executions::connect(self.state()?)?;
        let tx = executions::begin(&mut connection)?;
        let Some(current) = executions::load(&tx, id)? else {
            return Ok(());
        };
        if !allowed(current.state, ExecutionState::Exited) || current.identity.is_none() {
            return Ok(());
        }
        executions::transition(
            &tx,
            &current,
            ExecutionState::Exited,
            Change {
                exit: Some(exit.clone()),
                facts: json!({"source": "wait-status"}),
                ..Change::default()
            },
            &iso_now(),
        )?;
        sql(tx.commit())
    }
}

/// The owner of the lease of an execution: this runtime process, by its identity.
fn owner() -> Value {
    let pid = i64::from(std::process::id());
    let identity = process::identity_of(pid, &identity::observer())
        .map(|(identity, _)| identity.to_json())
        .unwrap_or(Value::Null);
    json!({"kind": "harness-runtime", "identity": identity})
}

/// Take the mutex of an operation, waiting until the deadline of the request.
fn acquire(lock: &Mutex<()>, deadline_ms: u64) -> Result<MutexGuard<'_, ()>, ErrorBody> {
    loop {
        match lock.try_lock() {
            Ok(held) => return Ok(held),
            Err(TryLockError::Poisoned(poisoned)) => return Ok(poisoned.into_inner()),
            Err(TryLockError::WouldBlock) if now_ms() >= deadline_ms => {
                return Err(store_busy("another request of the same operation runs"));
            }
            Err(TryLockError::WouldBlock) => thread::sleep(Duration::from_millis(20)),
        }
    }
}
