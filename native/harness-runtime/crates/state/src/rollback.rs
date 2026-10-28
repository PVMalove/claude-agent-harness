//! `harness-runtime rollback --to-legacy --repo <repo>` (ADR 0020, section 5): the way back from
//! the runtime store to the file ledger of installation 1.x, in one operation with a human
//! approval.
//!
//! 1. The approval: `approved_by`, `approved_at` and `reason` are not blank, the approver is no
//!    `policy:` actor, and `approved_at` is an RFC 3339 time that is not in the future.
//! 2. The serve instance lock, taken by the binary (`ipc::hold_instance_lock`): no runtime serves
//!    the repository while the rollback runs.
//! 3. The legacy lock, with `owner.json` naming `operation: runtime-rollback`.
//! 4. The store is opened and brought to the schema of this build; a newer schema refuses with
//!    no write, and a schema version is never lowered.
//! 5. In one read snapshot: no QA lease, no queued QA run and no execution that may still run
//!    (`launching`, `running`, `stopping`, `unknown`). A dispatch in `dispatched` or `working`
//!    with a stopped execution, or with none, is exported as it is.
//! 6. The archive `state/runtime-export/<store_id>.tar`, then a new generation of format v3 and
//!    its parity check against the store.
//! 7. The audit record `runtime-rollback` of the store, with the SHA-256 of the archive; the store
//!    leaves WAL mode and becomes read-only. It is never deleted.
//! 8. `AUTHORITY.json` names `file-ledger` and the store it was rolled back from.
//! 9. `ledger.json` names version 3 and the new generation: the commit point.
//!
//! A repeat after the commit point answers `already_rolled_back`. A repeat that finds step 8 done
//! and the pointer still at version 4 does step 9 only; a repeat that finds step 7 done continues
//! from step 8 with the generation and the archive the audit record names. A failure before
//! step 7 removes the generation it wrote, and the store stays the authority; the archive it
//! installed is named by no record, so a repeat replaces it. The archive the record names is
//! never replaced.

use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;

use contracts::ErrorCode;
use rusqlite::{Connection, OpenFlags, OptionalExtension, TransactionBehavior};
use serde_json::{Map, Value, json};

use crate::audit;
use crate::authority::{AUTHORITY_NAME, Authority, Epoch, FILE_LEDGER, STORE_NAME, epoch};
use crate::canonical::{canonical_json, sha256_hex};
use crate::error::{Error, Result};
use crate::export::{self, Generation, ROLLBACK_ACTION};
use crate::hooks;
use crate::legacy::{GENERATIONS, LEGACY_VERSION, POINTER_NAME, read_pointer};
use crate::lock;
use crate::migrate::STATE_REL;
use crate::parity;
use crate::platform;
use crate::schema;
use crate::time::{iso_now, now_micros, parse_rfc3339};

/// An execution in one of these states may still change the repository or the store.
const LIVE_EXECUTION_STATES: [&str; 4] = ["launching", "running", "stopping", "unknown"];

/// The human approval of a rollback (as for `release-lock --force`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Approval {
    pub approved_by: String,
    pub approved_at: String,
    pub reason: String,
}

/// What a successful rollback reports.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Outcome {
    /// The state was rolled back already: nothing was changed.
    pub already_rolled_back: bool,
    /// The store the state was rolled back from.
    pub store_id: String,
    pub generation: String,
    /// The archive, relative to the state root.
    pub export: String,
    pub export_sha256: String,
    pub rolled_back_at: String,
}

impl Outcome {
    /// The single JSON line `harness-runtime rollback` prints.
    pub fn to_json(&self) -> Value {
        json!({
            "rolled_back": !self.already_rolled_back,
            "already_rolled_back": self.already_rolled_back,
            "store_id": self.store_id,
            "generation": self.generation,
            "export": self.export,
            "export_sha256": self.export_sha256,
            "rolled_back_at": self.rolled_back_at,
        })
    }
}

/// What the operator does after a failure of the rollback.
pub fn remedy(error: &Error) -> &str {
    match error {
        Error::Refused { remedy, .. } => remedy,
        Error::Interrupted(_) => {
            "repeat `harness-runtime rollback --to-legacy` with the same approval: the store is the \
             authority until the pointer names version 3, and a repeat continues from the last \
             step that finished"
        }
        Error::Invariant(_) => {
            "stop every writer of the state directory and read the difference above; keep \
             runtime.db and state/runtime-export for analysis, and restore the snapshot named in \
             AUTHORITY.json only with a separate approval of the owner"
        }
    }
}

