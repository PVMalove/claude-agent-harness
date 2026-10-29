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

use std::io;
use std::os::windows::process::CommandExt;
use std::path::Path;
use std::process::{Command, Stdio};

use platform_ffi::{
    CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW, CREATE_SUSPENDED, Job, JobAccess, Process,
    create_private_file, current_user_sid,
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
    /// A leader another runtime started that this runtime held since it ran.
    Held(Launched),
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
    /// The leader of an execution another runtime started: the one this runtime holds, which it
    /// gives to the stop, or else the recorded identity.
    pub(crate) fn adopted(supervisor: &Supervisor, execution: &Execution) -> Option<Self> {
        if let Some(held) = guard(&supervisor.inner.held).remove(&execution.id) {
            return Some(Leader::Held(held));
        }
        execution.identity.clone().map(Leader::Adopted)
    }

    pub(crate) fn look(&mut self) -> Seen {
        match self {
            Self::Child(launched) => exit_of(&launched.process, "wait-status"),
            Self::Held(launched) => exit_of(&launched.process, "process-handle"),
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

/// The job object of an execution, the only target of a stop. The process group id the receipt
/// recorded must be the pid of the recorded leader.
pub(crate) struct Group {
    job: Option<Job>,
}

impl Group {
    /// The group the receipt of `execution` recorded, with its job opened by name; a job without
    /// a name has no member left.
    ///
    /// # Errors
    /// Why the group is never stopped.
    pub(crate) fn of(_supervisor: &Supervisor, execution: &Execution) -> Result<Self, String> {
        let pgid = execution.pgid.unwrap_or(0);
        let leader = execution.identity.as_ref().map(|identity| identity.pid);
        if pgid <= 0 || leader != Some(pgid) {
            return Err(format!(
                "the recorded process group {pgid} is not the pid of the recorded leader: it is never signalled"
            ));
        }
        let job = Job::open(&job_name(execution), JobAccess::Control)
            .map_err(|err| format!("the job object cannot be opened: {err}"))?;
        Ok(Self { job })
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

    /// The first step of a stop.
    ///
    /// # Errors
    /// Not served yet: the stop cannot prove its outcome.
    #[allow(clippy::unused_self)]
    pub(crate) fn term(&self) -> Result<Map<String, Value>, String> {
        Err("the deadline stop of a Windows execution is not served yet".to_owned())
    }

    /// The last step of a stop.
    ///
    /// # Errors
    /// Not served yet.
    #[allow(clippy::unused_self)]
    pub(crate) fn kill(&self) -> Result<(), String> {
        Err("the deadline stop of a Windows execution is not served yet".to_owned())
    }
}
