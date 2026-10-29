//! The Linux process layer of the supervisor, behind the platform seam of `launch` and `stop`
//! (ADR 0022, sections 2 and 3).
//!
//! An execution is the leader of its own process group, spawned by `process::spawn`; its
//! identity is the one `/proc/<pid>/stat` shows, and the group id is the one the identity
//! records. An intent without a receipt is adopted by the label scan of `/proc/*/environ`. A stop
//! sends SIGTERM to the recorded group and SIGKILL after the grace; `killpg` never reaches a group
//! id at or below 1.

use std::fmt;
use std::io;
use std::os::unix::fs::DirBuilderExt;
use std::os::unix::process::ExitStatusExt;
use std::path::Path;
use std::process::{Child, ExitStatus};

use contracts::ExecutionState;
use nix::errno::Errno;
use nix::sys::signal::{Signal, killpg};
use nix::unistd::Pid;
use serde_json::{Map, Value, json};
use state::executions::{self, Execution};
use state::identity::{self, Identity, Observer, Probe, Verdict};

use crate::Supervisor;
use crate::launch::Spec;
use crate::process;
use crate::stop::Seen;

/// How the receipt names the accounting of an execution.
pub(crate) const ACCOUNTING: &str = "process-group";
/// Why reconcile records an intent `lost` that no process answers.
pub(crate) const NOT_FOUND: &str = "no labelled process; never relaunched";
/// Why a stop recorded by an earlier runtime cannot go on.
pub(crate) const NO_TERM_TIME: &str = "the stop recorded no SIGTERM time";
/// Why a stop cannot prove its end after the kill.
pub(crate) const OUTLIVED_KILL: &str = "the process group outlived SIGKILL";

/// A process this runtime spawned and waits for.
pub(crate) type Launched = Child;

/// The pid of a process this runtime spawned.
pub(crate) fn pid(launched: &Launched) -> i64 {
    i64::from(launched.id())
}

/// Create the log directory of an execution, private to the runtime's user.
pub(crate) fn log_directory(dir: &Path) -> io::Result<()> {
    std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(dir)
}

/// What the intent of a launch records beyond the spec, the labels and the logs: nothing here.
pub(crate) fn launch_facts(_operation_id: &str) -> Map<String, Value> {
    Map::new()
}

/// Start the execution of `spec` as the leader of a new process group.
pub(crate) fn spawn(spec: &Spec<'_>, _execution: &Execution) -> io::Result<Launched> {
    process::spawn(spec)
}

/// The identity and the process group of the process that holds `pid` now.
pub(crate) fn identity_of(pid: i64, observer: &Observer) -> Option<(Identity, i64)> {
    process::identity_of(pid, observer)
}

/// The labelled group leader of an intent that no launch of this runtime holds: its pid and
/// start time.
pub(crate) fn find(execution: &Execution) -> io::Result<Option<(i64, String)>> {
    process::scan(&execution.id, writer_start(execution))
}

/// The start time, in clock ticks of this boot, of the runtime that wrote the intent of
/// `execution`: no process of its launch started before it. `None` when the intent names no
/// identity of this boot and PID namespace.
fn writer_start(execution: &Execution) -> Option<u64> {
    let writer = execution.lease_owner.get("identity")?;
    let observer = identity::observer();
    let here = |key: &str, value: Option<&String>| {
        value.is_some_and(|value| writer.get(key).and_then(Value::as_str) == Some(value.as_str()))
    };
    if !(here("boot_id", observer.boot_id.as_ref())
        && here("pid_namespace", observer.pid_namespace.as_ref()))
    {
        return None;
    }
    writer.get("start_time")?.as_str()?.parse().ok()
}

/// What reconcile sees of an execution with a recorded identity: the state it means, the facts
/// of the observation, and the exit evidence when the observation proves the exit.
pub(crate) struct Observed {
    pub(crate) to: ExecutionState,
    pub(crate) facts: Value,
    pub(crate) exit: Option<Value>,
}

