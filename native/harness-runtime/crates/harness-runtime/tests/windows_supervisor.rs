//! The Windows supervisor end to end, with the ctrl-break helper of this `harness-runtime`
//! executable (ADR 0022, sections 2, 3 and 5): a deadline stop sends `CTRL_BREAK` and an
//! execution that obeys ends without `TerminateJobObject`, one that ignores it ends by
//! `TerminateJobObject` after the grace, and an execution outlives a runtime that dies. A new
//! runtime finds its job by name, records its exit code from the handle it holds, finishes a stop
//! the dead runtime began, adopts the earliest member of the job of an intent without a second
//! launch, records an intent without a process `lost`, and never signals a process that holds the
//! pid of a forged identity.
//!
//! The runtime that dies is this test binary in a process of its own, running only the ignored
//! test `runtime_process`, killed with `TerminateProcess`; the new runtime is a supervisor in the
//! test process. The executions are Python scripts of the `python` on PATH. Runs in the Windows CI
//! job only.
#![cfg(windows)]

use std::fs;
use std::io::{BufRead, BufReader, Read};
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use contracts::{ExecutionState, PROTOCOL_VERSION, Request};
use platform_ffi::{CREATE_NEW_PROCESS_GROUP, Job, JobAccess, Process};
use rusqlite::Connection;
use serde_json::{Value, json};
use state::canonical::{canonical_json, object_sha256, sha256_hex};
use state::executions::{self, Execution};
use supervisor::{Supervisor, job};

const HELPER: &str = env!("CARGO_BIN_EXE_harness-runtime");
const RUNTIME_STATE: &str = "HR_TEST_WINDOWS_RUNTIME_STATE";
const DISPATCH: &str = "dispatch-11111111-0000-4000-8000-000000000001";
const FIXTURE: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../state/tests/fixtures/legacy_v3"
);
const OBEYS: &str = "import signal, sys, time\nsignal.signal(signal.SIGBREAK, lambda *_: sys.exit(7))\nwhile True: time.sleep(0.05)";
const IGNORES: &str = "import signal, time\nsignal.signal(signal.SIGBREAK, signal.SIG_IGN)\nwhile True: time.sleep(0.05)";

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

    /// Wait until the execution is in `state`, and return it; `runtime` reconciles meanwhile.
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
            if execution.state == state || Instant::now() > until {
                assert_eq!(execution.state, state, "{execution:?}");
                return execution;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
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

/// A runtime in a process of its own: this test binary running only `runtime_process` on the
/// store of a repository.
struct Runtime {
    child: Child,
    _stdin: ChildStdin,
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
        Self {
            child,
            _stdin: stdin,
        }
    }

    /// The rest of the first line of its stdout, or of its stderr, that starts with `prefix`.
    fn line(&mut self, stderr: bool, prefix: &str) -> String {
        let pipe: Box<dyn Read> = if stderr {
            Box::new(self.child.stderr.take().unwrap())
        } else {
            Box::new(self.child.stdout.take().unwrap())
        };
        for line in BufReader::new(pipe).lines() {
            let line = line.unwrap();
            if let Some(rest) = line.strip_prefix(prefix) {
                return rest.to_owned();
            }
        }
        panic!("the runtime process ended before a line {prefix:?}");
    }

    /// The receipt the runtime printed after its launch.
    fn receipt(&mut self) -> Value {
        serde_json::from_str(&self.line(false, "RECEIPT ")).unwrap()
    }

    /// Kill it with `TerminateProcess`: nothing of it runs any more.
    fn kill(mut self) {
        self.child.kill().unwrap();
        self.child.wait().unwrap();
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
    let until = Instant::now() + Duration::from_secs(20);
    while job.processes().unwrap().len() < 2 && Instant::now() < until {
        std::thread::sleep(Duration::from_millis(50));
    }
    assert_eq!(job.processes().unwrap().len(), 2);

    runtime.kill();

    let supervisor = supervisor_of(repo.state());
    supervisor.reconcile_all();
    let running = repo.execution();
    assert_eq!(running.state, ExecutionState::Running, "{running:?}");
    let receipt = running.receipt.unwrap();
    assert_eq!(receipt["adopted"], true);
    assert_eq!(receipt["accounting"], "job-object");
    let stdout = running.launch["logs"]["stdout"]
        .as_str()
        .unwrap()
        .to_owned();
    let printed: i64 = fs::read_to_string(stdout)
        .unwrap()
        .lines()
        .next()
        .unwrap()
        .trim()
        .parse()
        .unwrap();
    assert_eq!(receipt["identity"]["pid"], printed);
    // The adoption launched nothing: the leader and its child are the only members.
    assert_eq!(job.active_processes().unwrap(), 2);
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
    let mut outsider = Command::new("python")
        .args(["-c", OBEYS])
        .creation_flags(CREATE_NEW_PROCESS_GROUP)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let pid = i64::from(outsider.id());
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
    assert_eq!(outsider.try_wait().unwrap(), None);
    let _ = outsider.kill();
    let _ = outsider.wait();
}
