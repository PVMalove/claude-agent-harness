//! The legacy ledger lock, taken by the migrator and by the rollback (ADR 0020, section 5, step 2).
//!
//! The protocol is the one of the Python ledger (`LifecycleLedger.lock`): a directory
//! `.coordinator.lock` is created, and whoever creates its `owner.json` exclusively holds the lock.
//! The owner record names the operation (`runtime-cutover` or `runtime-rollback`). On Unix the
//! process also keeps an exclusive `flock` on `owner.json` for the whole operation, so the kernel
//! drops it when the process dies.
//!
//! A live owner is never removed. A lock is judged by the verdicts of `coordinator.py ledger
//! release-lock` (`_release_verdict`): `owner-dead` (same host, PID namespace and boot, and a pid
//! that names no process) and `owner-unknown-stale` (an owner that cannot be checked, held for
//! `STALE_AFTER_SECONDS`) remove it; `owner-alive`, `owner-on-another-host`,
//! `owner-in-other-environment` and `owner-unknown-recent` keep it. Every removal is written to
//! `lock-audit/` first, with its verdict, its owner and its time.

use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use contracts::ErrorCode;
use serde_json::{Map, Value, json};

use crate::canonical::{canonical_json, sha256_hex};
use crate::error::{Error, Result};
use crate::identity::{self, Observer, Probe};
use crate::platform;
use crate::time::{iso_from_micros, iso_now, now_micros, parse_rfc3339};

pub const LOCK_DIRECTORY: &str = ".coordinator.lock";
pub const LOCK_OWNER: &str = "owner.json";
/// The operating-system lock that lets one release at a time judge and remove the lock.
pub const LOCK_RELEASE_GUARD: &str = ".coordinator.lock.release";
/// The operation of `migrate` in the owner record.
pub const OPERATION: &str = "runtime-cutover";
/// The operation of `rollback --to-legacy` in the owner record.
pub const ROLLBACK_OPERATION: &str = "runtime-rollback";
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

/// How long a lock whose owner cannot be checked is held before it may be removed, as
/// `LEDGER_LOCK_STALE_SECONDS` of the Python ledger.
pub const STALE_AFTER_SECONDS: i64 = 3600;

/// A lock as it was observed: its owner record and its age.
#[derive(Debug)]
struct Observed {
    owner: Option<Map<String, Value>>,
    acquired_at: i64,
    held_seconds: i64,
}

fn modified_micros(path: &Path) -> Option<i64> {
    let modified = fs::metadata(path).ok()?.modified().ok()?;
    let since_epoch = modified.duration_since(std::time::UNIX_EPOCH).ok()?;
    i64::try_from(since_epoch.as_micros()).ok()
}

/// The lock at `lock_dir` as `LifecycleLedger.lock_state` sees it, or `None` when there is none.
/// The age counts from the recorded `acquired_at`; without one, from the owner record of an owner
/// that died before writing it, or else from the lock directory.
fn observe(lock_dir: &Path) -> Option<Observed> {
    let mut origin = modified_micros(lock_dir)?;
    let owner = read_owner(lock_dir);
    if owner.is_none()
        && let Some(record) = modified_micros(&lock_dir.join(LOCK_OWNER))
    {
        origin = record;
    }
    let acquired_at = owner
        .as_ref()
        .and_then(|owner| owner.get("acquired_at"))
        .and_then(Value::as_str)
        .and_then(parse_rfc3339)
        .unwrap_or(origin);
    Some(Observed {
        owner,
        acquired_at,
        held_seconds: ((now_micros() - acquired_at) / 1_000_000).max(0),
    })
}

/// Whether the recorded owner names a PID namespace or a boot that differs from the observer's.
/// Only a value both sides know is compared, as `_other_environment` of the Python ledger does.
fn other_environment(owner: &Map<String, Value>, observer: &Observer) -> bool {
    [
        ("pid_namespace", &observer.pid_namespace),
        ("boot_id", &observer.boot_id),
    ]
    .iter()
    .any(|(key, ours)| {
        matches!(
            (owner.get(*key).and_then(Value::as_str), ours),
            (Some(theirs), Some(ours)) if theirs != ours
        )
    })
}

