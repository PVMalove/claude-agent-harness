//! The QA lease of the store: one lane, a fencing token and the identity of its owner (ADR 0020,
//! sections 3 and 7; ADR 0021, section 5).
//!
//! The lane is the singleton row `qa_lane` of schema version 3. The typed mutations of
//! `state.write` on `qa-lane/lease.json` change it in the `qa_lane` class of the request's
//! transaction:
//!
//! - `lease_acquire {owner_kind, owner_id, ttl_seconds}` takes a free lane, raises the fencing
//!   token by one and records the identity of the IPC peer, never a pid from the request;
//! - `lease_release` and `lease_heartbeat` need the owner and the fencing token of the lease; any
//!   other pair is `fencing_mismatch` and changes nothing;
//! - `lease_reap` names the lease a command observed expired: it is cleared only when the owner is
//!   provably dead (`owner-dead`); any other verdict is `lease_held` with the facts
//!   `stale_heartbeat`, `verdict`, `owner`, `expires_at` and `heartbeat_at`;
//! - `lease_clear` is the operator override: a human approval and a reason, whatever the verdict.
//!
//! The row stays after a release, so the fencing token never goes back. Every change appends one
//! audit record with an existing action (`immutable-record`, `transition`, `deletion`) and the
//! details `lease_op`, `fencing_token`, `owner`, and for a reap or a clear `verdict`, `identity`
//! and `observed`.

use std::path::Path;

use contracts::{ErrorBody, ErrorCode, FailureDomain};
use rusqlite::{Connection, OpenFlags, OptionalExtension, Transaction};
use serde_json::{Map, Value, json};

use crate::authority::{Epoch, STORE_NAME, epoch};
use crate::identity::{self, Identity, Probe, Verdict};
use crate::read::ReadError;
use crate::time::{iso_from_micros, now_micros, parse_rfc3339};
use crate::write::{compact, failure, free_lane, sha_of, sqlite};

/// The path of the lease in the legacy generation.
pub const LEASE_PATH: &str = "qa-lane/lease.json";
/// The longest lease a request may ask for.
pub const MAX_TTL_SECONDS: i64 = 86_400;

/// A typed lease operation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Acquire,
    Release,
    Heartbeat,
    Reap,
    Clear,
}

impl Kind {
    /// The kind a mutation `op` names, if it names a lease operation.
    pub fn of(op: &str) -> Option<Self> {
        Some(match op {
            "lease_acquire" => Self::Acquire,
            "lease_release" => Self::Release,
            "lease_heartbeat" => Self::Heartbeat,
            "lease_reap" => Self::Reap,
            "lease_clear" => Self::Clear,
            _ => return None,
        })
    }

    /// The `lease_op` of the audit record.
    pub const fn name(self) -> &'static str {
        match self {
            Self::Acquire => "acquire",
            Self::Release => "release",
            Self::Heartbeat => "heartbeat",
            Self::Reap => "reap",
            Self::Clear => "clear",
        }
    }
}

/// The arguments of a lease operation (the `lease` object of the mutation).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LeaseRequest {
    pub kind: Kind,
    pub owner_kind: String,
    pub owner_id: String,
    /// The token of the lease the command holds or observed; every operation but acquire.
    pub fencing_token: Option<i64>,
    /// The lifetime from now; acquire and heartbeat.
    pub ttl_seconds: Option<i64>,
}

/// Parse the `lease` object of a lease mutation.
///
/// # Errors
/// The reason the arguments are not the ones `kind` takes.
pub fn parse(kind: Kind, arguments: Option<&Value>) -> Result<LeaseRequest, String> {
    let Some(Value::Object(members)) = arguments else {
        return Err("a lease operation carries a lease object".to_owned());
    };
    let takes_token = kind != Kind::Acquire;
    let takes_ttl = matches!(kind, Kind::Acquire | Kind::Heartbeat);
    if let Some(unknown) = members.keys().find(|key| {
        !matches!(key.as_str(), "owner_kind" | "owner_id")
            && !(takes_token && key.as_str() == "fencing_token")
            && !(takes_ttl && key.as_str() == "ttl_seconds")
    }) {
        return Err(format!("unknown lease field {unknown:?}"));
    }
    let text = |key: &str| {
        members
            .get(key)
            .and_then(Value::as_str)
            .filter(|text| !text.is_empty())
            .map(str::to_owned)
            .ok_or_else(|| format!("lease {key} must be a non-empty string"))
    };
    let number = |key: &str, low: i64, high: i64| {
        members
            .get(key)
            .and_then(Value::as_i64)
            .filter(|value| (low..=high).contains(value))
            .ok_or_else(|| format!("lease {key} must be an integer from {low} to {high}"))
    };
    Ok(LeaseRequest {
        kind,
        owner_kind: text("owner_kind")?,
        owner_id: text("owner_id")?,
        fencing_token: takes_token
            .then(|| number("fencing_token", 1, i64::MAX))
            .transpose()?,
        ttl_seconds: takes_ttl
            .then(|| number("ttl_seconds", 1, MAX_TTL_SECONDS))
            .transpose()?,
    })
}

