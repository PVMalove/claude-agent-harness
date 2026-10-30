//! The Windows process layer of the supervisor, behind the platform seam of `launch` and `stop`
//! (ADR 0022, sections 2, 3 and 5, and its implementation decisions of #806).
//!
//! A launch creates the named job object of the execution (`job::name`) with an owner-only DACL
//! and no limit, starts the execution suspended (`CREATE_SUSPENDED`), as the root of a new process
//! group (`CREATE_NEW_PROCESS_GROUP`) with a hidden console of its own (`CREATE_NO_WINDOW`), puts
//! it into the job, gives it its own query handle to the job and resumes it. A failure between the
//! spawn and the resume ends the suspended process: nothing of the execution ran, and the job
//! ends with the last handle. The identity is the pid and the creation time; the process group id
//! is the pid of the leader.
//!
//! After a restart the job is opened by the name the intent recorded. An intent is adopted by the
//! earliest live member of its job, whose threads are resumed, because the launch may have died
//! before its resume; a job without a name or a live member makes it `lost`. A recorded leader is
//! observed by its pid and creation time, and its job by the number of its members
//! (`job::outcome`); a live leader in its job is held by this runtime until its row ends, so its
//! exit code stays readable after it ends.
//!
//! A stop at the deadline sends `CTRL_BREAK` to the recorded process group through the
//! ctrl-break helper (`harness-runtime ctrl-break`, see `ctrl_break`): no runtime shares the
//! console of an execution, so a short process without a console attaches to the hidden console
//! of a live member of the job and generates the event there. After `stop_grace_seconds`
//! `TerminateJobObject` ends every member that is left with exit code 137. The stop is proven when
//! the job is empty and the leader is gone.

use std::io::{self, Read};
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use platform_ffi::{
    CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW, CREATE_SUSPENDED, DETACHED_PROCESS, Job, JobAccess,
    Process, create_private_file, current_user_sid, send_ctrl_break,
};
use serde_json::{Map, Value, json};
use state::executions::Execution;
use state::identity::{self, Identity, Observer, Verdict};
use state::time::iso_now;

use crate::Supervisor;
use crate::job::{self, JobSeen, LeaderSeen, Outcome};
use crate::launch::{LABEL_DISPATCH, LABEL_OPERATION, Observed, Spec, guard};
use crate::stop::{Seen, logs};

/// How the receipt names the accounting of an execution.
pub(crate) const ACCOUNTING: &str = "job-object";
/// Why reconcile records an intent `lost` that no process answers.
pub(crate) const NOT_FOUND: &str = "no job object with a live member; never relaunched";
/// Why a stop recorded by an earlier runtime cannot go on.
pub(crate) const NO_TERM_TIME: &str = "the stop recorded no CTRL_BREAK time";
/// Why a stop cannot prove its end after the kill.
pub(crate) const OUTLIVED_KILL: &str = "the job object outlived TerminateJobObject";

/// The leader of an execution this runtime holds: a handle to its process, which keeps the pid
/// and the exit code, and a handle to its job, which keeps the name of the job.
#[derive(Debug)]
pub(crate) struct Launched {
    process: Process,
    job: Job,
}

/// The pid of a leader this runtime holds.
pub(crate) fn pid(launched: &Launched) -> i64 {
    i64::from(launched.process.pid())
}

/// Create the log directory of an execution under the state directory of the runtime.
pub(crate) fn log_directory(dir: &Path) -> io::Result<()> {
    std::fs::create_dir_all(dir)
}

/// What the intent of a launch records beyond the spec, the labels and the logs: the name of the
/// job object of the execution.
pub(crate) fn launch_facts(operation_id: &str) -> Map<String, Value> {
    let mut facts = Map::new();
    facts.insert("job".to_owned(), json!(job::name(operation_id)));
    facts
}

/// The name of the job object the intent of `execution` recorded.
fn job_name(execution: &Execution) -> String {
    execution
        .launch
        .get("job")
        .and_then(Value::as_str)
        .map_or_else(|| job::name(&execution.id), str::to_owned)
}

