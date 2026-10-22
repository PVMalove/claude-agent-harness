//! `harness-runtime migrate --repo <repo>` (ADR 0020, section 5): the cutover from the file ledger
//! of installation 1.x to the runtime store, in one operation.
//!
//! 1. Preconditions: nothing that the import would have to guess, and nothing running.
//! 2. The legacy lock, with `owner.json` and a held `flock`.
//! 3. A snapshot of the state directory, flushed to disk.
//! 4. The import into `state/runtime.db.importing`.
//! 5. The parity check, in Rust, and a check that the state did not change since the snapshot.
//! 6. The commit point: `runtime.db`, `AUTHORITY.json`, then `ledger.json` with `version: 4`.
//! 7. The release of the legacy lock.
//!
//! Until step 6 writes the pointer, the file ledger is the authority and every failure leaves it so.
//! The pointer is the commit point, so a repeated migrate after a crash at any step either starts
//! again or, after the pointer, answers `already_cut_over`.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use serde_json::json;

use crate::authority::{
    AUTHORITY_NAME, Authority, Epoch, IMPORTING_NAME, RUNTIME_STORE, STORE_NAME, STORE_VERSION,
    epoch,
};
use crate::canonical::{canonical_json, sha256_hex};
use crate::error::{Error, Result};
use crate::import::{Imported, import};
use crate::legacy::{
    LEGACY_VERSION, LegacyState, POINTER_NAME, RECORD_DIRECTORIES, parse_object, read_pointer,
};
use crate::lock::{self, LegacyLock};
use crate::parity;
use crate::platform;
use crate::snapshot::{self, Snapshot};
use crate::time::iso_now;

/// The state directory of a repository, relative to its root.
pub const STATE_REL: &str = ".harness/orchestration/state";
/// The snapshots directory of a repository, relative to its root.
pub const SNAPSHOTS_REL: &str = ".harness/orchestration/state-snapshots";

/// A dispatch in one of these states has a worker that may still write the ledger.
const LIVE_DISPATCH_STATES: [&str; 2] = ["dispatched", "working"];

const LEDGER_MIGRATE_REMEDY: &str = "run 'coordinator.py ledger migrate' of installation 1.x \
    first, so the state directory holds ledger version 3, then repeat the command";

/// What a successful `migrate` reports.
#[derive(Debug, PartialEq, Eq)]
pub struct Outcome {
    /// The state was already cut over: nothing was changed.
    pub already_cut_over: bool,
    pub authority: Authority,
}

impl Outcome {
    /// The single JSON line `harness-runtime migrate` prints.
    pub fn to_json(&self) -> serde_json::Value {
        json!({
            "migrated": !self.already_cut_over,
            "already_cut_over": self.already_cut_over,
            "store_id": self.authority.store_id,
            "manifest_sha256": self.authority.manifest_sha256,
            "snapshot": self.authority.snapshot,
            "cutover_at": self.authority.cutover_at,
        })
    }
}

fn refused(message: impl Into<String>, remedy: impl Into<String>) -> Error {
    Error::refused(message, remedy)
}