/// Observe the recorded identity of `execution`. A leader gone by itself ends its execution only
/// with its group, as for a child of this runtime: a member that outlives it keeps the row
/// `running` and the lease held, and a group that cannot be observed is `unknown`.
pub(crate) fn observe(
    _supervisor: &Supervisor,
    execution: &Execution,
    recorded: &Identity,
) -> Observed {
    let observer = identity::observer();
    let verdict = identity::verdict(Some(recorded), &observer, &identity::probe(recorded.pid));
    let to = match executions::observed(verdict, false) {
        ExecutionState::Lost => match execution.pgid.map(outlived) {
            Some(Ok(true)) => ExecutionState::Running,
            Some(Err(_)) => ExecutionState::Unknown,
            Some(Ok(false)) | None => ExecutionState::Lost,
        },
        other => other,
    };
    Observed {
        to,
        facts: json!({"verdict": verdict.as_str()}),
        exit: None,
    }
}

/// The exit a stop or a watcher proves: the wait status of a child of this runtime.
pub(crate) struct Exit(ExitStatus);

impl Exit {
    pub(crate) fn code(&self) -> Value {
        json!(self.0.code())
    }

    pub(crate) fn signal(&self) -> Value {
        json!(self.0.signal())
    }

    pub(crate) const fn source(&self) -> &'static str {
        "wait-status"
    }
}

/// How the stop proves that the group leader is gone.
pub(crate) enum Leader<'a> {
    /// A child of this runtime: its wait status.
    Child(&'a mut Child),
    /// A process another runtime started: the verdict on its recorded identity.
    Adopted(Identity),
}

impl Leader<'_> {
    /// The leader of an execution another runtime started, by its recorded identity.
    pub(crate) fn adopted(_supervisor: &Supervisor, execution: &Execution) -> Option<Self> {
        execution.identity.clone().map(Leader::Adopted)
    }

    pub(crate) fn look(&mut self) -> Seen {
        match self {
            Self::Child(child) => match child.try_wait() {
                Ok(Some(status)) => Seen::Gone(Some(Exit(status))),
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
    /// The group the receipt of `execution` recorded.
    ///
    /// # Errors
    /// Why the group is never signalled.
    pub(crate) fn of(_supervisor: &Supervisor, execution: &Execution) -> Result<Self, String> {
        Self::of_pgid(execution.pgid.unwrap_or(0)).map_err(|refused| refused.to_string())
    }

    /// The group of a recorded group id.
    ///
    /// # Errors
    /// A group id at or below 1, or one beyond the pid range.
    fn of_pgid(pgid: i64) -> Result<Self, RefusedGroup> {
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
    ///
    /// # Errors
    /// Why the group cannot be observed.
    pub(crate) fn members_alive(&self) -> Result<bool, String> {
        if matches!(
            identity::probe(i64::from(self.0.as_raw())),
            Probe::Running(_)
        ) {
            return Ok(false);
        }
        self.alive()
            .map_err(|err| format!("the process group cannot be observed: {err}"))
    }

    /// Send `signal` to the group; a group that is gone already is no failure.
    fn signal(self, signal: Signal) -> Result<(), String> {
        match killpg(self.0, signal) {
            Ok(()) | Err(Errno::ESRCH) => Ok(()),
            Err(err) => Err(format!("{signal} to the process group failed: {err}")),
        }
    }

    /// The first step of a stop: SIGTERM to the group. The stop records no fact of it beyond its
    /// time.
    ///
    /// # Errors
    /// Why the signal failed: the stop cannot prove its outcome then.
    pub(crate) fn term(&self) -> Result<Map<String, Value>, String> {
        self.signal(Signal::SIGTERM).map(|()| Map::new())
    }

    /// The last step of a stop: SIGKILL to the group.
    ///
    /// # Errors
    /// Why the signal failed.
    pub(crate) fn kill(&self) -> Result<(), String> {
        self.signal(Signal::SIGKILL)
    }
}

/// Whether a member of the recorded group `pgid` outlives its gone leader; `Err` with the reason
/// when the group cannot be observed or is never signalled.
pub(crate) fn outlived(pgid: i64) -> Result<bool, String> {
    let group = Group::of_pgid(pgid).map_err(|refused| refused.to_string())?;
    group.members_alive()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_group_id_at_or_below_1_is_refused_and_never_signalled() {
        for pgid in [i64::MIN, -1, 0, 1, i64::from(i32::MAX) + 1] {
            assert_eq!(Group::of_pgid(pgid), Err(RefusedGroup(pgid)), "{pgid}");
        }

        assert_eq!(Group::of_pgid(2), Ok(Group(Pid::from_raw(2))));
        assert!(outlived(1).unwrap_err().contains("never signalled"));
    }
}