/// Start the execution of `spec` in the job object of `execution`, in the order of the module
/// documentation.
///
/// # Errors
/// The error of the OS: a job name that exists already, a log file, the spawn, or a step between
/// the spawn and the resume. The suspended process has ended then.
pub(crate) fn spawn(spec: &Spec<'_>, execution: &Execution) -> io::Result<Launched> {
    let (program, arguments) = spec
        .argv
        .split_first()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "the argv is empty"))?;
    let sid = current_user_sid()?;
    let job = Job::create(&job_name(execution), &sid)?;
    let stdout = create_private_file(spec.stdout, &sid)?;
    let stderr = create_private_file(spec.stderr, &sid)?;
    let mut child = Command::new(program)
        .args(arguments)
        .current_dir(spec.cwd)
        .creation_flags(CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW)
        .stdin(Stdio::null())
        .stdout(stdout)
        .stderr(stderr)
        .env(LABEL_OPERATION, spec.operation_id)
        .env(LABEL_DISPATCH, spec.dispatch_id)
        .spawn()?;
    let placed = Process::from_child(&child).and_then(|process| {
        job.assign(&process)?;
        job.share_with(&process)?;
        if process.resume()? == 0 {
            return Err(io::Error::other(
                "the suspended process has no thread to resume",
            ));
        }
        Ok(process)
    });
    match placed {
        // The handle of `std` goes: `process` keeps the pid and the exit code.
        Ok(process) => Ok(Launched { process, job }),
        Err(err) => {
            let _ = child.kill();
            let _ = child.wait();
            Err(err)
        }
    }
}

/// The identity and the process group of the process that holds `pid` now. The creation time
/// is read from a handle, so a process that has ended while another handle keeps it still has
/// one.
pub(crate) fn identity_of(pid: i64, observer: &Observer) -> Option<(Identity, i64)> {
    let process = Process::open(u32::try_from(pid).ok()?).ok()??;
    let created = process.creation_time().ok()?;
    Some((
        Identity {
            host: observer.host.clone(),
            boot_id: observer.boot_id.clone(),
            pid_namespace: observer.pid_namespace.clone(),
            pid,
            start_time: created.to_string(),
        },
        pid,
    ))
}

/// The leader of an intent that no launch of this runtime holds: the earliest live member of the
/// job object the intent named, with its threads resumed. `None` when the job has no name or no
/// live member.
///
/// # Errors
/// The job cannot be opened or listed, or the leader cannot be resumed: nothing is proven.
pub(crate) fn find(execution: &Execution) -> io::Result<Option<(i64, String)>> {
    let Some(job) = Job::open(&job_name(execution), JobAccess::Control)? else {
        return Ok(None);
    };
    let mut earliest: Option<(u64, Process)> = None;
    for pid in job.processes()? {
        // A member that has ended or left since the list is not the leader.
        let Ok(Some(process)) = Process::open(pid) else {
            continue;
        };
        let live =
            matches!(process.exit_code(), Ok(None)) && matches!(job.contains(&process), Ok(true));
        let Ok(created) = process.creation_time() else {
            continue;
        };
        if live && earliest.as_ref().is_none_or(|(first, _)| created < *first) {
            earliest = Some((created, process));
        }
    }
    let Some((created, leader)) = earliest else {
        return Ok(None);
    };
    leader.resume()?;
    Ok(Some((i64::from(leader.pid()), created.to_string())))
}

/// Observe the recorded leader of `execution` and its job object, by the table of
/// `job::outcome`. A leader that ended while this runtime held it leaves its exit code as the
/// exit evidence.
pub(crate) fn observe(
    supervisor: &Supervisor,
    execution: &Execution,
    recorded: &Identity,
) -> Observed {
    let opened = Job::open(&job_name(execution), JobAccess::Control);
    let job_seen = match &opened {
        Ok(None) => JobSeen::NameAbsent,
        Ok(Some(job)) => job.active_processes().map_or_else(
            |err| JobSeen::Unknown(format!("the job object cannot be observed: {err}")),
            JobSeen::Members,
        ),
        Err(err) => JobSeen::Unknown(format!("the job object cannot be opened: {err}")),
    };
    let leader_seen = look_at(supervisor, execution, recorded, opened.ok().flatten());
    let outcome = job::outcome(&leader_seen, &job_seen);
    let mut facts = json!({"verdict": leader_seen.as_str(), "job": job_seen.as_str()});
    let mut exit = None;
    match &outcome {
        Outcome::Exited(code) => {
            facts["source"] = json!("process-handle");
            exit = Some(json!({
                "exit_code": code,
                "signal": null,
                "source": "process-handle",
                "stopped_by": null,
                "ended_at": iso_now(),
                "logs": logs(&supervisor.log_paths(&execution.id)),
            }));
            guard(&supervisor.inner.held).remove(&execution.id);
        }
        Outcome::Lost => {
            guard(&supervisor.inner.held).remove(&execution.id);
        }
        Outcome::Unknown(reason) => facts["reason"] = json!(reason),
        Outcome::Running => {}
    }
    Observed {
        to: outcome.state(),
        facts,
        exit,
    }
}

