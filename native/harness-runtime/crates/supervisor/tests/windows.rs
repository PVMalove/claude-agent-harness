//! The Windows launch of the supervisor from a cut-over store: the leader runs with the labels in
//! the job object derived from its operation id, the receipt records its pid, creation time and
//! job, its exit is recorded with the exit code of the held handle, a launch the OS refuses
//! leaves no process and no job, a job name that exists is never taken over, and a container
//! brief is refused before the intent (ADR 0022, sections 2 and 3). The executions are Python
//! scripts of the `python` on PATH. Runs in the Windows CI job only.
#![cfg(windows)]

mod common;

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use contracts::{ErrorBody, ErrorCode, ExecutionState, PROTOCOL_VERSION, Request};
use platform_ffi::{Job, JobAccess, Process, current_user_sid};
use rusqlite::Connection;
use serde_json::{Value, json};
use state::canonical::{canonical_json, object_sha256, sha256_hex};
use state::executions::{self, Execution};
use supervisor::{Supervisor, job};

const DISPATCH: &str = "dispatch-11111111-0000-4000-8000-000000000001";
const FIXTURE: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../state/tests/fixtures/legacy_v3"
);
const SLEEPER: &str = "import os, time\nprint(os.environ['HARNESS_OPERATION_ID'], flush=True)\nwhile True: time.sleep(0.1)";

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

/// A repository cut over to the store, whose developer dispatch is dispatched with `argv` as its
/// launch spec; the job of the execution is terminated at the end.
struct Repo {
    root: PathBuf,
}