/// A lease the lane holds.
#[derive(Clone, Debug)]
pub struct Held {
    pub owner_kind: String,
    pub owner_id: String,
    pub acquired_at: String,
    pub expires_at: String,
    pub heartbeat_at: String,
    /// `None` for a lease of schema version 2 and for an owner the runtime could not observe.
    pub identity: Option<Identity>,
    /// The PID namespace of the owner process itself, as evidence; it may differ from the
    /// namespace that numbers the pid.
    pub owner_pid_namespace: Option<String>,
}

/// The lane row.
#[derive(Clone, Debug)]
pub struct Lane {
    pub revision: i64,
    pub fencing_token: i64,
    pub body: Map<String, Value>,
    pub held: Option<Held>,
}

/// The lane row, or `None` when no lease was ever taken.
///
/// # Errors
/// A store that cannot be read.
pub fn load(connection: &Connection) -> rusqlite::Result<Option<Lane>> {
    connection
        .query_row(
            "SELECT revision, fencing_token, body_json, owner_kind, owner_id, acquired_at,
                    expires_at, heartbeat_at, host, boot_id, pid_namespace, pid, start_time,
                    owner_pid_namespace
             FROM qa_lane WHERE id = 1",
            [],
            |row| {
                let body: String = row.get(2)?;
                let owner_kind: Option<String> = row.get(3)?;
                let identity = match (
                    row.get::<_, Option<String>>(8)?,
                    row.get::<_, Option<i64>>(11)?,
                    row.get::<_, Option<String>>(12)?,
                ) {
                    (Some(host), Some(pid), Some(start_time)) => Some(Identity {
                        host,
                        boot_id: row.get(9)?,
                        pid_namespace: row.get(10)?,
                        pid,
                        start_time,
                    }),
                    _ => None,
                };
                let held = match owner_kind {
                    Some(owner_kind) => Some(Held {
                        owner_kind,
                        owner_id: row.get(4)?,
                        acquired_at: row.get(5)?,
                        expires_at: row.get(6)?,
                        heartbeat_at: row.get(7)?,
                        identity,
                        owner_pid_namespace: row.get(13)?,
                    }),
                    None => None,
                };
                Ok(Lane {
                    revision: row.get(0)?,
                    fencing_token: row.get(1)?,
                    body: match serde_json::from_str::<Value>(&body) {
                        Ok(Value::Object(body)) => body,
                        _ => Map::new(),
                    },
                    held,
                })
            },
        )
        .optional()
}

impl Held {
    fn owner(&self) -> Value {
        json!({"owner_kind": self.owner_kind, "owner_id": self.owner_id})
    }

    /// Whether the lease has expired at `now` (microseconds); an expiry that is no time is.
    pub fn expired(&self, now: i64) -> bool {
        parse_rfc3339(&self.expires_at).is_none_or(|expires| expires <= now)
    }

    fn body(&self, fencing_token: i64) -> Map<String, Value> {
        let mut body = Map::new();
        body.insert("owner_kind".to_owned(), json!(self.owner_kind));
        body.insert("owner_id".to_owned(), json!(self.owner_id));
        body.insert("acquired_at".to_owned(), json!(self.acquired_at));
        body.insert("expires_at".to_owned(), json!(self.expires_at));
        body.insert("heartbeat_at".to_owned(), json!(self.heartbeat_at));
        body.insert("fencing_token".to_owned(), json!(fencing_token));
        body.insert(
            "identity".to_owned(),
            self.identity
                .as_ref()
                .map_or(Value::Null, Identity::to_json),
        );
        body
    }
}

