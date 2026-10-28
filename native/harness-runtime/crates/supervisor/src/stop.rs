//! The end of an execution: the exit a watcher reaps, and the stop at `execution_deadline`
//! (ADR 0022, section 3).
//!
//! The stop of a `running` execution (never of an `unknown` one): SIGTERM goes to the recorded
//! process group, then the row becomes `stopping` with the time of that signal and the lease
//! still held, SIGKILL follows after `stop_grace_seconds` when a member is still there, and the
//! stop waits until the leader is gone and the group is empty. Then one transaction records
//! `exited` with the exit evidence (the wait status of a child of this runtime, or
//! `stop-observed` for an adopted process, the signal times and the size and SHA-256 of both
//! logs) and releases the lease. An observation that fails during the stop moves the row to
//! `unknown` and keeps the lease. The group id comes only from the recorded identity, never from
//! a bare pid, a group id at or below 1 is never signalled, and the leader is looked at again
//! right before each signal. A runtime that dies between the SIGTERM and its record leaves the
//! row `running` past its deadline, and the next runtime stops it again. A stop that a runtime
//! before this one recorded is driven to its proof by reconcile: SIGTERM again while the grace
//! runs, SIGKILL once it has passed since the recorded SIGTERM, then the same evidence.
//!
//! A leader that exits by itself ends its execution only with its group: a member that outlives
//! it keeps the row `running` and the lease held until it ends, or until the deadline stops it.
//! A store that refuses an outcome for good leaves the row `unknown` with the lease held and
//! names the cause on the stderr of the runtime.
//!
//! Debug-build hook (`HARNESS_RUNTIME_TEST_PAUSE_AT`, once per process):
//! `execution.stop:signalled`, the SIGTERM of a deadline stop is sent and its record is not
//! written yet.

use std::fmt;
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
use state::hooks;
use state::identity::{self, Identity, Probe, Verdict};
use state::time::{iso_from_micros, iso_now, now_micros, parse_rfc3339};

use crate::Supervisor;
use crate::launch::{guard, sql};

/// How often a watcher or a stop looks at the process.
const POLL: Duration = Duration::from_millis(50);
/// How long a stop waits after SIGKILL before it calls the outcome unknown.
const KILL_PROOF_SECONDS: i64 = 30;
/// How often the record of an outcome is tried while the store answers a repeatable refusal (a
/// busy store, a revision conflict), and the wait between two tries.
const RECORD_ATTEMPTS: u32 = 120;
const RECORD_RETRY: Duration = Duration::from_millis(500);

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

/// The recorded process group of an execution, the only target of `killpg`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct Group(Pid);

/// A recorded group id the supervisor never signals: `killpg` of 0 reaches the group of the
/// runtime itself, and of 1 or below every process the runtime may signal.
#[derive(Debug, PartialEq, Eq)]
pub(crate) struct RefusedGroup(i64);

impl fmt::Display for RefusedGroup {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "the recorded process group {} is no group of an execution: it is never signalled",
            self.0
        )
    }
}

impl Group {
    /// The group of a recorded group id.
    ///
    /// # Errors
    /// A group id at or below 1, or one beyond the pid range.
    pub(crate) fn of(pgid: i64) -> Result<Self, RefusedGroup> {
        i32::try_from(pgid)
            .ok()
            .filter(|raw| *raw > 1)
            .map(|raw| Self(Pid::from_raw(raw)))
            .ok_or(RefusedGroup(pgid))
    }

    /// Whether a process of the group is still there; `Err` when the kernel cannot tell.
    fn alive(self) -> Result<bool, Errno> {
        match killpg(self.0, None) {
            Ok(()) | Err(Errno::EPERM) => Ok(true),
            Err(Errno::ESRCH) => Ok(false),
            Err(err) => Err(err),
        }
    }

    /// Whether a member of the group outlives its gone leader. The kernel gives the number of a
    /// group to another process only once the group is empty, and that process may lead a group
    /// of the same number: a live process at the leader's pid proves the execution's group gone.
    fn members_alive(self) -> Result<bool, Errno> {
        if matches!(
            identity::probe(i64::from(self.0.as_raw())),
            Probe::Running(_)
        ) {
            return Ok(false);
        }
        self.alive()
    }

    /// Send `signal` to the group; a group that is gone already is no failure.
    fn signal(self, signal: Signal) -> Result<(), String> {
        match killpg(self.0, signal) {
            Ok(()) | Err(Errno::ESRCH) => Ok(()),
            Err(err) => Err(format!("{signal} to the process group failed: {err}")),
        }
    }
}

/// Whether a member of the recorded group `pgid` outlives its gone leader; `Err` with the reason
/// when the group cannot be observed or is never signalled.
pub(crate) fn outlived(pgid: i64) -> Result<bool, String> {
    let group = Group::of(pgid).map_err(|refused| refused.to_string())?;
    group
        .members_alive()
        .map_err(|err| format!("the process group cannot be observed: {err}"))
}

