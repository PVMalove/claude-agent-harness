//! The legacy ledger lock, taken by the migrator (ADR 0020, section 5, step 2).
//!
//! The protocol is the one of the Python ledger (`LifecycleLedger.lock`): a directory
//! `.coordinator.lock` is created, and whoever creates its `owner.json` exclusively holds the lock.
//! The owner record names `operation: runtime-cutover`. On Unix the migrator also keeps an
//! exclusive `flock` on `owner.json` for the whole operation, so the kernel drops it when the
//! process dies.
//!
//! A live owner is never removed. A dead owner is removed only by the verdict `owner-dead` that
//! `coordinator.py ledger release-lock` also uses (same host, PID namespace and boot, and a pid that
//! names no process), and the removal is written to `lock-audit/` first.

use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use contracts::ErrorCode;
use serde_json::{Map, Value, json};

use crate::canonical::{canonical_json, sha256_hex};
use crate::error::{Error, Result};
use crate::platform;
use crate::time::iso_now;

pub const LOCK_DIRECTORY: &str = ".coordinator.lock";
pub const LOCK_OWNER: &str = "owner.json";
/// The operating-system lock that lets one release at a time judge and remove the lock.
pub const LOCK_RELEASE_GUARD: &str = ".coordinator.lock.release";
pub const OPERATION: &str = "runtime-cutover";
const LOCK_AUDIT: &str = "lock-audit";

const BUSY_REMEDY: &str = "repeat the command once the other operation finishes; \
    'coordinator.py ledger lock-status' on installation 1.x shows the holder";

/// The lock of the legacy ledger, held until `release`.
#[derive(Debug)]
pub struct LegacyLock {
    lock_dir: PathBuf,
    owner: PathBuf,
    /// The file that carries the `flock`; closing it drops the lock.
    _held: File,
}

fn busy(detail: &str) -> Error {
    Error::refused(
        ErrorCode::StoreBusy,
        format!("the ledger is locked: {detail}"),
        BUSY_REMEDY,
    )
}

fn read_owner(lock_dir: &Path) -> Option<Map<String, Value>> {
    let bytes = fs::read(lock_dir.join(LOCK_OWNER)).ok()?;
    match serde_json::from_slice(&bytes) {
        Ok(Value::Object(owner)) => Some(owner),
        _ => None,
    }
}

/// Why a lock may not be removed, or `None` when its owner is provably dead.
fn why_not_dead(owner: Option<&Map<String, Value>>) -> Option<&'static str> {
    let Some(owner) = owner else {
        return Some("owner-unknown");
    };
    let (Some(pid), Some(host)) = (
        owner
            .get("pid")
            .and_then(Value::as_i64)
            .filter(|pid| *pid > 0),
        owner.get("host").and_then(Value::as_str),
    ) else {
        return Some("owner-unknown");
    };
    if host != platform::hostname() {
        return Some("owner-on-another-host");
    }
    let here = platform::environment();
    let differs = |key: &str, ours: &Option<String>| matches!((owner.get(key).and_then(Value::as_str), ours), (Some(theirs), Some(ours)) if theirs != ours);
    if differs("pid_namespace", &here.pid_namespace) || differs("boot_id", &here.boot_id) {
        return Some("owner-in-other-environment");
    }
    match platform::process_alive(pid) {
        Some(false) => None,
        Some(true) => Some("owner-alive"),
        None => Some("owner-unprovable"),
    }
}

fn write_lock_audit(state: &Path, action: &str, details: Value) -> Result<()> {
    let audit_id = format!("audit-{}", platform::uuid4()?);
    let mut record = json!({
        "audit_id": audit_id,
        "at": iso_now(),
        "action": action,
        "details": details,
    });
    let checksum = sha256_hex(canonical_json(&record).as_bytes());
    record["record_sha256"] = Value::String(checksum);
    let directory = state.join(LOCK_AUDIT);
    fs::create_dir_all(&directory)?;
    platform::write_durable(
        &directory.join(format!("{audit_id}.json")),
        canonical_json(&record).as_bytes(),
    )?;
    platform::sync_dir(&directory)?;
    Ok(())
}