fn refused(code: ErrorCode, message: impl Into<String>, remedy: impl Into<String>) -> Error {
    Error::refused(code, message, remedy)
}

/// Step 1: the approval of a human.
///
/// # Errors
/// `malformed_request` for a blank field, or an `approved_at` that is no RFC 3339 time or lies in
/// the future; `policy_denied` for a `policy:` approver.
pub fn check_approval(approval: &Approval) -> Result<()> {
    const REMEDY: &str = "pass --approved-by <human>, --approved-at <RFC 3339 time> and --reason \
        <text> of the human who approved the rollback";
    for (flag, value) in [
        ("--approved-by", &approval.approved_by),
        ("--approved-at", &approval.approved_at),
        ("--reason", &approval.reason),
    ] {
        if value.trim().is_empty() {
            return Err(refused(
                ErrorCode::MalformedRequest,
                format!("{flag} is blank"),
                REMEDY,
            ));
        }
    }
    if approval.approved_by.trim_start().starts_with("policy:") {
        return Err(refused(
            ErrorCode::PolicyDenied,
            format!(
                "{:?} is a policy approver; a rollback needs a human approval",
                approval.approved_by
            ),
            "ask a human to approve the rollback and pass their name as --approved-by",
        ));
    }
    let Some(at) = parse_rfc3339(approval.approved_at.trim()) else {
        return Err(refused(
            ErrorCode::MalformedRequest,
            format!(
                "--approved-at {:?} is no RFC 3339 time",
                approval.approved_at
            ),
            REMEDY,
        ));
    };
    if at > now_micros() {
        return Err(refused(
            ErrorCode::MalformedRequest,
            format!("--approved-at {} lies in the future", approval.approved_at),
            "pass the time at which the human approved the rollback",
        ));
    }
    Ok(())
}

/// The rollback mark of `AUTHORITY.json` (step 8).
#[derive(Clone, Debug, PartialEq, Eq)]
struct Mark {
    store_id: String,
    generation: String,
    export: String,
    export_sha256: String,
    rolled_back_at: String,
}

fn text(record: &Map<String, Value>, key: &str) -> Option<String> {
    record
        .get(key)
        .and_then(Value::as_str)
        .filter(|text| !text.is_empty())
        .map(str::to_owned)
}

/// The rollback mark, when `AUTHORITY.json` names the file ledger.
fn read_mark(state: &Path) -> Result<Option<Mark>> {
    let bytes = match fs::read(state.join(AUTHORITY_NAME)) {
        Ok(bytes) => bytes,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(err) => return Err(err.into()),
    };
    let Ok(Value::Object(record)) = serde_json::from_slice::<Value>(&bytes) else {
        return Ok(None);
    };
    if text(&record, "authority").as_deref() != Some(FILE_LEDGER) {
        return Ok(None);
    }
    let field = |key: &str| {
        text(&record, key).ok_or_else(|| {
            Error::invariant(format!(
                "AUTHORITY.json names the file ledger but has no {key}"
            ))
        })
    };
    Ok(Some(Mark {
        store_id: field("rolled_back_from")?,
        generation: field("generation")?,
        export: field("export")?,
        export_sha256: field("export_sha256")?,
        rolled_back_at: field("rolled_back_at")?,
    }))
}

fn outcome(mark: Mark, already_rolled_back: bool) -> Outcome {
    Outcome {
        already_rolled_back,
        store_id: mark.store_id,
        generation: mark.generation,
        export: mark.export,
        export_sha256: mark.export_sha256,
        rolled_back_at: mark.rolled_back_at,
    }
}

/// The repeat after the commit point: the pointer names version 3 and `AUTHORITY.json` the file
/// ledger.
fn already_rolled_back(state: &Path) -> Result<Option<Outcome>> {
    let rolled_back = read_pointer(state)?.is_some_and(|pointer| pointer.version == LEGACY_VERSION);
    if !rolled_back {
        return Ok(None);
    }
    Ok(read_mark(state)?.map(|mark| outcome(mark, true)))
}

