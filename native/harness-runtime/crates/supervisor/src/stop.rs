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
//! the recorded identity, never from a bare pid, and the leader is looked at again right before
//! each signal. A stop that a runtime before this one began is driven to its proof by reconcile:
//! SIGKILL once the grace has passed since the recorded SIGTERM, then the same evidence.
//!
//! A leader that exits by itself ends its execution only with its group: a member that outlives
//! it keeps the row `running` and the lease held until it ends, or until the deadline stops it.

use std::fs::File;
use std::io::{self, Read};
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::{Child, ExitStatus};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use contracts::{ErrorBody, ExecutionState};
use nix::errno::Errno;
use nix::sys::signal::{Signal, killpg};
use nix::unistd::Pid;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use state::executions::{self, Change, Execution};
use state::identity::{self, Identity, Probe, Verdict};
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

/// The SIGTERM of a stop: the time it was sent, as recorded and in microseconds, and the grace
/// before SIGKILL.
struct StopPlan {
    term_sent_at: String,
    term_at: i64,
    grace_seconds: i64,
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

/// The process group of a recorded group id.
fn group_of(pgid: i64) -> Option<Pid> {
    i32::try_from(pgid).ok().map(Pid::from_raw)
}

/// Whether a process of the group `pgid` is still there; `Err` when the kernel cannot tell.
fn group_alive(pgid: Pid) -> Result<bool, Errno> {
    match killpg(pgid, None) {
        Ok(()) | Err(Errno::EPERM) => Ok(true),
        Err(Errno::ESRCH) => Ok(false),
        Err(err) => Err(err),
    }
}

/// Whether a member of the group `pgid` outlives its gone leader. The kernel gives the number of
/// a group to another process only once the group is empty, and that process may lead a group of
/// the same number: a live process at the leader's pid proves the execution's group gone.
fn members_alive(pgid: Pid) -> Result<bool, Errno> {
    if matches!(identity::probe(i64::from(pgid.as_raw())), Probe::Running(_)) {
        return Ok(false);
    }
    group_alive(pgid)
}

/// Send `signal` to the group; a group that is gone already is no failure.
fn signal_group(pgid: Pid, signal: Signal) -> Result<(), String> {
    match killpg(pgid, signal) {
        Ok(()) | Err(Errno::ESRCH) => Ok(()),
        Err(err) => Err(format!("{signal} to the process group failed: {err}")),
    }
}

/// The size and SHA-256 of one log, read in blocks: a large log never sits in memory whole.
fn digest(path: &Path) -> io::Result<(u64, String)> {
    let mut file = File::open(path)?;
    let mut hasher = Sha256::new();
    let mut block = vec![0_u8; 64 * 1024];
    let mut bytes = 0_u64;
    loop {
        let read = match file.read(&mut block) {
            Ok(0) => break,
            Ok(read) => read,
            Err(err) if err.kind() == io::ErrorKind::Interrupted => continue,
            Err(err) => return Err(err),
        };
        hasher.update(&block[..read]);
        bytes += u64::try_from(read).unwrap_or(u64::MAX);
    }
    let hex = hasher
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    Ok((bytes, hex))
}

/// The size and SHA-256 of each log of an execution, as far as it was written.
fn logs(paths: &[PathBuf]) -> Value {
    Value::Array(
        paths
            .iter()
            .map(|path| match digest(path) {
                Ok((bytes, sha256)) => json!({"path": path, "bytes": bytes, "sha256": sha256}),
                Err(err) => json!({"path": path, "error": err.to_string()}),
            })
            .collect(),
    )
}

impl Supervisor {
    /// One transition of the execution `id` from `from`, under its mutex. Returns the row after
    /// the transition, or `None` when it was no longer in `from`.
    fn locked(
        &self,
        id: &str,
        from: ExecutionState,
        to: ExecutionState,
        change: Change,
    ) -> Result<Option<Execution>, ErrorBody> {
        let lock = self.lock_of(id);
        let _held = guard(&lock);
        let mut connection = executions::connect(self.state()?)?;
        let tx = executions::begin(&mut connection)?;
        let Some(current) = executions::load(&tx, id)?.filter(|current| current.state == from)
        else {
            return Ok(None);
        };
        let moved = executions::transition(&tx, &current, to, change, &iso_now())?;
        sql(tx.commit())?;
        Ok(Some(moved))
    }

    /// Record `to` until the store takes it: a busy store delays the evidence, it never drops it
    /// while the runtime lives.
    fn record(&self, id: &str, from: ExecutionState, to: ExecutionState, change: &Change) {
        while self.locked(id, from, to, change.clone()).is_err() {
            thread::sleep(Duration::from_millis(500));
        }
    }