/// What this runtime sees of the recorded leader. A live leader in `job` is held from now on.
fn look_at(
    supervisor: &Supervisor,
    execution: &Execution,
    recorded: &Identity,
    job: Option<Job>,
) -> LeaderSeen {
    if recorded.host != identity::observer().host {
        return LeaderSeen::Unknown(Verdict::OnAnotherHost.as_str().to_owned());
    }
    if let Some(held) = guard(&supervisor.inner.held).get(&execution.id) {
        return match held.process.exit_code() {
            Ok(Some(code)) => LeaderSeen::Exited { code, held: true },
            Ok(None) => held.job.contains(&held.process).map_or_else(
                |err| LeaderSeen::Unknown(format!("the job membership cannot be read: {err}")),
                |in_job| LeaderSeen::Alive { in_job },
            ),
            Err(err) => LeaderSeen::Unknown(format!("the held leader cannot be observed: {err}")),
        };
    }
    let Ok(pid) = u32::try_from(recorded.pid) else {
        return LeaderSeen::Absent;
    };
    let process = match Process::open(pid) {
        Ok(Some(process)) => process,
        Ok(None) => return LeaderSeen::Absent,
        Err(err) => return LeaderSeen::Unknown(format!("the leader cannot be opened: {err}")),
    };
    match process.creation_time() {
        Ok(created) if created.to_string() == recorded.start_time => {}
        Ok(_) => return LeaderSeen::PidReused,
        Err(err) => return LeaderSeen::Unknown(format!("the creation time cannot be read: {err}")),
    }
    match process.exit_code() {
        Ok(Some(code)) => LeaderSeen::Exited { code, held: false },
        Ok(None) => {
            let Some(job) = job else {
                return LeaderSeen::Alive { in_job: false };
            };
            match job.contains(&process) {
                Ok(true) => {
                    guard(&supervisor.inner.held)
                        .insert(execution.id.clone(), Launched { process, job });
                    LeaderSeen::Alive { in_job: true }
                }
                Ok(false) => LeaderSeen::Alive { in_job: false },
                Err(err) => {
                    LeaderSeen::Unknown(format!("the job membership cannot be read: {err}"))
                }
            }
        }
        Err(err) => LeaderSeen::Unknown(format!("the exit code cannot be read: {err}")),
    }
}

/// Close the handles this runtime held to the leader of the execution `id`: a stop recorded its
/// row `exited`.
pub(crate) fn release_leader(supervisor: &Supervisor, id: &str) {
    guard(&supervisor.inner.held).remove(id);
}

/// Close the handles of a leader that a watcher gives up without the proof of its exit, as the
/// end of the watcher always did: the job object keeps the execution, and reconcile observes it.
pub(crate) fn reap(launched: Launched) {
    drop(launched);
}

/// The exit a stop or a watcher proves: the exit code of a leader this runtime held, as an
/// unsigned `DWORD`. Windows has no signal.
pub(crate) struct Exit {
    code: u32,
    source: &'static str,
}

impl Exit {
    pub(crate) fn code(&self) -> Value {
        json!(self.code)
    }

    pub(crate) const fn signal(&self) -> Value {
        Value::Null
    }

    pub(crate) const fn source(&self) -> &'static str {
        self.source
    }
}

/// The leader of a stop or a watcher.
pub(crate) enum Leader<'a> {
    /// A child of this runtime: its handle.
    Child(&'a mut Launched),
    /// A leader another runtime started that this runtime held since it ran: a copy of the
    /// held handle.
    Held(Process),
    /// A leader this runtime does not hold: its recorded pid and creation time.
    Adopted(Identity),
}

/// What a held handle shows of its leader.
fn exit_of(process: &Process, source: &'static str) -> Seen {
    match process.exit_code() {
        Ok(Some(code)) => Seen::Gone(Some(Exit { code, source })),
        Ok(None) => Seen::Alive,
        Err(err) => Seen::Unproven(format!("the exit code cannot be read: {err}")),
    }
}