/// The verdict on the owner of `held` now, and what the runtime observed to reach it.
pub fn judge(held: &Held) -> (Verdict, Value) {
    let observer = identity::observer();
    let probe = held
        .identity
        .as_ref()
        .map_or(Probe::Unknown, |owner| identity::probe(owner.pid));
    let verdict = identity::verdict(held.identity.as_ref(), &observer, &probe);
    let (state, start_time) = match &probe {
        Probe::Absent => ("absent", None),
        Probe::Running(start_time) => ("running", start_time.clone()),
        Probe::Unknown => ("unknown", None),
    };
    let observed = json!({
        "at": iso_from_micros(now_micros()),
        "host": observer.host,
        "boot_id": observer.boot_id,
        "pid_namespace": observer.pid_namespace,
        "process": {"state": state, "start_time": start_time},
    });
    (verdict, observed)
}

/// What a lease operation did, for the audit chain and the answer of the request.
pub(crate) struct Applied {
    pub action: &'static str,
    pub details: Value,
    /// The revision of `qa-lane/lease.json` after the operation; `None` once it is released.
    pub revision: Option<i64>,
    /// The lease after the operation, or `null`.
    pub lease: Value,
}

fn lease_held(held: &Held, now: i64, verdict: Option<Verdict>) -> ErrorBody {
    let mut error = failure(
        ErrorCode::LeaseHeld,
        FailureDomain::Policy,
        true,
        format!(
            "the QA lane is held by {} {} until {}",
            held.owner_kind, held.owner_id, held.expires_at
        ),
        "wait for the owner to release the lane; a lease whose owner is provably dead is reaped \
         after it expires, and an operator clears any other lease with an approval",
    )
    .with_fact("owner", held.owner())
    .with_fact("expires_at", held.expires_at.clone())
    .with_fact("heartbeat_at", held.heartbeat_at.clone())
    .with_fact("stale_heartbeat", held.expired(now));
    if let Some(verdict) = verdict {
        error = error.with_fact("verdict", verdict.as_str());
    }
    error
}

fn fencing_mismatch(request: &LeaseRequest, lane: Option<&Lane>) -> ErrorBody {
    let held = lane.and_then(|lane| lane.held.as_ref());
    failure(
        ErrorCode::FencingMismatch,
        FailureDomain::Policy,
        false,
        format!(
            "the QA lease is not the one of {} {} with the fencing token {}",
            request.owner_kind,
            request.owner_id,
            request.fencing_token.unwrap_or(0)
        ),
        "read the QA lane again: another owner holds it now, or an operator cleared the lease",
    )
    .with_fact(
        "expected",
        json!({"owner_kind": request.owner_kind, "owner_id": request.owner_id,
               "fencing_token": request.fencing_token}),
    )
    .with_fact(
        "actual",
        held.map_or(Value::Null, |held| {
            json!({"owner_kind": held.owner_kind, "owner_id": held.owner_id,
                   "fencing_token": lane.map(|lane| lane.fencing_token)})
        }),
    )
}

/// The lease `request` names, held by its owner with its fencing token.
fn named<'a>(request: &LeaseRequest, lane: Option<&'a Lane>) -> Result<&'a Held, ErrorBody> {
    match lane.and_then(|lane| lane.held.as_ref().map(|held| (lane, held))) {
        Some((lane, held))
            if held.owner_kind == request.owner_kind
                && held.owner_id == request.owner_id
                && Some(lane.fencing_token) == request.fencing_token =>
        {
            Ok(held)
        }
        _ => Err(fencing_mismatch(request, lane)),
    }
}