    fn log_paths(&self, id: &str) -> Vec<PathBuf> {
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
        let outlived =
            || group_of(pgid).is_some_and(|group| matches!(members_alive(group), Ok(true)));
        loop {
            match child.try_wait() {
                Ok(Some(status)) if !outlived() => {
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
                // The leader runs, or a member of its group outlives it: both end at the deadline.
                Ok(_) if past(deadline) => {
                    if self.stop(id, pgid, Leader::Child(&mut child)) {
                        return;
                    }
                }
                Ok(_) => thread::sleep(POLL),
                // The wait status is lost: reconcile observes the identity from now on.
                Err(_) => return,
            }
        }
    }

    /// Stop an adopted execution on its own thread: a `running` one past its deadline, or a
    /// `stopping` one whose stop a runtime before this one began.
    pub(crate) fn stop_adopted(self: &Arc<Self>, execution: &Execution) {
        let (Some(recorded), Some(pgid)) = (execution.identity.clone(), execution.pgid) else {
            return;
        };
        if !guard(&self.inner.watched).insert(execution.id.clone()) {
            return;
        }
        let supervisor = Arc::clone(self);
        let execution = execution.clone();
        thread::spawn(move || {
            let leader = Leader::Adopted(&recorded);
            if execution.state == ExecutionState::Stopping {
                supervisor.resume(&execution, pgid, leader);
            } else {
                supervisor.stop(&execution.id, pgid, leader);
            }
            guard(&supervisor.inner.watched).remove(&execution.id);
        });
    }

    /// The deadline stop of a `running` execution. Returns `false` when it did not begin because
    /// the leader is gone and no member of its group is left: its exit is recorded elsewhere.
    fn stop(&self, id: &str, pgid: i64, mut leader: Leader<'_>) -> bool {
        let group = group_of(pgid);
        // The leader and its group again, right before the first signal: an adopted leader may
        // have ended since reconcile saw it, and the number of an empty group may be reused.
        if !matches!(leader.look(), Seen::Alive)
            && !group.is_some_and(|group| matches!(members_alive(group), Ok(true)))
        {
            return false;
        }
        let term_sent_at = iso_now();
        let stopping = Change {
            observation: Some(json!({"stopped_by": "deadline", "term_sent_at": term_sent_at})),
            facts: json!({"stopped_by": "deadline", "term_sent_at": term_sent_at}),
            ..Change::default()
        };
        let Ok(Some(execution)) = self.locked(
            id,
            ExecutionState::Running,
            ExecutionState::Stopping,
            stopping,
        ) else {
            return true;
        };
        let Some(group) = group else {
            self.unproven(id, "the recorded process group is not a pid");
            return true;
        };
        // The grace is the one the row pins, read before the signal.
        let grace = execution.stop_grace_seconds;
        if let Err(reason) = signal_group(group, Signal::SIGTERM) {
            self.unproven(id, &reason);
            return true;
        }
        let plan = StopPlan {
            term_sent_at,
            term_at: now_micros(),
            grace_seconds: grace,
        };
        self.conclude(id, group, leader, &plan);
        true
    }

    /// Drive the stop of a `stopping` execution that a runtime before this one began to its
    /// proof, from the SIGTERM time it recorded.
    fn resume(&self, execution: &Execution, pgid: i64, leader: Leader<'_>) {
        let term_sent_at = execution
            .observation
            .as_ref()
            .and_then(|observation| observation.get("term_sent_at"))
            .and_then(Value::as_str);
        let Some(plan) = term_sent_at.and_then(|at| {
            parse_rfc3339(at).map(|term_at| StopPlan {
                term_sent_at: at.to_owned(),
                term_at,
                grace_seconds: execution.stop_grace_seconds,
            })
        }) else {
            self.unproven(&execution.id, "the stop recorded no SIGTERM time");
            return;
        };
        let Some(group) = group_of(pgid) else {
            self.unproven(&execution.id, "the recorded process group is not a pid");
            return;
        };
        self.conclude(&execution.id, group, leader, &plan);
    }

    /// The rest of a stop after the SIGTERM of `plan`: SIGKILL after its grace when a member is
    /// still there, the wait until the leader is gone and the group is empty, then `exited` with
    /// the exit evidence and the lease released, in one transition.
    fn conclude(&self, id: &str, group: Pid, mut leader: Leader<'_>, plan: &StopPlan) {
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
            if leader_gone {
                match members_alive(group) {
                    Ok(false) => break,
                    Ok(true) => {}
                    Err(err) => {
                        self.unproven(id, &format!("the process group cannot be observed: {err}"));
                        return;
                    }
                }
            }
            let now = now_micros();
            if kill_sent_at.is_none() && now >= plan.term_at + plan.grace_seconds * 1_000_000 {
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
            "term_sent_at": plan.term_sent_at,
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
