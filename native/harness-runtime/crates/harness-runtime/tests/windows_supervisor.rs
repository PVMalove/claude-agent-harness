//! The Windows supervisor end to end, with the ctrl-break helper of this `harness-runtime`
//! executable (ADR 0022, sections 2, 3 and 5): a deadline stop sends `CTRL_BREAK` and an
//! execution that obeys ends without `TerminateJobObject`, one that ignores it ends by
//! `TerminateJobObject` after the grace, and an execution outlives a runtime that dies. A new
//! runtime finds its job by name, records its exit code from the handle it holds even while the
//! console host of its hidden console lives on in the job, finishes a stop
//! the dead runtime began, adopts the leader the runtime of an intent launched without a second
//! launch and never a member created before it, records an intent without a process `lost`, and
//! never signals a process that holds the pid of a forged identity.
//!
//! The runtime that dies is this test binary in a process of its own, running only the ignored
//! test `runtime_process`, killed with `TerminateProcess`; the new runtime is a supervisor in the
//! test process. The executions are Python scripts of the `python` on PATH. Runs in the Windows CI
//! job only.
//!
//! Every wait has a deadline that fails the test with what it waited for and what it saw: the
//! output of the runtime is read by two threads, because the leader inherits the pipes of the
//! runtime and keeps them open after the runtime died, so their end never comes while it runs.
#![cfg(windows)]

#[path = "../../supervisor/tests/common/mod.rs"]
mod common;
#[path = "../../supervisor/tests/windows_job/mod.rs"]
mod windows_job;

use std::fs;
use std::io::{self, BufRead, BufReader, Read};
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, ExitStatus, Stdio};
use std::sync::Arc;
use std::sync::mpsc::{self, Receiver, Sender};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use contracts::{ExecutionState, PROTOCOL_VERSION, Request};
use platform_ffi::{
    CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW, DETACHED_PROCESS, Job, JobAccess, Process,
};
use rusqlite::Connection;
use serde_json::{Value, json};
use state::canonical::{canonical_json, object_sha256, sha256_hex};
use state::executions::{self, Execution};
use supervisor::{Supervisor, job};
use windows_job::Expected;

const HELPER: &str = env!("CARGO_BIN_EXE_harness-runtime");
const RUNTIME_STATE: &str = "HR_TEST_WINDOWS_RUNTIME_STATE";
const DISPATCH: &str = "dispatch-11111111-0000-4000-8000-000000000001";
const FIXTURE: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../state/tests/fixtures/legacy_v3"
);
const OBEYS: &str = "import signal, sys, time\nsignal.signal(signal.SIGBREAK, lambda *_: sys.exit(7))\nwhile True: time.sleep(0.05)";
const IGNORES: &str = "import signal, time\nsignal.signal(signal.SIGBREAK, signal.SIG_IGN)\nwhile True: time.sleep(0.05)";
/// How long a test waits for a line of a runtime.
const LINE_WAIT: Duration = Duration::from_secs(60);
/// How long a test waits for a process it killed to end.
const END_WAIT: Duration = Duration::from_secs(20);

/// Wait up to `within` for `child` to end: its status, or `None` while it still runs.
fn reap(child: &mut Child, within: Duration) -> io::Result<Option<ExitStatus>> {
    let until = Instant::now() + within;
    loop {
        match child.try_wait()? {
            Some(status) => return Ok(Some(status)),
            None if Instant::now() >= until => return Ok(None),
            None => std::thread::sleep(Duration::from_millis(50)),
        }
    }
}

/// A process a test started outside the runtime: killed and waited for at the end of the test,
/// whatever happened.
struct Reaper(Child);

impl Drop for Reaper {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = reap(&mut self.0, END_WAIT);
    }
}

fn copy_tree(from: &Path, to: &Path) {
    fs::create_dir_all(to).unwrap();
    for entry in fs::read_dir(from).unwrap() {
        let entry = entry.unwrap();
        let target = to.join(entry.file_name());
        if entry.file_type().unwrap().is_dir() {
            copy_tree(&entry.path(), &target);
        } else {
            fs::copy(entry.path(), target).unwrap();
        }
    }
}

fn store_of(state: &Path) -> Connection {
    Connection::open(state.join("runtime.db")).unwrap()
}