fn nothing_to_roll_back() -> Error {
    refused(
        ErrorCode::NotFound,
        "nothing_to_roll_back: the file ledger is the authority of this repository",
        "run the rollback only in a repository that `harness-runtime migrate` cut over",
    )
}

/// Run the rollback of the repository at `repo` with the human `approval`.
///
/// # Errors
/// `Error::Refused` (exit code 2): the approval or a precondition refused it, nothing was changed.
/// `Error::Interrupted` (3): it failed after it began; the store is the authority until step 9,
/// and a repeat continues. `Error::Invariant` (4): the export differs from the store, the archive
/// the `runtime-rollback` record names has other bytes, or the state directory is inconsistent.
pub fn run(repo: &Path, approval: &Approval) -> Result<Outcome> {
    check_approval(approval)?;
    let state: PathBuf = repo.join(STATE_REL);
    if let Some(done) = already_rolled_back(&state)? {
        let _ = lock::release_if_dead(&state);
        return Ok(done);
    }
    match epoch(&state) {
        Epoch::FileLedger => return Err(nothing_to_roll_back()),
        Epoch::Inconsistent(reason) if read_mark(&state)?.is_none() => {
            return Err(Error::invariant(format!(
                "the state directory is cut over but inconsistent: {reason}"
            )));
        }
        Epoch::Inconsistent(_) | Epoch::Store(_) => {}
    }
    let held = lock::acquire(&state, lock::ROLLBACK_OPERATION)?;
    let result = hooks::reach("rollback-locked").and_then(|()| locked(&state, approval));
    let released = held.release();
    let done = result?;
    // A lock that cannot be removed after the commit point is no failure: the next operation
    // removes it when its owner is dead.
    let _ = released;
    Ok(done)
}

/// Steps 4 to 9, under the legacy lock; the epoch is read again, because another rollback may
/// have finished while this one waited.
fn locked(state: &Path, approval: &Approval) -> Result<Outcome> {
    if let Some(done) = already_rolled_back(state)? {
        return Ok(done);
    }
    match epoch(state) {
        Epoch::FileLedger => Err(nothing_to_roll_back()),
        Epoch::Inconsistent(reason) => match read_mark(state)? {
            Some(mark) => {
                check_generation(state, &mark.generation)?;
                replace_pointer(state, &mark.generation, &mark.rolled_back_at)?;
                Ok(outcome(mark, false))
            }
            None => Err(Error::invariant(format!(
                "the state directory is cut over but inconsistent: {reason}"
            ))),
        },
        Epoch::Store(authority) => from_store(state, &authority, approval),
    }
}

/// The generation and the archive the store's `runtime-rollback` record names.
#[derive(Clone, Debug)]
struct Recorded {
    generation: String,
    export: String,
    export_sha256: String,
}

fn recorded_rollback(connection: &Connection) -> Result<Option<Recorded>> {
    let details: Option<String> = connection
        .query_row(
            "SELECT details_json FROM audit WHERE action = ?1 ORDER BY seq DESC LIMIT 1",
            [ROLLBACK_ACTION],
            |row| row.get(0),
        )
        .optional()?;
    let Some(details) = details else {
        return Ok(None);
    };
    let Ok(Value::Object(details)) = serde_json::from_str::<Value>(&details) else {
        return Err(Error::invariant(
            "the runtime-rollback audit record of the store has no details",
        ));
    };
    let field = |key: &str| {
        text(&details, key).ok_or_else(|| {
            Error::invariant(format!(
                "the runtime-rollback audit record of the store has no {key}"
            ))
        })
    };
    Ok(Some(Recorded {
        generation: field("generation")?,
        export: field("export")?,
        export_sha256: field("export_sha256")?,
    }))
}