/// Step 1: whether the state directory can be migrated, and the state it holds.
fn preconditions(state: &Path) -> Result<LegacyState> {
    let Some(pointer) = read_pointer(state)? else {
        let records = RECORD_DIRECTORIES
            .iter()
            .any(|directory| state.join(directory).exists());
        return Err(if records {
            refused(
                "the state directory holds records of a layout older than the ledger",
                LEDGER_MIGRATE_REMEDY,
            )
        } else {
            refused(
                "nothing_to_migrate: the state directory holds no ledger",
                "run the migration on a repository that has orchestration state",
            )
        });
    };
    if pointer.version < LEGACY_VERSION {
        return Err(refused(
            format!(
                "the ledger is version {}, and migrate imports version {LEGACY_VERSION}",
                pointer.version
            ),
            LEDGER_MIGRATE_REMEDY,
        ));
    }
    if pointer.version != LEGACY_VERSION {
        return Err(refused(
            format!(
                "the ledger version {} is newer than this runtime knows",
                pointer.version
            ),
            "update harness-runtime, then repeat the command",
        ));
    }
    let legacy = LegacyState::read(state, pointer)?;
    legacy.validate()?;
    if legacy.file("qa-lane/lease.json").is_some() {
        return Err(refused(
            "the QA lane holds a lease",
            "finish the QA run, or clear the lease with 'coordinator.py qa clear-stale-lease' of installation 1.x, then repeat the command",
        ));
    }
    if legacy
        .files
        .iter()
        .any(|file| file.path.starts_with("qa-lane/queue/") && file.path.ends_with(".json"))
    {
        return Err(refused(
            "the QA lane has queued runs",
            "let the queued QA runs finish, then repeat the command",
        ));
    }
    let live: Vec<String> = legacy
        .files
        .iter()
        .filter_map(|file| {
            let name = file.path.strip_prefix("dispatch-status/")?;
            let record = parse_object(&file.bytes)?;
            let state = record.get("state")?.as_str()?;
            LIVE_DISPATCH_STATES
                .contains(&state)
                .then(|| name.trim_end_matches(".json").to_owned())
        })
        .collect();
    if !live.is_empty() {
        return Err(refused(
            format!("dispatches are sent or working: {}", live.join(", ")),
            "wait until their workers report, or abandon them, then repeat the command",
        ));
    }
    Ok(legacy)
}

fn remove_if_exists(path: &Path) -> io::Result<()> {
    match fs::remove_file(path) {
        Err(err) if err.kind() != io::ErrorKind::NotFound => Err(err),
        _ => Ok(()),
    }
}

/// Remove what an interrupted attempt left of the store. Only called while `ledger.json` is the
/// authority, so none of it is trusted by anyone.
fn remove_store_leftovers(state: &Path) -> Result<()> {
    for name in [
        STORE_NAME,
        "runtime.db-wal",
        "runtime.db-shm",
        "runtime.db-journal",
        IMPORTING_NAME,
        "runtime.db.importing-wal",
        "runtime.db.importing-shm",
        "runtime.db.importing-journal",
        AUTHORITY_NAME,
    ] {
        remove_if_exists(&state.join(name))?;
    }
    platform::sync_dir(state)?;
    Ok(())
}

/// A store a rollback kept for analysis is evidence: migrate does not delete it.
fn refuse_a_rolled_back_store(state: &Path) -> Result<()> {
    if let Some((authority, _)) = crate::authority::read(state)?
        && authority != RUNTIME_STORE
        && authority != String::new()
    {
        return Err(refused(
            format!(
                "AUTHORITY.json names {authority:?}, the mark of a store kept after a rollback"
            ),
            "archive or remove runtime.db and AUTHORITY.json yourself, then repeat the command",
        ));
    }
    Ok(())
}

/// The snapshot holds exactly the files the import read.
fn legacy_matches_snapshot(legacy: &LegacyState, snapshot: &Snapshot) -> Result<()> {
    let selected = format!("generations/{}", legacy.pointer.generation);
    for file in &legacy.files {
        let path = format!("{selected}/{}", file.path);
        if snapshot.files.get(&path) != Some(&sha256_hex(&file.bytes)) {
            return Err(Error::invariant(format!(
                "the legacy state changed between the preconditions and the snapshot: {path}"
            )));
        }
    }
    Ok(())
}