/// Write `held` with `fencing_token` to the lane row, creating the row when the lane never had
/// one. Returns the revision and the body.
fn store(
    tx: &Transaction<'_>,
    held: &Held,
    fencing_token: i64,
) -> Result<(i64, Map<String, Value>), ErrorBody> {
    let body = held.body(fencing_token);
    let identity = held.identity.as_ref();
    sqlite(tx.execute(
        "INSERT INTO qa_lane (id, revision, fencing_token, body_json, owner_kind, owner_id,
                              acquired_at, expires_at, heartbeat_at, host, boot_id, pid_namespace,
                              pid, start_time, owner_pid_namespace)
         VALUES (1, 1, ?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13)
         ON CONFLICT (id) DO UPDATE SET
             revision = revision + 1, fencing_token = excluded.fencing_token,
             body_json = excluded.body_json, owner_kind = excluded.owner_kind,
             owner_id = excluded.owner_id, acquired_at = excluded.acquired_at,
             expires_at = excluded.expires_at, heartbeat_at = excluded.heartbeat_at,
             host = excluded.host, boot_id = excluded.boot_id,
             pid_namespace = excluded.pid_namespace, pid = excluded.pid,
             start_time = excluded.start_time, owner_pid_namespace = excluded.owner_pid_namespace",
        rusqlite::params![
            fencing_token,
            compact(&body),
            held.owner_kind,
            held.owner_id,
            held.acquired_at,
            held.expires_at,
            held.heartbeat_at,
            identity.map(|owner| owner.host.clone()),
            identity.and_then(|owner| owner.boot_id.clone()),
            identity.and_then(|owner| owner.pid_namespace.clone()),
            identity.map(|owner| owner.pid),
            identity.map(|owner| owner.start_time.clone()),
            held.owner_pid_namespace,
        ],
    ))?;
    let revision = sqlite(
        tx.query_row("SELECT revision FROM qa_lane WHERE id = 1", [], |row| {
            row.get(0)
        }),
    )?;
    Ok((revision, body))
}

/// The PID namespace of the process `pid` itself, where the platform reveals it.
fn own_namespace(pid: i64) -> Option<String> {
    #[cfg(target_os = "linux")]
    {
        std::fs::read_link(format!("/proc/{pid}/ns/pid"))
            .ok()
            .map(|target| target.to_string_lossy().into_owned())
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = pid;
        None
    }
}

fn later(now: i64, seconds: i64) -> String {
    iso_from_micros(now + seconds * 1_000_000)
}