fn operation_id_of(state: &Path) -> String {
    let (brief, _) = executions::dispatch_records(&store_of(state), DISPATCH)
        .unwrap()
        .unwrap();
    sha256_hex(canonical_json(&json!([DISPATCH, object_sha256(&brief), 1])).as_bytes())
}

fn launch_request(operation_id: String) -> Request {
    let deadline = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis()
        + 30_000;
    Request {
        protocol_version: PROTOCOL_VERSION,
        operation_id,
        operation: "execution.launch".to_owned(),
        request_deadline: u64::try_from(deadline).unwrap(),
        batch_id: None,
        dispatch_id: None,
        candidate_sha: None,
        plan_digest: None,
        expected_revision: None,
        fencing_token: None,
        payload: Some(json!({"dispatch_id": DISPATCH})),
    }
}

fn supervisor_of(state: PathBuf) -> Arc<Supervisor> {
    Arc::new(Supervisor::new(Some(state)).with_ctrl_break_helper(PathBuf::from(HELPER)))
}

/// A repository cut over to the store, whose developer dispatch is dispatched with the Python
/// `script` as its launch spec and `timeout` and `grace` as its pinned limits. The name of the
/// test goes into the brief, so every test has its own operation id and job object.
struct Repo {
    root: PathBuf,
}

