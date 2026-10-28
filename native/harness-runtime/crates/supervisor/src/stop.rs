//! The end of an execution: the exit a watcher reaps, and the stop at `execution_deadline`
//! (ADR 0022, section 3).
//!
//! The stop of a `running` execution (never of an `unknown` one): the row becomes `stopping` with
//! the lease still held, SIGTERM goes to the recorded process group, SIGKILL follows after
//! `stop_grace_seconds` when a member is still there, and the stop waits until the leader is gone
//! and the group is empty. Then one transaction records `exited` with the exit evidence (the
//! wait status of a child of this runtime, or `stop-observed` for an adopted process, the signal
//! times and the size and SHA-256 of both logs) and releases the lease. An observation that fails
//! during the stop moves the row to `unknown` and keeps the lease. The group id comes only from
//! the recorded identity, never from a bare pid.

use std::os::unix::process::ExitStatusExt;
use std::process::{Child, ExitStatus};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use contracts::{ErrorBody, ExecutionState};
use nix::errno::Errno;
use nix::sys::signal::{Signal, killpg};
use nix::unistd::Pid;
use serde_json::{Value, json};
use state::canonical::sha256_hex;
use state::executions::{self, Change, Execution};
use state::identity::{self, Identity, Verdict};
use state::time::{iso_now, now_micros, parse_rfc3339};

use crate::Supervisor;
use crate::launch::{guard, sql};

/// How often a watcher or a stop looks at the process.
const POLL: Duration = Duration::from_millis(50);
/// How long a stop waits after SIGKILL before it calls the outcome unknown.
const KILL_PROOF_SECONDS: i64 = 30;

/// Whether the RFC 3339 time `deadline` has passed. A deadline that cannot be read never passes.
pub(crate) fn past(deadline: &str) -> bool {
    parse_rfc3339(deadline).is_some_and(|deadline| now_micros() >= deadline)
}

/// How the stop proves that the group leader is gone.
enum Leader<'a> {
    /// A child of this runtime: its wait status.
    Child(&'a mut Child),
    /// A process another runtime started: the verdict on its recorded identity.
    Adopted(&'a Identity),
}

/// What the stop saw of the leader: gone (with the wait status of a child), alive, or nothing
/// it can prove.
enum Seen {
    Gone(Option<ExitStatus>),
    Alive,
    Unproven(String),
}

impl Leader<'_> {
    fn look(&mut self) -> Seen {
        match self {
            Self::Child(child) => match child.try_wait() {
                Ok(Some(status)) => Seen::Gone(Some(status)),
                Ok(None) => Seen::Alive,
                Err(err) => Seen::Unproven(format!("the wait status cannot be read: {err}")),
            },
            Self::Adopted(recorded) => {
                let observer = identity::observer();
                match identity::verdict(Some(recorded), &observer, &identity::probe(recorded.pid)) {
                    Verdict::Dead | Verdict::PidReused => Seen::Gone(None),
                    Verdict::Alive => Seen::Alive,
                    other => Seen::Unproven(other.as_str().to_owned()),
                }
            }
        }
    }
}

/// Whether a process of the group `pgid` is still there; `Err` when the kernel cannot tell.
fn group_alive(pgid: Pid) -> Result<bool, Errno> {
    match killpg(pgid, None) {
        Ok(()) | Err(Errno::EPERM) => Ok(true),
        Err(Errno::ESRCH) => Ok(false),
        Err(err) => Err(err),
    }
}

/// Send `signal` to the group; a group that is gone already is no failure.
fn signal_group(pgid: Pid, signal: Signal) -> Result<(), String> {
    match killpg(pgid, signal) {
        Ok(()) | Err(Errno::ESRCH) => Ok(()),
        Err(err) => Err(format!("{signal} to the process group failed: {err}")),
    }
}

/// The size and SHA-256 of each log of an execution, as far as it was written.
fn logs(paths: &[std::path::PathBuf]) -> Value {
    Value::Array(
        paths
            .iter()
            .map(|path| match std::fs::read(path) {
                Ok(bytes) => json!({
                    "path": path,
                    "bytes": bytes.len(),
                    "sha256": sha256_hex(&bytes),
                }),
                Err(err) => json!({"path": path, "error": err.to_string()}),
            })
            .collect(),
    )
}

impl Supervisor {
    /// One transition of the execution `id` from `from`, under its mutex. Returns whether the row
    /// was still in `from`.
    fn locked(
        &self,
        id: &str,
        from: ExecutionState,
        to: ExecutionState,
        change: Change,
    ) -> Result<bool, ErrorBody> {
        let lock = self.lock_of(id);
        let _held = guard(&lock);
        let mut connection = executions::connect(self.state()?)?;
        let tx = executions::begin(&mut connection)?;
        let Some(current) = executions::load(&tx, id)?.filter(|current| current.state == from)
        else {
            return Ok(false);
        };
        executions::transition(&tx, &current, to, change, &iso_now())?;
        sql(tx.commit())?;
        Ok(true)
    }

    /// Record `to` until the store takes it: a busy store delays the evidence, it never drops it
    /// while the runtime lives.
    fn record(&self, id: &str, from: ExecutionState, to: ExecutionState, change: &Change) {
        while self.locked(id, from, to, change.clone()).is_err() {
            thread::sleep(Duration::from_millis(500));
        }
    }

    fn log_paths(&self, id: &str) -> Vec<std::path::PathBuf> {
        self.logs(id)
            .map(|(stdout, stderr)| vec![stdout, stderr])
            .unwrap_or_default()
    }