/// Whether the leader lives, or a member of its group outlives it: looked at right before a
/// signal, because an adopted leader may have ended since reconcile saw it and the number of an
/// empty group may be reused.
fn still_there(leader: &mut Leader<'_>, group: Group) -> bool {
    matches!(leader.look(), Seen::Alive) || matches!(group.members_alive(), Ok(true))
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

    /// Record `to`. A repeatable refusal of the store delays the evidence for a bounded time; a
    /// refusal for good leaves the row `unknown` from `from` with the lease held, and the stderr
    /// of the runtime names the cause. Reconcile observes the execution again afterwards.
    fn record(&self, id: &str, from: ExecutionState, to: ExecutionState, change: &Change) {
        let mut attempt = 1;
        let refused = loop {
            match self.locked(id, from, to, change.clone()) {
                Ok(_) => return,
                Err(err) if err.retryable && attempt < RECORD_ATTEMPTS => {
                    attempt += 1;
                    thread::sleep(RECORD_RETRY);
                }
                Err(err) => break err,
            }
        };
        eprintln!(
            "harness-runtime: the execution {id} was not recorded {}: [{}] {}",
            to.as_str(),
            refused.code.as_str(),
            refused.message
        );
        if to == ExecutionState::Unknown {
            return;
        }
        let unrecorded = Change {
            facts: json!({
                "unrecorded": to.as_str(),
                "store_error": {"code": refused.code.as_str(), "message": refused.message},
            }),
            ..Change::default()
        };
        if let Err(again) = self.locked(id, from, ExecutionState::Unknown, unrecorded) {
            eprintln!(
                "harness-runtime: the execution {id} was not recorded unknown either: [{}] {}",
                again.code.as_str(),
                again.message
            );
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
        let outlived = || matches!(outlived(pgid), Ok(true));
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
        let group = match Group::of(pgid) {
            Ok(group) => group,
            Err(refused) => {
                self.unproven(id, ExecutionState::Running, &refused.to_string());
                return true;
            }
        };
        if !still_there(&mut leader, group) {
            return false;
        }
        // The SIGTERM goes out before the row records it: the row never claims a signal that was
        // not sent, and a runtime that dies between the two leaves the row `running` past its
        // deadline, which the next runtime stops again.
        let term_at = now_micros();
        if let Err(reason) = group.signal(Signal::SIGTERM) {
            self.unproven(id, ExecutionState::Running, &reason);
            return true;
        }
        // A debug-build pause only: the step has no failure to inject.
        let _ = hooks::reach_once("execution.stop:signalled");
        let term_sent_at = iso_from_micros(term_at);
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
        let plan = StopPlan {
            term_sent_at,
            term_at,
            grace_seconds: execution.stop_grace_seconds,
        };
        self.conclude(id, group, leader, &plan);
        true
    }

    /// Drive the stop of a `stopping` execution that a runtime before this one began to its
    /// proof, from the SIGTERM time it recorded.
    fn resume(&self, execution: &Execution, pgid: i64, mut leader: Leader<'_>) {
        let id = &execution.id;
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
            self.unproven(
                id,
                ExecutionState::Stopping,
                "the stop recorded no SIGTERM time",
            );
            return;
        };
        let group = match Group::of(pgid) {
            Ok(group) => group,
            Err(refused) => {
                self.unproven(id, ExecutionState::Stopping, &refused.to_string());
                return;
            }
        };
        // SIGTERM again while the grace runs: harmless after the first one, and it reaches a
        // group that a stop recorded before its signal (by an earlier runtime) never got.
        if now_micros() < plan.term_at + plan.grace_seconds * 1_000_000
            && still_there(&mut leader, group)
            && let Err(reason) = group.signal(Signal::SIGTERM)
        {
            self.unproven(id, ExecutionState::Stopping, &reason);
            return;
        }
        self.conclude(id, group, leader, &plan);
    }

    /// The rest of a stop after the SIGTERM of `plan`: SIGKILL after its grace when a member is
    /// still there, the wait until the leader is gone and the group is empty, then `exited` with
    /// the exit evidence and the lease released, in one transition.
    fn conclude(&self, id: &str, group: Group, mut leader: Leader<'_>, plan: &StopPlan) {
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
                        self.unproven(id, ExecutionState::Stopping, &reason);
                        return;
                    }
                }
            }
            if leader_gone {
                match group.members_alive() {
                    Ok(false) => break,
                    Ok(true) => {}
                    Err(err) => {
                        let reason = format!("the process group cannot be observed: {err}");
                        self.unproven(id, ExecutionState::Stopping, &reason);
                        return;
                    }
                }
            }
            let now = now_micros();
            if kill_sent_at.is_none() && now >= plan.term_at + plan.grace_seconds * 1_000_000 {
                if let Err(reason) = group.signal(Signal::SIGKILL) {
                    self.unproven(id, ExecutionState::Stopping, &reason);
                    return;
                }
                kill_sent_at = Some(iso_now());
                kill_at = now;
            }
            if kill_sent_at.is_some() && now >= kill_at + KILL_PROOF_SECONDS * 1_000_000 {
                self.unproven(
                    id,
                    ExecutionState::Stopping,
                    "the process group outlived SIGKILL",
                );
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

    /// A stop that cannot prove its outcome: `unknown` from `from`, and the lease stays held.
    fn unproven(&self, id: &str, from: ExecutionState, reason: &str) {
        let change = Change {
            facts: json!({"stopped_by": "deadline", "unproven": reason}),
            ..Change::default()
        };
        self.record(id, from, ExecutionState::Unknown, &change);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_group_id_at_or_below_1_is_refused_and_never_signalled() {
        for pgid in [i64::MIN, -1, 0, 1, i64::from(i32::MAX) + 1] {
            assert_eq!(Group::of(pgid), Err(RefusedGroup(pgid)), "{pgid}");
        }

        assert_eq!(Group::of(2), Ok(Group(Pid::from_raw(2))));
        assert!(outlived(1).unwrap_err().contains("never signalled"));
    }
}