fn open_store(state: &Path) -> Result<Connection> {
    let connection = Connection::open_with_flags(
        state.join(STORE_NAME),
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    connection.busy_timeout(Duration::from_secs(5))?;
    connection.execute_batch("PRAGMA synchronous = FULL; PRAGMA foreign_keys = ON;")?;
    Ok(connection)
}

/// Step 5: nothing runs and the QA lane is free: its row may stay, without an owner.
fn preconditions(connection: &Connection) -> Result<()> {
    let count = |sql: &str| -> Result<i64> { Ok(connection.query_row(sql, [], |row| row.get(0))?) };
    if count("SELECT COUNT(*) FROM qa_lane WHERE owner_id IS NOT NULL")? > 0 {
        return Err(refused(
            ErrorCode::LeaseHeld,
            "the QA lane of the store holds a lease",
            "finish the QA run or release its lease, then repeat the rollback",
        ));
    }
    if count("SELECT COUNT(*) FROM qa_queue")? > 0 {
        return Err(refused(
            ErrorCode::LeaseHeld,
            "the QA lane of the store has queued runs",
            "let the queued QA runs finish, then repeat the rollback",
        ));
    }
    let marks = vec!["?"; LIVE_EXECUTION_STATES.len()].join(", ");
    let mut statement = connection.prepare(&format!(
        "SELECT id FROM dispatch_status WHERE execution_state IN ({marks}) ORDER BY id"
    ))?;
    let live: Vec<String> = statement
        .query_map(rusqlite::params_from_iter(LIVE_EXECUTION_STATES), |row| {
            row.get(0)
        })?
        .collect::<rusqlite::Result<_>>()?;
    if !live.is_empty() {
        return Err(refused(
            ErrorCode::LeaseHeld,
            format!("executions may still run: {}", live.join(", ")),
            "stop the executions of these dispatches and wait until they have ended, then \
             repeat the rollback",
        ));
    }
    Ok(())
}

/// What step 6 wrote.
struct Written {
    export: export::Export,
    generation: Generation,
    tail: Option<(i64, String)>,
    at: String,
}

/// Step 6, in one read snapshot with the checks of step 5.
fn write_export(
    connection: &Connection,
    state: &Path,
    authority: &Authority,
    approval: &Approval,
) -> Result<Written> {
    preconditions(connection)?;
    hooks::reach("rollback-checked")?;
    let tail = audit::tail(connection)?;
    let bytes = export::archive(connection)?;
    discard_unfinished_archive(state, &authority.store_id, &bytes)?;
    let export = export::write_archive(state, &authority.store_id, &bytes)?;
    hooks::reach("rollback-archive")?;
    let at = iso_now();
    let details = json!({
        "store_id": authority.store_id,
        "export": export.path,
        "export_sha256": export.sha256,
        "approved_by": approval.approved_by,
        "approved_at": approval.approved_at,
        "reason": approval.reason,
    });
    let generation = export::write_generation(connection, state, &at, &details)?;
    let checked = parity::check_export(connection, state, &generation)
        .and_then(|()| hooks::reach("rollback-generation"));
    if let Err(err) = checked {
        remove_generation(state, &generation.name);
        return Err(err);
    }
    Ok(Written {
        export,
        generation,
        tail,
        at,
    })
}

/// The archive an unfinished attempt installed before the commit point. Step 6 runs only while
/// the store has no `runtime-rollback` record, so no record names that archive: it is discarded
/// state, and the store may have changed since. It is removed when its bytes differ, and this
/// attempt installs its own. The archive of a committed rollback is never reached here: a repeat
/// then continues from the record, and `finish` only compares the archive with it.
fn discard_unfinished_archive(state: &Path, store_id: &str, bytes: &[u8]) -> Result<()> {
    // `write_archive` refuses a store_id that names no plain file name.
    if store_id.is_empty() || store_id.contains(['/', '\\']) || store_id.starts_with('.') {
        return Ok(());
    }
    let path = state
        .join(export::EXPORT_DIRECTORY)
        .join(format!("{store_id}.tar"));
    match fs::read(&path) {
        Ok(existing) if existing == bytes => Ok(()),
        Ok(_) => {
            fs::remove_file(&path)?;
            Ok(())
        }
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(err) => Err(err.into()),
    }
}

fn remove_generation(state: &Path, name: &str) {
    let _ = fs::remove_dir_all(state.join(GENERATIONS).join(name));
}

/// Steps 4 to 9 from a store that is the authority.
fn from_store(state: &Path, authority: &Authority, approval: &Approval) -> Result<Outcome> {
    let mut connection = open_store(state)?;
    schema::upgrade(&mut connection)?;
    if let Some(recorded) = recorded_rollback(&connection)? {
        seal_read_only(connection, state)?;
        return finish(state, authority, &recorded);
    }
    connection.execute_batch("BEGIN")?;
    let written = write_export(&connection, state, authority, approval);
    let _ = connection.execute_batch("COMMIT");
    let written = written?;
    let recorded = Recorded {
        generation: written.generation.name.clone(),
        export: written.export.path.clone(),
        export_sha256: written.export.sha256.clone(),
    };
    let appended = (|| -> Result<()> {
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        if audit::tail(&transaction)? != written.tail {
            return Err(Error::invariant(
                "the audit chain of the store moved during the rollback",
            ));
        }
        audit::append(
            &transaction,
            &written.at,
            ROLLBACK_ACTION,
            &json!({
                "generation": recorded.generation,
                "export": recorded.export,
                "export_sha256": recorded.export_sha256,
                "approved_by": approval.approved_by,
                "approved_at": approval.approved_at,
                "reason": approval.reason,
            }),
        )?;
        transaction.commit()?;
        Ok(())
    })();
    if let Err(err) = appended {
        remove_generation(state, &recorded.generation);
        return Err(err);
    }
    seal_read_only(connection, state)?;
    hooks::reach("rollback-audit")?;
    finish(state, authority, &recorded)
}

/// The rest of step 7: the store leaves WAL mode, so its one file holds every row, and becomes
/// read-only. It is kept for analysis and never deleted.
fn seal_read_only(connection: Connection, state: &Path) -> Result<()> {
    let path = state.join(STORE_NAME);
    let mut permissions = fs::metadata(&path)?.permissions();
    if !permissions.readonly() {
        let _: String =
            connection.pragma_update_and_check(None, "journal_mode", "DELETE", |row| row.get(0))?;
    }
    connection.close().map_err(|(_, err)| Error::from(err))?;
    if !permissions.readonly() {
        permissions.set_readonly(true);
        fs::set_permissions(&path, permissions)?;
    }
    Ok(())
}

fn check_generation(state: &Path, generation: &str) -> Result<()> {
    if state.join(GENERATIONS).join(generation).is_dir() {
        Ok(())
    } else {
        Err(Error::invariant(format!(
            "the generation {generation} that the rollback wrote is missing"
        )))
    }
}

/// Steps 8 and 9.
fn finish(state: &Path, authority: &Authority, recorded: &Recorded) -> Result<Outcome> {
    check_generation(state, &recorded.generation)?;
    let bytes = fs::read(state.join(&recorded.export))?;
    if sha256_hex(&bytes) != recorded.export_sha256 {
        return Err(Error::invariant(format!(
            "{} is not the archive the runtime-rollback audit record names",
            recorded.export
        )));
    }
    let rolled_back_at = iso_now();
    let record = canonical_json(&json!({
        "authority": FILE_LEDGER,
        "rolled_back_from": authority.store_id,
        "generation": recorded.generation,
        "export": recorded.export,
        "export_sha256": recorded.export_sha256,
        "rolled_back_at": rolled_back_at,
        "store_id": authority.store_id,
        "manifest_sha256": authority.manifest_sha256,
        "snapshot": authority.snapshot,
        "cutover_at": authority.cutover_at,
    }));
    platform::replace_durable(&state.join(AUTHORITY_NAME), record.as_bytes())?;
    hooks::reach("rollback-authority")?;
    replace_pointer(state, &recorded.generation, &rolled_back_at)?;
    Ok(Outcome {
        already_rolled_back: false,
        store_id: authority.store_id.clone(),
        generation: recorded.generation.clone(),
        export: recorded.export.clone(),
        export_sha256: recorded.export_sha256.clone(),
        rolled_back_at,
    })
}

/// Step 9: the pointer names version 3 and the new generation. The rename is the commit point.
fn replace_pointer(state: &Path, generation: &str, selected_at: &str) -> Result<()> {
    let pointer = canonical_json(&json!({
        "version": LEGACY_VERSION,
        "generation": generation,
        "selected_at": selected_at,
    }));
    let temporary = state.join(format!(".{POINTER_NAME}.{}.tmp", platform::random_hex()?));
    platform::write_durable(&temporary, pointer.as_bytes())?;
    let replaced = hooks::reach("rollback-pointer-replace")
        .and_then(|()| fs::rename(&temporary, state.join(POINTER_NAME)).map_err(Error::from));
    if let Err(err) = replaced {
        let _ = fs::remove_file(&temporary);
        return Err(err);
    }
    let _ = platform::sync_dir(state);
    hooks::pause("rollback-pointer");
    Ok(())
}