    /// Wait for a child of this runtime on its own thread: its exit, or its deadline.
    pub(crate) fn watch(self: &Arc<Self>, id: &str, child: Child, pgid: i64, deadline: &str) {
        guard(&self.inner.watched).insert(id.to_owned());
        let supervisor = Arc::clone(self);
        let (id, deadline) = (id.to_owned(), deadline.to_owned());
        thread::spawn(move || {
            supervisor.wait_for(&id, child, pgid, &deadline);
            guard(&supervisor.inner.watched).remove(&id);
        });
    }

    fn wait_for(&self, id: &str, mut child: Child, pgid: i64, deadline: &str) {
        loop {
            match child.try_wait() {
                Ok(Some(status)) => {
                    let exit = json!({
                        "exit_code": status.code(),
                        "signal": status.signal(),
                        "source": "wait-status",
                        "stopped_by": null,
                        "ended_at": iso_now(),
                        "logs": logs(&self.log_paths(id)),
                    });
                    let change = Change {
                        exit: Some(exit),
                        facts: json!({"source": "wait-status"}),
                        ..Change::default()
                    };
                    self.record(id, ExecutionState::Running, ExecutionState::Exited, &change);
                    return;
                }
                Ok(None) if past(deadline) => {
                    self.stop(id, pgid, Leader::Child(&mut child));
                    return;
                }
                Ok(None) => thread::sleep(POLL),
                // The wait status is lost: reconcile observes the identity from now on.
                Err(_) => return,
            }
        }
    }

    /// Stop an adopted `running` execution past its deadline on its own thread.
    pub(crate) fn stop_adopted(self: &Arc<Self>, execution: &Execution) {
        let (Some(recorded), Some(pgid)) = (execution.identity.clone(), execution.pgid) else {
            return;
        };
        if !guard(&self.inner.watched).insert(execution.id.clone()) {
            return;
        }
        let supervisor = Arc::clone(self);
        let id = execution.id.clone();
        thread::spawn(move || {
            supervisor.stop(&id, pgid, Leader::Adopted(&recorded));
            guard(&supervisor.inner.watched).remove(&id);
        });
    }

    /// The deadline stop of a `running` execution.
    fn stop(&self, id: &str, pgid: i64, mut leader: Leader<'_>) {
        let term_sent_at = iso_now();
        let stopping = Change {
            observation: Some(json!({"stopped_by": "deadline", "term_sent_at": term_sent_at})),
            facts: json!({"stopped_by": "deadline", "term_sent_at": term_sent_at}),
            ..Change::default()
        };
        if !matches!(
            self.locked(
                id,
                ExecutionState::Running,
                ExecutionState::Stopping,
                stopping
            ),
            Ok(true)
        ) {
            return;
        }
        let Ok(group) = i32::try_from(pgid).map(Pid::from_raw) else {
            self.unproven(id, "the recorded process group is not a pid");
            return;
        };
        if let Err(reason) = signal_group(group, Signal::SIGTERM) {
            self.unproven(id, &reason);
            return;
        }
        let grace = self.grace_of(id);
        let term_at = now_micros();
        let mut kill_sent_at: Option<String> = None;
        let mut kill_at = 0;
        let mut status = None;
        let mut leader_gone = false;
        loop {
            if !leader_gone {
                match leader.look() {
                    Seen::Gone(reaped) => {
                        leader_gone = true;
                        status = reaped;
                    }
                    Seen::Alive => {}
                    Seen::Unproven(reason) => {
                        self.unproven(id, &reason);
                        return;
                    }
                }
            }
            match group_alive(group) {
                Ok(false) if leader_gone => break,
                Ok(_) => {}
                Err(err) => {
                    self.unproven(id, &format!("the process group cannot be observed: {err}"));
                    return;
                }
            }
            let now = now_micros();
            if kill_sent_at.is_none() && now >= term_at + grace * 1_000_000 {
                if let Err(reason) = signal_group(group, Signal::SIGKILL) {
                    self.unproven(id, &reason);
                    return;
                }
                kill_sent_at = Some(iso_now());
                kill_at = now;
            }
            if kill_sent_at.is_some() && now >= kill_at + KILL_PROOF_SECONDS * 1_000_000 {
                self.unproven(id, "the process group outlived SIGKILL");
                return;
            }
            thread::sleep(POLL);
        }
        let source = if status.is_some() {
            "wait-status"
        } else {
            "stop-observed"
        };
        let exit = json!({
            "exit_code": status.and_then(|status| status.code()),
            "signal": status.and_then(|status| status.signal()),
            "source": source,
            "stopped_by": "deadline",
            "term_sent_at": term_sent_at,
            "kill_sent_at": kill_sent_at,
            "ended_at": iso_now(),
            "logs": logs(&self.log_paths(id)),
        });
        let change = Change {
            exit: Some(exit),
            facts: json!({"source": source, "stopped_by": "deadline"}),
            ..Change::default()
        };
        self.record(
            id,
            ExecutionState::Stopping,
            ExecutionState::Exited,
            &change,
        );
    }

    /// The pinned grace of the execution `id`; 0 when it cannot be read.
    fn grace_of(&self, id: &str) -> i64 {
        self.state()
            .ok()
            .and_then(|state| executions::connect(state).ok())
            .and_then(|connection| executions::load(&connection, id).ok().flatten())
            .map_or(0, |execution| execution.stop_grace_seconds)
    }

    /// A stop that cannot prove its outcome: `unknown`, and the lease stays held.
    fn unproven(&self, id: &str, reason: &str) {
        let change = Change {
            facts: json!({"stopped_by": "deadline", "unproven": reason}),
            ..Change::default()
        };
        self.record(
            id,
            ExecutionState::Stopping,
            ExecutionState::Unknown,
            &change,
        );
    }
}