/// Whether the lock may be removed, and the verdict: exactly `_release_verdict` of
/// `coordinator.py ledger release-lock`. A recorded owner on this host, PID namespace and boot is
/// judged by its process, and a live one is never removed. An owner that cannot be checked, from
/// another environment or without a usable record, is removed only after `STALE_AFTER_SECONDS`.
/// An owner of another host is never removed.
fn legacy_verdict(
    owner: Option<&Map<String, Value>>,
    held_seconds: i64,
    observer: &Observer,
    probe: impl Fn(i64) -> Probe,
) -> (bool, &'static str) {
    let stale = held_seconds >= STALE_AFTER_SECONDS;
    let recorded = owner.and_then(|owner| {
        let pid = owner
            .get("pid")
            .and_then(Value::as_i64)
            .filter(|pid| *pid > 0)?;
        let host = owner.get("host").and_then(Value::as_str)?;
        Some((owner, pid, host))
    });
    let Some((owner, pid, host)) = recorded else {
        return if stale {
            (true, "owner-unknown-stale")
        } else {
            (false, "owner-unknown-recent")
        };
    };
    if host != observer.host {
        return (false, "owner-on-another-host");
    }
    if other_environment(owner, observer) {
        return if stale {
            (true, "owner-unknown-stale")
        } else {
            (false, "owner-in-other-environment")
        };
    }
    // A process the platform cannot see is treated as alive, as `_pid_active` does.
    match probe(pid) {
        Probe::Absent => (true, "owner-dead"),
        Probe::Running(_) | Probe::Unknown => (false, "owner-alive"),
    }
}

fn judge(observed: &Observed) -> (bool, &'static str) {
    legacy_verdict(
        observed.owner.as_ref(),
        observed.held_seconds,
        &identity::observer(),
        identity::probe,
    )
}

fn not_released(verdict: &str) -> Error {
    busy(&format!(
        "the lock is not released ({verdict}); a live owner is never removed, and an owner that \
         cannot be checked only once the lock has been held for {STALE_AFTER_SECONDS} seconds"
    ))
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

/// Remove the lock of an owner that `legacy_verdict` releases, after the removal is audited with
/// its verdict, its owner and its time. The lock is judged and removed under the release guard, so
/// a second release cannot interleave.
fn remove_dead(state: &Path, lock_dir: &Path, operation: &str) -> Result<()> {
    // A live or foreign owner is refused before the guard file is even created.
    if let Some(observed) = observe(lock_dir) {
        let (releasable, verdict) = judge(&observed);
        if !releasable {
            return Err(not_released(verdict));
        }
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
    let Some(observed) = observe(lock_dir) else {
        return Ok(());
    };
    let (releasable, verdict) = judge(&observed);
    if !releasable {
        return Err(not_released(verdict));
    }
    let action = if verdict == "owner-dead" {
        "ledger-lock-owner-dead-release"
    } else {
        "ledger-lock-stale-release"
    };
    write_lock_audit(
        state,
        action,
        json!({
            "verdict": verdict,
            "released_by": operation,
            "lock": {
                "path": lock_dir.to_string_lossy(),
                "owner": observed.owner,
                "acquired_at": iso_from_micros(observed.acquired_at),
                "held_seconds": observed.held_seconds,
            },
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

/// Remove the lock a dead migrate or 1.x coordinator left behind, if its verdict releases it.
/// `Ok(false)` when there is no lock.
///
/// # Errors
/// `Error::Refused` with the verdict when the lock stays.
pub fn release_if_dead(state: &Path) -> Result<bool> {
    let lock_dir = state.join(LOCK_DIRECTORY);
    if !lock_dir.is_dir() {
        return Ok(false);
    }
    remove_dead(state, &lock_dir, OPERATION)?;
    Ok(true)
}

/// Take the lock of the legacy ledger for `operation`, which the owner record names.
///
/// # Errors
/// `Error::Refused` when another operation holds it and its verdict keeps it.
pub fn acquire(state: &Path, operation: &str) -> Result<LegacyLock> {
    let lock_dir = state.join(LOCK_DIRECTORY);
    let mut removed_dead_owner = false;
    loop {
        match fs::create_dir(&lock_dir) {
            Ok(()) => break,
            Err(err) if err.kind() == io::ErrorKind::AlreadyExists => {
                if removed_dead_owner {
                    return Err(busy("another operation took it first"));
                }
                remove_dead(state, &lock_dir, operation)?;
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
        "operation": operation,
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