/// Remove the lock of a dead owner, after the removal is audited. The lock is judged and removed
/// under the release guard, so a second release cannot interleave.
fn remove_dead(state: &Path, lock_dir: &Path) -> Result<()> {
    // A live or foreign owner is refused before the guard file is even created.
    if let Some(reason) = why_not_dead(read_owner(lock_dir).as_ref()) {
        return Err(busy(&format!(
            "its owner is not provably dead ({reason}); a live owner is never removed"
        )));
    }
    let guard = OpenOptions::new()
        .read(true)
        .append(true)
        .create(true)
        .open(state.join(LOCK_RELEASE_GUARD))?;
    #[cfg(unix)]
    if guard.try_lock().is_err() {
        return Err(busy("another release of the ledger lock is in progress"));
    }
    let observed = read_owner(lock_dir);
    if let Some(reason) = why_not_dead(observed.as_ref()) {
        return Err(busy(&format!(
            "its owner is not provably dead ({reason}); a live owner is never removed"
        )));
    }
    write_lock_audit(
        state,
        "ledger-lock-owner-dead-release",
        json!({
            "verdict": "owner-dead",
            "released_by": OPERATION,
            "lock": {"path": lock_dir.to_string_lossy(), "owner": observed},
        }),
    )?;
    fs::remove_file(lock_dir.join(LOCK_OWNER)).or_else(|err| {
        if err.kind() == io::ErrorKind::NotFound {
            Ok(())
        } else {
            Err(err)
        }
    })?;
    fs::remove_dir(lock_dir)?;
    drop(guard);
    Ok(())
}

/// Remove the lock a dead migrate or 1.x coordinator left behind, if its owner is provably dead.
/// `Ok(false)` when there is no lock.
///
/// # Errors
/// `Error::Refused` when the owner is alive or cannot be judged.
pub fn release_if_dead(state: &Path) -> Result<bool> {
    let lock_dir = state.join(LOCK_DIRECTORY);
    if !lock_dir.is_dir() {
        return Ok(false);
    }
    remove_dead(state, &lock_dir)?;
    Ok(true)
}

/// Take the lock of the legacy ledger.
///
/// # Errors
/// `Error::Refused` when another operation holds it and its owner is not provably dead.
pub fn acquire(state: &Path) -> Result<LegacyLock> {
    let lock_dir = state.join(LOCK_DIRECTORY);
    let mut removed_dead_owner = false;
    loop {
        match fs::create_dir(&lock_dir) {
            Ok(()) => break,
            Err(err) if err.kind() == io::ErrorKind::AlreadyExists => {
                if removed_dead_owner {
                    return Err(busy("another operation took it first"));
                }
                remove_dead(state, &lock_dir)?;
                removed_dead_owner = true;
            }
            Err(err) => {
                return Err(Error::refused(
                    ErrorCode::Internal,
                    format!("cannot create {}: {err}", lock_dir.display()),
                    "let this process write the state directory, then repeat the command",
                ));
            }
        }
    }
    let owner = lock_dir.join(LOCK_OWNER);
    let environment = platform::environment();
    let mut record = json!({
        "pid": std::process::id(),
        "host": platform::hostname(),
        "acquired_at": iso_now(),
        "operation": OPERATION,
    });
    if let Some(namespace) = environment.pid_namespace {
        record["pid_namespace"] = Value::String(namespace);
    }
    if let Some(boot) = environment.boot_id {
        record["boot_id"] = Value::String(boot);
    }
    let created = OpenOptions::new().write(true).create_new(true).open(&owner);
    let mut held = match created {
        Ok(file) => file,
        Err(err) => {
            let _ = fs::remove_dir(&lock_dir);
            return Err(if err.kind() == io::ErrorKind::AlreadyExists {
                busy("another operation recorded its owner first")
            } else {
                Error::refused(
                    ErrorCode::Internal,
                    format!("cannot record the lock owner: {err}"),
                    "let this process write the state directory, then repeat the command",
                )
            });
        }
    };
    let recorded = held
        .write_all(record.to_string().as_bytes())
        .and_then(|()| held.sync_all());
    #[cfg(unix)]
    let recorded = recorded.and_then(|()| held.try_lock().map_err(io::Error::from));
    if let Err(err) = recorded {
        let _ = fs::remove_file(&owner);
        let _ = fs::remove_dir(&lock_dir);
        return Err(Error::refused(
            ErrorCode::Internal,
            format!("cannot hold the lock owner record: {err}"),
            "make the state directory support file locks, then repeat the command",
        ));
    }
    Ok(LegacyLock {
        lock_dir,
        owner,
        _held: held,
    })
}

impl LegacyLock {
    /// Release the lock: remove the owner record and the directory.
    pub fn release(self) -> io::Result<()> {
        fs::remove_file(&self.owner)?;
        fs::remove_dir(&self.lock_dir)
    }
}