impl Repo {
    fn new(name: &str, script: &str, timeout: i64, grace: i64) -> Self {
        let root = std::env::temp_dir().join(format!("hr-win-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let state = root.join(".harness/orchestration/state");
        copy_tree(Path::new(FIXTURE), &state);
        let generation = state.join("generations/generation-7a4a6d6e");
        fs::remove_file(generation.join("qa-lane/lease.json")).unwrap();
        fs::remove_file(generation.join("qa-lane/queue/00000000000000000001-dispatch.json"))
            .unwrap();
        state::migrate::run(&root).unwrap();
        let repo = Self { root };
        let connection = repo.store();
        let body = |table: &str| -> serde_json::Map<String, Value> {
            let text: String = connection
                .query_row(
                    &format!("SELECT body_json FROM {table} WHERE id = ?1"),
                    [DISPATCH],
                    |row| row.get(0),
                )
                .unwrap();
            serde_json::from_str(&text).unwrap()
        };
        let mut brief = body("dispatch");
        brief.insert("resolved_transport".to_owned(), json!("external"));
        brief.insert("test_case".to_owned(), json!(name));
        brief.insert(
            "execution".to_owned(),
            json!({"supervisor": "harness-runtime", "timeout_seconds": timeout,
                   "stop_grace_seconds": grace, "poll_interval_seconds": 1}),
        );
        common::pin_hook_policy(&mut brief, &Value::Null);
        let mut status = body("dispatch_status");
        status.insert(
            "launch".to_owned(),
            json!({"argv": ["python", "-c", script], "cwd": std::env::temp_dir()}),
        );
        connection
            .execute(
                "UPDATE dispatch SET body_json = ?2 WHERE id = ?1",
                [DISPATCH, &Value::Object(brief).to_string()],
            )
            .unwrap();
        connection
            .execute(
                "UPDATE dispatch_status SET state = 'dispatched', body_json = ?2 WHERE id = ?1",
                [DISPATCH, &Value::Object(status).to_string()],
            )
            .unwrap();
        repo
    }

    fn state(&self) -> PathBuf {
        self.root.join(".harness/orchestration/state")
    }

    fn store(&self) -> Connection {
        store_of(&self.state())
    }

    fn operation_id(&self) -> String {
        operation_id_of(&self.state())
    }

    fn job_name(&self) -> String {
        job::name(&self.operation_id())
    }

    fn execution(&self) -> Execution {
        executions::of_dispatch(&self.store(), DISPATCH)
            .unwrap()
            .unwrap()
    }

    /// Wait up to `seconds` until the execution is in `state`, and return it; `runtime` reconciles
    /// meanwhile.
    fn wait_for(
        &self,
        state: ExecutionState,
        seconds: u64,
        runtime: Option<&Arc<Supervisor>>,
    ) -> Execution {
        let until = Instant::now() + Duration::from_secs(seconds);
        loop {
            if let Some(runtime) = runtime {
                runtime.reconcile_all();
            }
            let execution = self.execution();
            if execution.state == state {
                return execution;
            }
            assert!(
                Instant::now() <= until,
                "the execution is not {state:?} within {seconds} s; {}",
                self.seen(&execution)
            );
            std::thread::sleep(Duration::from_millis(100));
        }
    }

    /// The pid a leader printed as the first line of its stdout, waited for up to `seconds`.
    fn printed_pid(&self, seconds: u64) -> u32 {
        let until = Instant::now() + Duration::from_secs(seconds);
        loop {
            let execution = self.execution();
            let printed = execution.launch["logs"]["stdout"]
                .as_str()
                .and_then(|path| fs::read_to_string(path).ok())
                .and_then(|text| text.lines().next()?.trim().parse().ok());
            if let Some(pid) = printed {
                return pid;
            }
            assert!(
                Instant::now() <= until,
                "the leader printed no pid within {seconds} s; {}",
                self.seen(&execution)
            );
            std::thread::sleep(Duration::from_millis(50));
        }
    }

    /// What a test that waited in vain saw: the execution, the members of its job and the logs of
    /// its leader.
    fn seen(&self, execution: &Execution) -> String {
        let members = match Job::open(&self.job_name(), JobAccess::Query) {
            Ok(Some(job)) => format!("{:?}", job.processes()),
            Ok(None) => "no job object".to_owned(),
            Err(err) => format!("the job object cannot be opened: {err}"),
        };
        let log = |stream: &str| {
            execution.launch["logs"][stream]
                .as_str()
                .map(|path| fs::read_to_string(path).unwrap_or_else(|err| format!("<{err}>")))
                .unwrap_or_default()
        };
        format!(
            "execution: {execution:?}; job members: {members}; leader stdout: {:?}; leader stderr: {:?}",
            log("stdout"),
            log("stderr")
        )
    }
}

impl Drop for Repo {
    fn drop(&mut self) {
        if let Ok(Some(job)) = Job::open(&self.job_name(), JobAccess::Control) {
            let _ = job.terminate(1);
        }
        std::thread::sleep(Duration::from_millis(300));
        let _ = fs::remove_dir_all(&self.root);
    }
}

/// The first line `child` prints on its piped stdout, waited for up to `within`.
fn first_line(child: &mut Child, within: Duration) -> String {
    let (send, lines) = mpsc::channel();
    forward(child.stdout.take().unwrap(), false, send);
    lines.recv_timeout(within).map_or_else(
        |err| {
            panic!(
                "the process printed no line within {} s ({err}); try_wait: {:?}",
                within.as_secs(),
                child.try_wait()
            )
        },
        |(_, line)| line,
    )
}

/// Send every line of `pipe` to `lines`, marked `stderr` or not, until its end.
fn forward(pipe: impl Read + Send + 'static, stderr: bool, lines: Sender<(bool, String)>) {
    std::thread::spawn(move || {
        let mut reader = BufReader::new(pipe);
        let mut line = Vec::new();
        while matches!(reader.read_until(b'\n', &mut line), Ok(read) if read > 0) {
            let text = String::from_utf8_lossy(&line).trim_end().to_owned();
            if lines.send((stderr, text)).is_err() {
                return;
            }
            line.clear();
        }
    });
}

/// A runtime in a process of its own: this test binary running only `runtime_process` on the
/// store of a repository. It is killed and waited for at the end of the test, whatever happened.
struct Runtime {
    child: Child,
    _stdin: ChildStdin,
    lines: Receiver<(bool, String)>,
    /// Every line read from it so far, for the message of a wait in vain.
    journal: Vec<String>,
}

impl Runtime {
    /// Start it; with `pause_at`, it pauses at that debug-build step of its launch.
    fn start(repo: &Repo, pause_at: Option<&str>) -> Self {
        let mut command = Command::new(std::env::current_exe().unwrap());
        command
            .args([
                "runtime_process",
                "--exact",
                "--ignored",
                "--nocapture",
                "--test-threads=1",
            ])
            .env(RUNTIME_STATE, repo.state())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        if let Some(step) = pause_at {
            command.env("HARNESS_RUNTIME_TEST_PAUSE_AT", step);
        }
        let mut child = command.spawn().unwrap();
        let stdin = child.stdin.take().unwrap();
        let (send, lines) = mpsc::channel();
        forward(child.stdout.take().unwrap(), false, send.clone());
        forward(child.stderr.take().unwrap(), true, send);
        Self {
            child,
            _stdin: stdin,
            lines,
            journal: Vec::new(),
        }
    }

    /// The rest of the first line of its stdout, or of its stderr, after `marker`. The marker may
    /// follow other text: libtest prints `test runtime_process ... ` without a line end before the
    /// test prints. A runtime that prints no such line within `LINE_WAIT` fails the test with
    /// everything it printed and whether it ended.
    fn line(&mut self, stderr: bool, marker: &str) -> String {
        let until = Instant::now() + LINE_WAIT;
        loop {
            match self
                .lines
                .recv_timeout(until.saturating_duration_since(Instant::now()))
            {
                Ok((from_stderr, line)) => {
                    let rest = line
                        .split_once(marker)
                        .filter(|_| from_stderr == stderr)
                        .map(|(_, rest)| rest.to_owned());
                    let stream = if from_stderr { "stderr" } else { "stdout" };
                    self.journal.push(format!("{stream}: {line}"));
                    if let Some(rest) = rest {
                        return rest;
                    }
                }
                Err(err) => panic!(
                    "no {} line with {marker:?} from the runtime within {} s ({err}); try_wait: {:?}; its output:\n{}",
                    if stderr { "stderr" } else { "stdout" },
                    LINE_WAIT.as_secs(),
                    self.child.try_wait(),
                    self.journal.join("\n")
                ),
            }
        }
    }

    /// The receipt the runtime printed after its launch.
    fn receipt(&mut self) -> Value {
        serde_json::from_str(&self.line(false, "RECEIPT ")).unwrap()
    }

    /// Kill it with `TerminateProcess` and wait up to `END_WAIT` for its end: nothing of it runs
    /// any more.
    fn kill(mut self) {
        self.child.kill().unwrap();
        let ended = reap(&mut self.child, END_WAIT);
        assert!(
            matches!(ended, Ok(Some(_))),
            "the killed runtime {} did not end within {} s: {ended:?}",
            self.child.id(),
            END_WAIT.as_secs()
        );
    }
}

impl Drop for Runtime {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = reap(&mut self.child, END_WAIT);
    }
}

/// The runtime process of the restart tests: it launches the dispatch of the store a test names,
/// prints the receipt and keeps its supervisor until the test kills it.
#[test]
#[ignore = "the runtime process of the restart tests: a test starts it with its store"]
fn runtime_process() {
    let Ok(state) = std::env::var(RUNTIME_STATE) else {
        return;
    };
    let state = PathBuf::from(state);
    let supervisor = supervisor_of(state.clone());
    let receipt = supervisor
        .launch(&launch_request(operation_id_of(&state)))
        .unwrap();
    println!("RECEIPT {receipt}");
    loop {
        std::thread::sleep(Duration::from_secs(1));
    }
}

fn leader_pid(receipt: &Value) -> u32 {
    u32::try_from(receipt["identity"]["pid"].as_i64().unwrap()).unwrap()
}

#[test]
fn a_deadline_stop_sends_ctrl_break_and_an_obeying_execution_ends_without_terminate_job_object() {
    let repo = Repo::new("obeys", OBEYS, 2, 20);
    let supervisor = supervisor_of(repo.state());

    supervisor
        .launch(&launch_request(repo.operation_id()))
        .unwrap();

    let exited = repo.wait_for(ExecutionState::Exited, 40, None);
    let exit = exited.exit.unwrap();
    assert_eq!(exit["stopped_by"], "deadline", "{exit}");
    assert_eq!(exit["ctrl_break"], "delivered", "{exit}");
    assert_eq!(exit["exit_code"], 7, "{exit}");
    assert_eq!(exit["kill_sent_at"], Value::Null, "{exit}");
    assert_eq!(exit["source"], "wait-status");
    assert!(exit["term_sent_at"].is_string());
    assert!(exited.lease_released_at.is_some());
}

#[test]
fn an_execution_that_ignores_ctrl_break_ends_by_terminate_job_object_after_the_grace() {
    let repo = Repo::new("ignores", IGNORES, 2, 2);
    let supervisor = supervisor_of(repo.state());

    let receipt = supervisor
        .launch(&launch_request(repo.operation_id()))
        .unwrap();

    let stopping = repo.wait_for(ExecutionState::Stopping, 30, None);
    assert_eq!(stopping.lease_released_at, None);
    assert_eq!(stopping.observation.unwrap()["ctrl_break"], "delivered");
    let exited = repo.wait_for(ExecutionState::Exited, 40, None);
    let exit = exited.exit.unwrap();
    assert_eq!(exit["exit_code"], 137, "{exit}");
    assert!(exit["kill_sent_at"].is_string(), "{exit}");
    assert_eq!(exit["ctrl_break"], "delivered");
    assert!(exited.lease_released_at.is_some());
    let leader = Process::open(leader_pid(&receipt)).unwrap();
    assert!(leader.is_none_or(|leader| leader.exit_code().unwrap().is_some()));
}

#[test]
fn an_execution_outlives_its_runtime_and_a_new_runtime_records_its_exit_code() {
    let marker = std::env::temp_dir().join(format!("hr-win-marker-{}", std::process::id()));
    let _ = fs::remove_file(&marker);
    let script = format!(
        "import os, sys, time\nwhile not os.path.exists(r'{}'): time.sleep(0.05)\nsys.exit(5)",
        marker.display()
    );
    let repo = Repo::new("outlives", &script, 300, 5);
    let mut runtime = Runtime::start(&repo, None);
    let receipt = runtime.receipt();

    runtime.kill();

    let leader = Process::open(leader_pid(&receipt)).unwrap().unwrap();
    assert_eq!(leader.exit_code().unwrap(), None);
    let supervisor = supervisor_of(repo.state());
    supervisor.reconcile_all();
    let running = repo.execution();
    assert_eq!(running.state, ExecutionState::Running);
    assert_eq!(running.lease_released_at, None);
    assert!(
        Job::open(&repo.job_name(), JobAccess::Query)
            .unwrap()
            .is_some()
    );
    fs::write(&marker, "").unwrap();
    let exited = repo.wait_for(ExecutionState::Exited, 30, Some(&supervisor));
    let exit = exited.exit.unwrap();
    assert_eq!(exit["exit_code"], 5, "{exit}");
    assert_eq!(exit["source"], "process-handle");
    assert!(exited.lease_released_at.is_some());
    let _ = fs::remove_file(&marker);
}

#[test]
fn a_new_runtime_records_the_exit_of_a_leader_whose_console_host_lives_on() {
    let marker = std::env::temp_dir().join(format!("hr-win-host-marker-{}", std::process::id()));
    let _ = fs::remove_file(&marker);
    let script = format!(
        "import os, sys, time\nwhile not os.path.exists(r'{}'): time.sleep(0.05)\nsys.exit(5)",
        marker.display()
    );
    let repo = Repo::new("host", &script, 300, 5);
    let mut runtime = Runtime::start(&repo, None);
    let leader = leader_pid(&runtime.receipt());
    runtime.kill();
    // The test holds the job, so its name outlives the leader.
    let job = Job::open(&repo.job_name(), JobAccess::Query)
        .unwrap()
        .unwrap();
    windows_job::wait_for(
        &job,
        "host before the exit",
        &[
            Expected::python("leader", leader),
            Expected::console_host_of(leader),
        ],
        20,
    );
    // A process outside the job attaches to the hidden console of the leader, so its console host
    // lives on after the leader.
    let holder_script = format!(
        "import ctypes, time\nkernel32 = ctypes.WinDLL('kernel32', use_last_error=True)\nprint('ATTACHED' if kernel32.AttachConsole({leader}) else 'FAILED %d' % ctypes.get_last_error(), flush=True)\nwhile True: time.sleep(0.1)"
    );
    let mut holder = Reaper(
        Command::new("python")
            .args(["-c", &holder_script])
            .creation_flags(DETACHED_PROCESS)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .unwrap(),
    );
    assert_eq!(first_line(&mut holder.0, LINE_WAIT), "ATTACHED");
    let supervisor = supervisor_of(repo.state());
    supervisor.reconcile_all();
    let running = repo.execution();
    assert_eq!(
        running.state,
        ExecutionState::Running,
        "{}",
        repo.seen(&running)
    );
    assert_eq!(running.lease_released_at, None);

    fs::write(&marker, "").unwrap();

    let exited = repo.wait_for(ExecutionState::Exited, 30, Some(&supervisor));
    let exit = exited.exit.unwrap();
    assert_eq!(exit["exit_code"], 5, "{exit}");
    assert_eq!(exit["source"], "process-handle", "{exit}");
    assert!(exited.lease_released_at.is_some());
    // The console host of the leader still runs in the job: the execution ended without it.
    let after = windows_job::members(&job);
    windows_job::print("host after the exit", &after);
    assert!(
        windows_job::composed_of(&after, &[Expected::console_host_of(leader)]),
        "{}",
        windows_job::text(&after)
    );
    let _ = fs::remove_file(&marker);
}

#[test]
fn a_stop_begun_before_a_restart_is_finished_by_the_next_runtime() {
    let repo = Repo::new("resumed", IGNORES, 2, 6);
    let mut runtime = Runtime::start(&repo, None);
    runtime.receipt();
    let stopping = repo.wait_for(ExecutionState::Stopping, 30, None);

    runtime.kill();

    let term_sent_at = stopping.observation.unwrap()["term_sent_at"].clone();
    let supervisor = supervisor_of(repo.state());
    let exited = repo.wait_for(ExecutionState::Exited, 60, Some(&supervisor));
    let exit = exited.exit.unwrap();
    assert_eq!(exit["stopped_by"], "deadline", "{exit}");
    assert_eq!(exit["term_sent_at"], term_sent_at);
    assert_eq!(exit["exit_code"], 137, "{exit}");
    assert_eq!(exit["source"], "process-handle");
    assert!(exit["kill_sent_at"].is_string());
    assert!(exited.lease_released_at.is_some());
}

#[test]
fn a_runtime_killed_after_the_spawn_is_followed_by_the_adoption_of_the_earliest_member() {
    let script = "import os, subprocess, sys, time\nprint(os.getpid(), flush=True)\nsubprocess.Popen([sys.executable, '-c', 'import time\\nwhile True: time.sleep(0.05)'])\nwhile True: time.sleep(0.05)";
    let repo = Repo::new("adopt", script, 300, 5);
    let mut runtime = Runtime::start(&repo, Some("execution.launch:launched"));
    runtime.line(true, "PAUSED:");
    let job = Job::open(&repo.job_name(), JobAccess::Query)
        .unwrap()
        .unwrap();
    let leader = repo.printed_pid(20);
    let before = windows_job::wait_for(
        &job,
        "adopt before the restart",
        &[
            Expected::python("leader", leader),
            Expected::python_child_of("child of the leader", leader),
            Expected::console_host_of(leader),
        ],
        20,
    );

    runtime.kill();

    let supervisor = supervisor_of(repo.state());
    supervisor.reconcile_all();
    let running = repo.execution();
    assert_eq!(
        running.state,
        ExecutionState::Running,
        "{}",
        repo.seen(&running)
    );
    let receipt = running.receipt.unwrap();
    assert_eq!(receipt["adopted"], true);
    assert_eq!(receipt["accounting"], "job-object");
    assert_eq!(receipt["identity"]["pid"], leader, "{receipt}");
    // The adoption launched nothing: the members are the ones before the restart.
    let after = windows_job::members(&job);
    windows_job::print("adopt after the restart", &after);
    assert_eq!(
        windows_job::pids(&after),
        windows_job::pids(&before),
        "{}",
        windows_job::text(&after)
    );
}

#[test]
fn an_adoption_takes_the_leader_its_runtime_launched_and_never_an_earlier_member() {
    // A decoy older than the runtime and its leader, created by this test, not by the runtime.
    let mut decoy = Reaper(
        Command::new("python")
            .args([
                "-c",
                "import time\nprint('READY', flush=True)\nwhile True: time.sleep(0.05)",
            ])
            .creation_flags(CREATE_NO_WINDOW)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .unwrap(),
    );
    assert_eq!(first_line(&mut decoy.0, LINE_WAIT), "READY");
    let decoy_process = Process::from_child(&decoy.0).unwrap();
    let script = "import os, time\nprint(os.getpid(), flush=True)\nwhile True: time.sleep(0.05)";
    let repo = Repo::new("decoy", script, 300, 5);
    let mut runtime = Runtime::start(&repo, Some("execution.launch:launched"));
    runtime.line(true, "PAUSED:");
    let leader = repo.printed_pid(20);
    // The decoy joins the job of the execution after the leader: the earliest created member is
    // not the leader.
    let job = Job::open(&repo.job_name(), JobAccess::Assign)
        .unwrap()
        .unwrap();
    job.assign(&decoy_process).unwrap();
    let before = windows_job::wait_for(
        &job,
        "decoy before the restart",
        &[
            Expected::python("leader", leader),
            Expected::python("decoy", decoy_process.pid()),
            Expected::console_host_of(leader),
        ],
        20,
    );
    let leader_created = Process::open(leader)
        .unwrap()
        .unwrap()
        .creation_time()
        .unwrap();
    assert!(decoy_process.creation_time().unwrap() < leader_created);

    runtime.kill();

    let supervisor = supervisor_of(repo.state());
    supervisor.reconcile_all();
    let running = repo.execution();
    assert_eq!(
        running.state,
        ExecutionState::Running,
        "{}",
        repo.seen(&running)
    );
    let receipt = running.receipt.unwrap();
    assert_eq!(receipt["adopted"], true);
    assert_eq!(receipt["identity"]["pid"], leader, "{receipt}");
    assert_eq!(
        receipt["identity"]["start_time"],
        leader_created.to_string(),
        "{receipt}"
    );
    // The adoption launched nothing: the members are the ones before the restart.
    let after = windows_job::members(&job);
    windows_job::print("decoy after the restart", &after);
    assert_eq!(
        windows_job::pids(&after),
        windows_job::pids(&before),
        "{}",
        windows_job::text(&after)
    );
}

#[test]
fn a_runtime_killed_after_the_intent_leaves_the_execution_lost_and_launches_nothing() {
    let repo = Repo::new("intent", OBEYS, 300, 5);
    let mut runtime = Runtime::start(&repo, Some("execution.launch:intent"));
    runtime.line(true, "PAUSED:");

    runtime.kill();

    let supervisor = supervisor_of(repo.state());
    supervisor.reconcile_all();
    let lost = repo.execution();
    assert_eq!(lost.state, ExecutionState::Lost, "{lost:?}");
    assert!(
        Job::open(&repo.job_name(), JobAccess::Query)
            .unwrap()
            .is_none()
    );
    let operation: String = repo
        .store()
        .query_row(
            "SELECT state FROM operations WHERE operation_id = ?1",
            [repo.operation_id()],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(operation, "failed");
}

#[test]
fn a_process_at_the_pid_of_a_forged_identity_never_receives_a_signal() {
    let repo = Repo::new("forged", IGNORES, 2, 2);
    let mut runtime = Runtime::start(&repo, None);
    runtime.receipt();
    runtime.kill();
    // A process outside the job takes the place of the leader in a forged identity.
    let mut outsider = Reaper(
        Command::new("python")
            .args(["-c", OBEYS])
            .creation_flags(CREATE_NEW_PROCESS_GROUP)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap(),
    );
    let pid = i64::from(outsider.0.id());
    repo.store()
        .execute(
            "UPDATE executions SET pid = ?2, pgid = ?2, start_time = '1' WHERE id = ?1",
            rusqlite::params![repo.operation_id(), pid],
        )
        .unwrap();

    let supervisor = supervisor_of(repo.state());
    let exited = repo.wait_for(ExecutionState::Exited, 60, Some(&supervisor));

    let exit = exited.exit.unwrap();
    assert_eq!(exit["stopped_by"], "deadline", "{exit}");
    assert_eq!(exit["source"], "stop-observed", "{exit}");
    assert!(exit["kill_sent_at"].is_string(), "{exit}");
    // The outsider obeys CTRL_BREAK by its exit: it is still running, so it got none.
    assert_eq!(outsider.0.try_wait().unwrap(), None);
}
