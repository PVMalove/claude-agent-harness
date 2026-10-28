//! Read snapshots of the store that span the requests of one command (ADR 0020, section 3).
//!
//! A command reads the store more than once, and a commit of another batch between two of its
//! reads must neither give it half of two states nor refuse it. `state.read` with the payload
//! field `snapshot` serves this:
//!
//! - `"snapshot": "open"` opens a read transaction (a read-only connection, `BEGIN` and its first
//!   `SELECT`), answers the request from it and names it in the answer (`snapshot`);
//! - `"snapshot": "<id>"` answers from the same read transaction, so the command sees the store as
//!   it was when the snapshot was opened; a writer never waits for it (WAL);
//! - `"close": true` with an id ends the read transaction after the answer.
//!
//! The registry keeps a snapshot for at most `IDLE` without a request and `LIFETIME` in all, and
//! holds at most `MAX_SNAPSHOTS`: a snapshot keeps the WAL from being checkpointed past it. A
//! closed or expired id, and an open over the limit, is a retryable refusal; the command reads
//! again from a new snapshot. A request without `snapshot` is one read transaction of its own,
//! with the global `expected_revision` as before.

use std::collections::HashMap;
use std::path::Path;
use std::sync::{Mutex, PoisonError};
use std::time::{Duration, Instant};

use serde_json::Value;

use crate::hooks;
use crate::platform;
use crate::read::{self, Answer, ReadError, Snapshot};

/// How long a snapshot lives without a request.
pub const IDLE: Duration = Duration::from_secs(30);
/// How long a snapshot lives in all.
pub const LIFETIME: Duration = Duration::from_secs(120);
/// How many snapshots may be open at once.
pub const MAX_SNAPSHOTS: usize = 64;

struct Entry {
    /// `None` while a request reads from it.
    snapshot: Option<Snapshot>,
    opened: Instant,
    used: Instant,
}

/// The open snapshots of one serving runtime.
pub struct Registry {
    idle: Duration,
    lifetime: Duration,
    max: usize,
    open: Mutex<HashMap<String, Entry>>,
}

impl std::fmt::Debug for Registry {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Registry")
            .field("idle", &self.idle)
            .field("lifetime", &self.lifetime)
            .field("max", &self.max)
            .finish_non_exhaustive()
    }
}

impl Default for Registry {
    fn default() -> Self {
        Self::with_limits(IDLE, LIFETIME, MAX_SNAPSHOTS)
    }
}

fn expired(detail: impl Into<String>) -> ReadError {
    ReadError::Snapshot {
        reason: "snapshot_expired",
        detail: detail.into(),
    }
}

/// What the snapshot field of a payload asks for.
enum Asked {
    Open,
    Named(String),
}

impl Registry {
    /// A registry with other limits than the defaults (a test uses short ones).
    pub fn with_limits(idle: Duration, lifetime: Duration, max: usize) -> Self {
        Self {
            idle,
            lifetime,
            max,
            open: Mutex::new(HashMap::new()),
        }
    }

    fn entries(&self) -> std::sync::MutexGuard<'_, HashMap<String, Entry>> {
        self.open.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// End every snapshot that has outlived its limits; returns how many stay open.
    fn sweep(&self, now: Instant) -> usize {
        let mut entries = self.entries();
        let stale: Vec<String> = entries
            .iter()
            .filter(|(_, entry)| {
                entry.snapshot.is_some()
                    && (now.duration_since(entry.used) > self.idle
                        || now.duration_since(entry.opened) > self.lifetime)
            })
            .map(|(id, _)| id.clone())
            .collect();
        for id in stale {
            if let Some(Entry {
                snapshot: Some(snapshot),
                ..
            }) = entries.remove(&id)
            {
                snapshot.close();
            }
        }
        entries.len()
    }

    /// How many snapshots are open.
    pub fn len(&self) -> usize {
        self.entries().len()
    }

    /// Whether no snapshot is open.
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Answer one `state.read` request for the store of `state`. See the module documentation
    /// for the fields `snapshot` and `close`.
    ///
    /// # Errors
    /// See [`ReadError`]; `ReadError::Snapshot` for a closed or expired snapshot and for an open
    /// over the limit.
    pub fn read(
        &self,
        state: &Path,
        payload: Option<&Value>,
        expected_revision: Option<u64>,
    ) -> Result<Answer, ReadError> {
        let asked = match payload.and_then(|payload| payload.get("snapshot")) {
            None | Some(Value::Null) => return read::read(state, payload, expected_revision),
            Some(Value::String(text)) if text == "open" => Asked::Open,
            Some(Value::String(id)) if !id.is_empty() => Asked::Named(id.clone()),
            Some(_) => {
                return Err(ReadError::Malformed(
                    "snapshot must be \"open\" or the id of an open snapshot".to_owned(),
                ));
            }
        };
        let close = match payload.and_then(|payload| payload.get("close")) {
            None | Some(Value::Null | Value::Bool(false)) => false,
            Some(Value::Bool(true)) => true,
            Some(_) => return Err(ReadError::Malformed("close must be a boolean".to_owned())),
        };
        let wanted = read::wanted(payload, &["snapshot", "close"])?;
        let now = Instant::now();
        let open = self.sweep(now);
        let (id, snapshot, opened) = match asked {
            Asked::Open => {
                if close {
                    return Err(ReadError::Malformed(
                        "a snapshot that is being opened cannot be closed".to_owned(),
                    ));
                }
                if open >= self.max {
                    return Err(ReadError::Snapshot {
                        reason: "snapshot_limit",
                        detail: format!("{open} snapshots are open, the limit is {}", self.max),
                    });
                }
                let snapshot = read::open(state)?;
                hooks::reach_once("state.read:in-snapshot").map_err(|err| ReadError::Store {
                    message: err.to_string(),
                    busy: false,
                })?;
                let id = format!(
                    "snapshot-{}",
                    platform::random_hex().map_err(|err| ReadError::Store {
                        message: err.to_string(),
                        busy: false,
                    })?
                );
                (id, snapshot, now)
            }
            Asked::Named(id) => {
                let mut entries = self.entries();
                let entry = entries
                    .get_mut(&id)
                    .ok_or_else(|| expired(format!("the snapshot {id} is closed or expired")))?;
                let snapshot = entry.snapshot.take().ok_or_else(|| {
                    expired(format!("the snapshot {id} is answering another request"))
                })?;
                let opened = entry.opened;
                drop(entries);
                (id, snapshot, opened)
            }
        };
        let answer = expected_revision
            .filter(|expected| *expected != snapshot.revision)
            .map_or_else(
                || snapshot.answer(&wanted),
                |expected| {
                    Err(ReadError::RevisionConflict {
                        expected,
                        actual: snapshot.revision,
                    })
                },
            );
        let mut entries = self.entries();
        if close {
            entries.remove(&id);
            snapshot.close();
        } else {
            entries.insert(
                id.clone(),
                Entry {
                    snapshot: Some(snapshot),
                    opened,
                    used: Instant::now(),
                },
            );
        }
        drop(entries);
        answer.map(|mut answer| {
            answer.snapshot = Some(id);
            answer
        })
    }
}