/// Step 6: rename the store into place, write `AUTHORITY.json`, replace the pointer.
fn commit(state: &Path, legacy: &LegacyState, authority: &Authority) -> Result<()> {
    let importing = state.join(IMPORTING_NAME);
    for sidecar in ["-wal", "-shm", "-journal"] {
        if state.join(format!("{IMPORTING_NAME}{sidecar}")).exists() {
            return Err(Error::Interrupted(format!(
                "the store has a {sidecar} file next to it; renaming it would lose rows"
            )));
        }
    }
    fs::File::open(&importing)?.sync_all()?;
    fs::rename(&importing, state.join(STORE_NAME))?;
    platform::sync_dir(state)?;
    platform::replace_durable(&state.join(AUTHORITY_NAME), authority.to_json().as_bytes())?;
    let pointer = canonical_json(&json!({
        "version": STORE_VERSION,
        "generation": legacy.pointer.generation,
        "selected_at": authority.cutover_at,
    }));
    // The rename of the pointer is the commit point. Whatever fails after it cannot be undone.
    let temporary = state.join(format!(".{POINTER_NAME}.{}.tmp", platform::random_hex()?));
    platform::write_durable(&temporary, pointer.as_bytes())?;
    if let Err(err) = fs::rename(&temporary, state.join(POINTER_NAME)) {
        let _ = fs::remove_file(&temporary);
        return Err(err.into());
    }
    let _ = platform::sync_dir(state);
    Ok(())
}

/// Steps 1 to 6, under the lock. What an attempt leaves of the store goes with it, unless the
/// pointer was replaced: then the cutover is done.
fn locked(repo: &Path, state: &Path) -> Result<Authority> {
    refuse_a_rolled_back_store(state)?;
    remove_store_leftovers(state)?;
    let outcome = import_and_commit(repo, state);
    if outcome.is_err() && matches!(epoch(state), Epoch::FileLedger) {
        let _ = remove_store_leftovers(state);
    }
    outcome
}

/// Steps 3 to 6.
fn import_and_commit(repo: &Path, state: &Path) -> Result<Authority> {
    let legacy = preconditions(state)?;
    let snapshot = snapshot::create(&repo.join(SNAPSHOTS_REL), state)?;
    legacy_matches_snapshot(&legacy, &snapshot)?;
    let store_id = platform::random_hex()?;
    let at = iso_now();
    let importing = state.join(IMPORTING_NAME);
    let Imported {
        manifest_json,
        manifest_sha256,
        ..
    } = import(&legacy, &importing, &store_id, &at)?;
    snapshot::write_import_manifest(&snapshot, &manifest_json)?;
    parity::check(&legacy, &importing)?;
    snapshot::verify_unchanged(state, &snapshot)?;
    let authority = Authority {
        store_id,
        manifest_sha256,
        snapshot: snapshot_relative(repo, &snapshot.dir),
        cutover_at: iso_now(),
    };
    commit(state, &legacy, &authority)?;
    Ok(authority)
}

fn snapshot_relative(repo: &Path, snapshot: &Path) -> String {
    snapshot.strip_prefix(repo).map_or_else(
        |_| snapshot.to_string_lossy().into_owned(),
        |relative| relative.to_string_lossy().replace('\\', "/"),
    )
}

/// The state is cut over already: the only change allowed is the removal of the lock a dead
/// migrate left behind.
fn already_cut_over(state: &Path, authority: Authority) -> Outcome {
    let _ = lock::release_if_dead(state);
    Outcome {
        already_cut_over: true,
        authority,
    }
}

/// Run the migration of the repository at `repo`.
///
/// # Errors
/// `Error::Refused` (exit code 2): a precondition failed, nothing was changed. `Error::Interrupted`
/// (3): the operation failed after it began; the partial store was removed and the file ledger is
/// still the authority. `Error::Invariant` (4): the legacy state changed during the operation or
/// the parity check failed; an operator must look.
pub fn run(repo: &Path) -> Result<Outcome> {
    let state: PathBuf = repo.join(STATE_REL);
    match epoch(&state) {
        Epoch::Store(authority) => return Ok(already_cut_over(&state, authority)),
        Epoch::Inconsistent(reason) => {
            return Err(Error::invariant(format!(
                "the state directory is cut over but inconsistent: {reason}"
            )));
        }
        Epoch::FileLedger => {}
    }
    preconditions(&state)?;
    let held: LegacyLock = lock::acquire(&state)?;
    let outcome = locked(repo, &state);
    let released = held.release();
    let authority = outcome?;
    // A lock that cannot be removed after the commit point is no failure: the next migrate
    // removes it when its owner is dead.
    let _ = released;
    Ok(Outcome {
        already_cut_over: false,
        authority,
    })
}