impl Leader<'_> {
    /// The leader of an execution another runtime started: a copy of the handle this runtime
    /// holds, or else the recorded identity. The held handle stays until the row ends
    /// (`release_leader`), so a stop that does not begin, or that ends `unknown`, leaves the exit
    /// code to the next observation.
    pub(crate) fn adopted(supervisor: &Supervisor, execution: &Execution) -> Option<Self> {
        let held = guard(&supervisor.inner.held)
            .get(&execution.id)
            .and_then(|held| held.process.try_clone().ok());
        held.map(Leader::Held)
            .or_else(|| execution.identity.clone().map(Leader::Adopted))
    }

    pub(crate) fn look(&mut self) -> Seen {
        match self {
            Self::Child(launched) => exit_of(&launched.process, "wait-status"),
            Self::Held(process) => exit_of(process, "process-handle"),
            Self::Adopted(recorded) => {
                let Ok(pid) = u32::try_from(recorded.pid) else {
                    return Seen::Gone(None);
                };
                match Process::open(pid) {
                    Ok(None) => Seen::Gone(None),
                    Ok(Some(process)) => match (process.creation_time(), process.exit_code()) {
                        (Ok(created), Ok(None)) if created.to_string() == recorded.start_time => {
                            Seen::Alive
                        }
                        // Another process at the pid, or an end no handle of this runtime saw.
                        (Ok(_), Ok(_)) => Seen::Gone(None),
                        (Err(err), _) | (_, Err(err)) => {
                            Seen::Unproven(format!("the leader cannot be observed: {err}"))
                        }
                    },
                    Err(err) => Seen::Unproven(format!("the leader cannot be opened: {err}")),
                }
            }
        }
    }
}

/// The job object of an execution, the only target of a stop, and the recorded process group of
/// its leader, the only target of `CTRL_BREAK`. The process group id the receipt recorded must
/// be the pid of the recorded leader.
pub(crate) struct Group {
    name: String,
    job: Option<Job>,
    pgid: u32,
    leader: Identity,
    helper: Option<PathBuf>,
}

impl Group {
    /// The group the receipt of `execution` recorded, with its job opened by name; a job without
    /// a name has no member left.
    ///
    /// # Errors
    /// Why the group is never stopped.
    pub(crate) fn of(supervisor: &Supervisor, execution: &Execution) -> Result<Self, String> {
        let pgid = execution.pgid.unwrap_or(0);
        let refused = || {
            format!(
                "the recorded process group {pgid} is not the pid of the recorded leader: it is never signalled"
            )
        };
        let leader = execution
            .identity
            .clone()
            .filter(|identity| identity.pid == pgid)
            .ok_or_else(refused)?;
        let pgid = u32::try_from(pgid)
            .ok()
            .filter(|pgid| *pgid > 0)
            .ok_or_else(refused)?;
        let name = job_name(execution);
        let job = Job::open(&name, JobAccess::Control)
            .map_err(|err| format!("the job object cannot be opened: {err}"))?;
        Ok(Self {
            name,
            job,
            pgid,
            leader,
            helper: supervisor.ctrl_break_helper(),
        })
    }

    /// Whether a process of the job has not ended.
    ///
    /// # Errors
    /// Why the job cannot be observed.
    pub(crate) fn members_alive(&self) -> Result<bool, String> {
        self.job.as_ref().map_or(Ok(false), |job| {
            job.active_processes()
                .map(|members| members > 0)
                .map_err(|err| format!("the job object cannot be observed: {err}"))
        })
    }

    /// The first step of a stop: `CTRL_BREAK` to the recorded group, through the ctrl-break
    /// helper, which runs without a console and attaches to the one of a live member of the job.
    /// A `CTRL_BREAK` that was not delivered is a fact of the stop, not its failure: the kill after
    /// the grace and the empty job prove the stop.
    ///
    /// # Errors
    /// Never: the outcome is the fact `ctrl_break` (`delivered`, `no-member` or `failed`, with
    /// `ctrl_break_error`).
    pub(crate) fn term(&self) -> Result<Map<String, Value>, String> {
        let mut facts = Map::new();
        let outcome = match self.send_ctrl_break() {
            CtrlBreak::Delivered => "delivered",
            CtrlBreak::NoMember => "no-member",
            CtrlBreak::Failed(reason) => {
                facts.insert("ctrl_break_error".to_owned(), json!(reason));
                "failed"
            }
        };
        facts.insert("ctrl_break".to_owned(), json!(outcome));
        Ok(facts)
    }