/// Apply one lease operation in the transaction of a `state.write` request. `peer_pid` is the pid
/// of the IPC client as the kernel reported it; a request never names it.
pub(crate) fn apply(
    tx: &Transaction<'_>,
    request: &LeaseRequest,
    reason: Option<&str>,
    decision: Option<&Value>,
    peer_pid: Option<u32>,
) -> Result<Applied, ErrorBody> {
    let lane = sqlite(load(tx))?;
    let now = now_micros();
    let at = iso_from_micros(now);
    let before = lane
        .as_ref()
        .filter(|lane| lane.held.is_some())
        .map(|lane| sha_of(&lane.body));
    match request.kind {
        Kind::Acquire => {
            if let Some(held) = lane.as_ref().and_then(|lane| lane.held.as_ref()) {
                return Err(lease_held(held, now, None));
            }
            let pid = peer_pid.map(i64::from).filter(|pid| *pid > 0);
            let observer = identity::observer();
            let held = Held {
                owner_kind: request.owner_kind.clone(),
                owner_id: request.owner_id.clone(),
                acquired_at: at.clone(),
                expires_at: later(now, request.ttl_seconds.unwrap_or(1)),
                heartbeat_at: at,
                identity: pid.and_then(|pid| identity::observe(pid, &observer)),
                owner_pid_namespace: pid.and_then(own_namespace),
            };
            let fencing_token = lane.as_ref().map_or(0, |lane| lane.fencing_token) + 1;
            let (revision, body) = store(tx, &held, fencing_token)?;
            let sha256 = sha_of(&body);
            Ok(Applied {
                action: "immutable-record",
                details: json!({
                    "path": LEASE_PATH,
                    "sha256": sha256,
                    "lease_op": request.kind.name(),
                    "fencing_token": fencing_token,
                    "owner": held.owner(),
                    "identity": held.identity.as_ref().map_or(Value::Null, Identity::to_json),
                }),
                revision: Some(revision),
                lease: Value::Object(body),
            })
        }
        Kind::Heartbeat => {
            let held = named(request, lane.as_ref())?;
            let fencing_token = request.fencing_token.unwrap_or(0);
            let renewed = Held {
                expires_at: later(now, request.ttl_seconds.unwrap_or(1)),
                heartbeat_at: at,
                ..held.clone()
            };
            let (revision, body) = store(tx, &renewed, fencing_token)?;
            Ok(Applied {
                action: "transition",
                details: json!({
                    "path": LEASE_PATH,
                    "before_sha256": before,
                    "sha256": sha_of(&body),
                    "lease_op": request.kind.name(),
                    "fencing_token": fencing_token,
                    "owner": held.owner(),
                }),
                revision: Some(revision),
                lease: Value::Object(body),
            })
        }
        Kind::Release | Kind::Reap | Kind::Clear => {
            let held = named(request, lane.as_ref())?;
            let mut details = Map::new();
            if request.kind != Kind::Release {
                let (verdict, observed) = judge(held);
                if request.kind == Kind::Reap {
                    if !held.expired(now) || verdict != Verdict::Dead {
                        return Err(lease_held(held, now, Some(verdict)));
                    }
                } else {
                    let approved_by = decision
                        .and_then(|decision| decision.get("approved_by"))
                        .and_then(Value::as_str)
                        .unwrap_or_default();
                    if approved_by.trim().is_empty() || approved_by.starts_with("policy:") {
                        return Err(failure(
                            ErrorCode::PolicyDenied,
                            FailureDomain::Policy,
                            false,
                            "clearing a QA lease needs a human approval",
                            "pass the approval of the operator who decided to clear the lease",
                        ));
                    }
                    details.insert("decision".to_owned(), decision.cloned().unwrap_or_default());
                }
                details.insert("verdict".to_owned(), json!(verdict.as_str()));
                details.insert(
                    "identity".to_owned(),
                    held.identity
                        .as_ref()
                        .map_or(Value::Null, Identity::to_json),
                );
                details.insert("observed".to_owned(), observed);
                details.insert("expires_at".to_owned(), json!(held.expires_at));
                details.insert("heartbeat_at".to_owned(), json!(held.heartbeat_at));
            }
            free_lane(tx)?;
            details.insert("path".to_owned(), json!(LEASE_PATH));
            details.insert("sha256".to_owned(), json!(before));
            details.insert(
                "reason".to_owned(),
                reason.map_or(Value::Null, |reason| json!(reason)),
            );
            details.insert("lease_op".to_owned(), json!(request.kind.name()));
            details.insert("fencing_token".to_owned(), json!(request.fencing_token));
            details.insert("owner".to_owned(), held.owner());
            Ok(Applied {
                action: "deletion",
                details: Value::Object(details),
                revision: None,
                lease: Value::Null,
            })
        }
    }
}

/// What `qa.lane.inspect` answers: the lease, its fencing token, whether its expiry passed
/// (`stale_heartbeat`) and the verdict on its owner now. It reads without a write lock.
///
/// # Errors
/// See [`ReadError`].
pub fn inspect(state: &Path) -> Result<Value, ReadError> {
    match epoch(state) {
        Epoch::Store(_) => {}
        Epoch::FileLedger => {
            return Err(ReadError::NotAttached(
                "the file ledger is the authority of this repository".to_owned(),
            ));
        }
        Epoch::Inconsistent(reason) => return Err(ReadError::NotAttached(reason)),
    }
    let connection = Connection::open_with_flags(
        state.join(STORE_NAME),
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    connection.busy_timeout(std::time::Duration::from_secs(5))?;
    let lane = load(&connection)?;
    let fencing_token = lane.as_ref().map_or(0, |lane| lane.fencing_token);
    let Some((lane, held)) = lane
        .as_ref()
        .and_then(|lane| lane.held.as_ref().map(|held| (lane, held)))
    else {
        return Ok(json!({
            "lease": null,
            "fencing_token": fencing_token,
            "stale_heartbeat": null,
            "verdict": null,
        }));
    };
    let (verdict, observed) = judge(held);
    Ok(json!({
        "lease": lane.body,
        "revision": lane.revision,
        "fencing_token": fencing_token,
        "owner": held.owner(),
        "acquired_at": held.acquired_at,
        "expires_at": held.expires_at,
        "heartbeat_at": held.heartbeat_at,
        "identity": held.identity.as_ref().map_or(Value::Null, Identity::to_json),
        "owner_pid_namespace": held.owner_pid_namespace,
        "stale_heartbeat": held.expired(now_micros()),
        "verdict": verdict.as_str(),
        "observed": observed,
    }))
}