impl Repo {
    fn new(name: &str, argv: &[&str], docker: bool) -> Self {
        let root =
            std::env::temp_dir().join(format!("hr-supervisor-win-{name}-{}", std::process::id()));
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
        // The tests run in parallel: each brief, so each operation id and job name, is its own.
        brief.insert("test_case".to_owned(), json!(name));
        brief.insert(
            "execution".to_owned(),
            json!({"supervisor": "harness-runtime", "timeout_seconds": 300,
                   "stop_grace_seconds": 5, "poll_interval_seconds": 1}),
        );
        if docker {
            brief.insert(
                "runtime_access".to_owned(),
                json!({"runtime_extension": "docker"}),
            );
        }
        common::pin_hook_policy(&mut brief, &Value::Null);
        let mut status = body("dispatch_status");
        status.insert(
            "launch".to_owned(),
            json!({"argv": argv, "cwd": std::env::temp_dir()}),
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
        Connection::open(self.state().join("runtime.db")).unwrap()
    }

    fn execution(&self) -> Option<Execution> {
        executions::of_dispatch(&self.store(), DISPATCH).unwrap()
    }

    fn operation_id(&self) -> String {
        let (brief, _) = executions::dispatch_records(&self.store(), DISPATCH)
            .unwrap()
            .unwrap();
        sha256_hex(canonical_json(&json!([DISPATCH, object_sha256(&brief), 1])).as_bytes())
    }

    fn launch(&self, supervisor: &Arc<Supervisor>) -> Result<Value, ErrorBody> {
        let deadline = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis()
            + 30_000;
        supervisor.launch(&Request {
            protocol_version: PROTOCOL_VERSION,
            operation_id: self.operation_id(),
            operation: "execution.launch".to_owned(),
            request_deadline: u64::try_from(deadline).unwrap(),
            batch_id: None,
            dispatch_id: None,
            candidate_sha: None,
            plan_digest: None,
            expected_revision: None,
            fencing_token: None,
            payload: Some(json!({"dispatch_id": DISPATCH})),
        })
    }

    /// Wait until the execution is in `state`, and return it.
    fn wait_for(&self, state: ExecutionState, seconds: u64) -> Execution {
        let until = Instant::now() + Duration::from_secs(seconds);
        loop {
            let execution = self.execution().unwrap();
            if execution.state == state || Instant::now() > until {
                assert_eq!(execution.state, state, "{execution:?}");
                return execution;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
    }

    fn stdout(&self, operation_id: &str) -> String {
        fs::read_to_string(
            self.state()
                .join("executions")
                .join(operation_id)
                .join("stdout.log"),
        )
        .unwrap_or_default()
    }
}

impl Drop for Repo {
    fn drop(&mut self) {
        if let Ok(Some(job)) = Job::open(&job::name(&self.operation_id()), JobAccess::Control) {
            let _ = job.terminate(1);
        }
        std::thread::sleep(Duration::from_millis(200));
        let _ = fs::remove_dir_all(&self.root);
    }
}

#[test]
fn a_launch_runs_the_leader_with_its_labels_in_the_job_of_its_operation_id() {
    let repo = Repo::new("launch", &["python", "-c", SLEEPER], false);
    let supervisor = Arc::new(Supervisor::new(Some(repo.state())));
    let operation_id = repo.operation_id();

    let receipt = repo.launch(&supervisor).unwrap();

    let name = job::name(&operation_id);
    assert_eq!(receipt["accounting"], "job-object");
    assert_eq!(receipt["job"], name.as_str());
    assert_eq!(receipt["state"], "running");
    let pid = receipt["identity"]["pid"].as_i64().unwrap();
    assert_eq!(receipt["pgid"].as_i64(), Some(pid));
    let leader = Process::open(u32::try_from(pid).unwrap()).unwrap().unwrap();
    assert_eq!(
        receipt["identity"]["start_time"],
        leader.creation_time().unwrap().to_string()
    );
    let job = Job::open(&name, JobAccess::Query).unwrap().unwrap();
    assert!(job.contains(&leader).unwrap());
    assert_eq!(job.limit_flags().unwrap(), 0);
    let execution = repo.execution().unwrap();
    assert_eq!(execution.launch["job"], name.as_str());
    assert_eq!(execution.state, ExecutionState::Running);
    // The leader resumed and carries its label.
    let until = Instant::now() + Duration::from_secs(20);
    while !repo.stdout(&operation_id).contains(&operation_id) && Instant::now() < until {
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(repo.stdout(&operation_id).contains(&operation_id));
    // A repeat answers the stored receipt and starts nothing.
    assert_eq!(repo.launch(&supervisor).unwrap(), receipt);
    assert_eq!(job.active_processes().unwrap(), 1);
}

#[test]
fn a_natural_exit_is_recorded_with_its_exit_code_and_releases_the_lease() {
    let repo = Repo::new(
        "natural",
        &[
            "python",
            "-c",
            "import sys, time\ntime.sleep(1)\nsys.exit(259)",
        ],
        false,
    );
    let supervisor = Arc::new(Supervisor::new(Some(repo.state())));

    repo.launch(&supervisor).unwrap();

    let exited = repo.wait_for(ExecutionState::Exited, 30);
    let exit = exited.exit.unwrap();
    assert_eq!(exit["exit_code"], 259);
    assert_eq!(exit["signal"], Value::Null);
    assert_eq!(exit["source"], "wait-status");
    assert_eq!(exit["logs"].as_array().unwrap().len(), 2);
    assert!(exited.lease_released_at.is_some());
}

#[test]
fn a_launch_the_os_refuses_leaves_no_process_and_no_job() {
    let repo = Repo::new("refused", &["harness-no-such-program-806"], false);
    let supervisor = Arc::new(Supervisor::new(Some(repo.state())));

    let err = repo.launch(&supervisor).unwrap_err();

    assert_eq!(err.code, ErrorCode::EngineUnavailable, "{err:?}");
    let execution = repo.execution().unwrap();
    assert_eq!(execution.state, ExecutionState::Lost);
    assert!(
        Job::open(&job::name(&repo.operation_id()), JobAccess::Query)
            .unwrap()
            .is_none()
    );
    // A repeat answers the stored failure and launches nothing.
    assert_eq!(
        repo.launch(&supervisor).unwrap_err().code,
        ErrorCode::EngineUnavailable
    );
}

#[test]
fn a_job_name_that_exists_is_never_taken_over_by_a_launch() {
    let repo = Repo::new("taken", &["python", "-c", SLEEPER], false);
    let supervisor = Arc::new(Supervisor::new(Some(repo.state())));
    let name = job::name(&repo.operation_id());
    let foreign = Job::create(&name, &current_user_sid().unwrap()).unwrap();

    let err = repo.launch(&supervisor).unwrap_err();

    assert_eq!(err.code, ErrorCode::EngineUnavailable, "{err:?}");
    assert_eq!(repo.execution().unwrap().state, ExecutionState::Lost);
    assert_eq!(foreign.active_processes().unwrap(), 0);
}

#[test]
fn a_container_brief_is_refused_before_the_intent() {
    let repo = Repo::new("container", &["python", "-c", SLEEPER], true);
    let supervisor = Arc::new(Supervisor::new(Some(repo.state())));

    let err = repo.launch(&supervisor).unwrap_err();

    assert_eq!(err.code, ErrorCode::PolicyDenied, "{err:?}");
    assert_eq!(repo.execution(), None);
}