    /// Run the ctrl-break helper and wait for it up to `HELPER_WAIT`; one that does not end by
    /// then is killed.
    fn send_ctrl_break(&self) -> CtrlBreak {
        if self.job.is_none() {
            return CtrlBreak::NoMember;
        }
        let Some(helper) = &self.helper else {
            return CtrlBreak::Failed(
                "no ctrl-break helper: the runtime path is unknown".to_owned(),
            );
        };
        let spawned = Command::new(helper)
            .args(["ctrl-break", "--job", &self.name, "--group"])
            .arg(self.pgid.to_string())
            .arg("--pid")
            .arg(self.leader.pid.to_string())
            .arg("--created")
            .arg(&self.leader.start_time)
            .creation_flags(DETACHED_PROCESS)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .spawn();
        let mut child = match spawned {
            Ok(child) => child,
            Err(err) => {
                return CtrlBreak::Failed(format!("the ctrl-break helper cannot start: {err}"));
            }
        };
        let until = Instant::now() + HELPER_WAIT;
        loop {
            match child.try_wait() {
                Ok(Some(status)) => {
                    let mut stderr = String::new();
                    if let Some(mut pipe) = child.stderr.take() {
                        let _ = pipe.read_to_string(&mut stderr);
                    }
                    return match status.code() {
                        Some(0) => CtrlBreak::Delivered,
                        Some(code) if code == i32::from(CTRL_BREAK_NO_MEMBER) => {
                            CtrlBreak::NoMember
                        }
                        code => CtrlBreak::Failed(format!(
                            "the ctrl-break helper ended with {code:?}: {}",
                            stderr.trim()
                        )),
                    };
                }
                Ok(None) if Instant::now() < until => thread::sleep(Duration::from_millis(20)),
                Ok(None) => {
                    let _ = child.kill();
                    let _ = child.wait();
                    return CtrlBreak::Failed(format!(
                        "the ctrl-break helper did not end within {} seconds",
                        HELPER_WAIT.as_secs()
                    ));
                }
                Err(err) => {
                    return CtrlBreak::Failed(format!(
                        "the ctrl-break helper cannot be waited for: {err}"
                    ));
                }
            }
        }
    }

    /// The last step of a stop: `TerminateJobObject` with exit code 137 for every member; a job
    /// without a name has none.
    ///
    /// # Errors
    /// Why the job was not terminated.
    pub(crate) fn kill(&self) -> Result<(), String> {
        self.job.as_ref().map_or(Ok(()), |job| {
            job.terminate(KILL_EXIT_CODE)
                .map_err(|err| format!("TerminateJobObject failed: {err}"))
        })
    }
}

/// The exit code `TerminateJobObject` gives every member of a stopped job: 128 + 9, as SIGKILL on
/// Linux.
const KILL_EXIT_CODE: u32 = 137;
/// How long a stop waits for the ctrl-break helper.
const HELPER_WAIT: Duration = Duration::from_secs(10);
/// The exit code of the ctrl-break helper when the job has no live member or no name.
pub const CTRL_BREAK_NO_MEMBER: u8 = 3;

/// What the ctrl-break helper did.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CtrlBreak {
    /// `CTRL_BREAK` reached the group on the console of a live member of the job.
    Delivered,
    /// The job has no name or no live member: nobody to deliver to.
    NoMember,
    /// The event was not delivered; the reason.
    Failed(String),
}

/// The ctrl-break helper (`harness-runtime ctrl-break`): send `CTRL_BREAK` to the process group
/// `group` on the console of a live member of the job object `job`. The member is the leader
/// `pid` when its creation time is `created` and it is a live member of the job, else the first
/// live member of the job: a process outside the job, such as one that reuses the pid of the
/// leader, is never touched. Only a process without a console of its own calls this, because it
/// attaches to the console of the member.
pub fn ctrl_break(job: &str, group: u32, pid: u32, created: &str) -> CtrlBreak {
    if let Err(err) = platform_ffi::ignore_ctrl_events() {
        return CtrlBreak::Failed(format!("the helper cannot ignore console events: {err}"));
    }
    let job = match Job::open(job, JobAccess::Query) {
        Ok(Some(job)) => job,
        Ok(None) => return CtrlBreak::NoMember,
        Err(err) => return CtrlBreak::Failed(format!("the job object cannot be opened: {err}")),
    };
    let live = |process: &Process| {
        matches!(process.exit_code(), Ok(None)) && matches!(job.contains(process), Ok(true))
    };
    let leader = Process::open(pid).ok().flatten().filter(|leader| {
        leader
            .creation_time()
            .is_ok_and(|time| time.to_string() == created)
            && live(leader)
    });
    let member = leader.or_else(|| {
        job.processes()
            .ok()?
            .into_iter()
            .filter_map(|pid| Process::open(pid).ok().flatten())
            .find(|process| live(process))
    });
    let Some(member) = member else {
        return CtrlBreak::NoMember;
    };
    match send_ctrl_break(&member, group) {
        Ok(()) => CtrlBreak::Delivered,
        Err(err) => CtrlBreak::Failed(format!("CTRL_BREAK was not delivered: {err}")),
    }
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::sync::Arc;

    use contracts::ExecutionState;
    use state::executions::KIND_PROCESS;

    use super::*;

    /// Wait up to 30 seconds until `done`.
    fn wait_until(what: &str, mut done: impl FnMut() -> bool) {
        let until = Instant::now() + Duration::from_secs(30);
        while !done() {
            assert!(Instant::now() < until, "{what}");
            thread::sleep(Duration::from_millis(50));
        }
    }

    #[test]
    fn a_held_leader_that_ends_before_its_deadline_stop_begins_is_exited_with_its_exit_code() {
        let id = format!("held-leader-{}", std::process::id());
        let dir = std::env::temp_dir().join(format!("hr-win-unit-{id}"));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let marker = dir.join("exit");
        let (stdout, stderr) = (dir.join("stdout.log"), dir.join("stderr.log"));
        let argv = [
            "python".to_owned(),
            "-c".to_owned(),
            format!(
                "import os, sys, time\nwhile not os.path.exists(r'{}'): time.sleep(0.05)\nsys.exit(5)",
                marker.display()
            ),
        ];
        let mut execution = Execution {
            id: id.clone(),
            dispatch_id: "dispatch-held-leader".to_owned(),
            attempt: 1,
            state: ExecutionState::Running,
            revision: 1,
            kind: KIND_PROCESS.to_owned(),
            engine: None,
            container_id: None,
            launch: Value::Object(launch_facts(&id)),
            execution_deadline: "2000-01-01T00:00:00Z".to_owned(),
            stop_grace_seconds: 5,
            poll_interval_seconds: 1,
            identity: None,
            pgid: None,
            receipt: None,
            exit: None,
            observation: None,
            lease_owner: Value::Null,
            lease_released_at: None,
            created_at: iso_now(),
            updated_at: iso_now(),
        };
        let spec = Spec {
            argv: &argv,
            cwd: &dir,
            operation_id: &id,
            dispatch_id: &execution.dispatch_id,
            stdout: &stdout,
            stderr: &stderr,
        };
        let launched = spawn(&spec, &execution).unwrap();
        let (recorded, pgid) = identity_of(pid(&launched), &identity::observer()).unwrap();
        execution.identity = Some(recorded.clone());
        execution.pgid = Some(pgid);
        // The runtime that launched it is gone: no handle of this test keeps the leader.
        drop(launched);
        let supervisor = Arc::new(Supervisor::new(None));
        // Reconcile of a new runtime sees the live leader in its job and holds it.
        assert_eq!(
            observe(&supervisor, &execution, &recorded).to,
            ExecutionState::Running
        );
        // The leader ends after that observation and before the stop of its deadline begins.
        fs::write(&marker, "").unwrap();
        wait_until("the held leader did not end", || {
            guard(&supervisor.inner.held)
                .get(&id)
                .is_some_and(|held| matches!(held.process.exit_code(), Ok(Some(_))))
        });

        supervisor.stop_adopted(&execution);
        wait_until("the stop did not end", || !supervisor.is_watched(&id));

        let observed = observe(&supervisor, &execution, &recorded);
        assert_eq!(observed.to, ExecutionState::Exited, "{}", observed.facts);
        let exit = observed.exit.unwrap();
        assert_eq!(exit["exit_code"], 5, "{exit}");
        assert_eq!(exit["source"], "process-handle", "{exit}");
        assert!(!guard(&supervisor.inner.held).contains_key(&id));
        let _ = fs::remove_dir_all(&dir);
    }
}
