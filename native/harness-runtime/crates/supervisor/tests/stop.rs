//! The deadline stop of an execution the supervisor launched from a cut-over store: SIGTERM to
//! the process group, SIGKILL after the grace when the group is still there, the row `stopping`
//! with the lease held meanwhile, and the exit evidence recorded with the lease release in one
//! transition; a group that outlives its leader holds the lease until the stop ends it (ADR 0022,
//! section 3).
#![cfg(target_os = "linux")]

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use contracts::{ExecutionState, PROTOCOL_VERSION, Request};
use rusqlite::Connection;
use serde_json::{Value, json};
use state::canonical::{canonical_json, object_sha256, sha256_hex};
use state::executions::{self, Execution};
use supervisor::Supervisor;

const DISPATCH: &str = "dispatch-11111111-0000-4000-8000-000000000001";
const FIXTURE: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../state/tests/fixtures/legacy_v3"
);

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

/// A repository cut over to the store, whose developer dispatch is dispatched with `script` as
/// its launch spec and `timeout` and `grace` as its pinned limits; the leftover process groups
/// are killed at the end.
struct Repo {
    root: PathBuf,
    groups: Vec<i64>,
}

impl Repo {
    fn new(name: &str, script: &str, timeout: i64, grace: i64) -> Self {
        let root =
            std::env::temp_dir().join(format!("hr-supervisor-stop-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let state = root.join(".harness/orchestration/state");
        copy_tree(Path::new(FIXTURE), &state);
        let generation = state.join("generations/generation-7a4a6d6e");
        fs::remove_file(generation.join("qa-lane/lease.json")).unwrap();
        fs::remove_file(generation.join("qa-lane/queue/00000000000000000001-dispatch.json"))
            .unwrap();
        state::migrate::run(&root).unwrap();
        let repo = Self {
            root,
            groups: Vec::new(),
        };
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
        brief.insert(
            "execution".to_owned(),
            json!({"supervisor": "harness-runtime", "timeout_seconds": timeout,
                   "stop_grace_seconds": grace, "poll_interval_seconds": 1}),
        );
        let mut status = body("dispatch_status");
        status.insert(
            "launch".to_owned(),
            json!({"argv": ["sh", "-c", script], "cwd": "/"}),
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

    fn execution(&self) -> Execution {
        executions::of_dispatch(&self.store(), DISPATCH)
            .unwrap()
            .unwrap()
    }

    fn launch(&mut self, supervisor: &Arc<Supervisor>) -> Value {
        let (brief, _) = executions::dispatch_records(&self.store(), DISPATCH)
            .unwrap()
            .unwrap();
        let operation_id =
            sha256_hex(canonical_json(&json!([DISPATCH, object_sha256(&brief), 1])).as_bytes());
        let deadline = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis()
            + 30_000;
        let request = Request {
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
        };
        let receipt = supervisor.launch(&request).unwrap();
        self.groups.push(receipt["pgid"].as_i64().unwrap());
        receipt
    }

    /// Wait until the execution is in `state`, and return it.
    fn wait_for(&self, state: ExecutionState, seconds: u64) -> Execution {
        let until = Instant::now() + Duration::from_secs(seconds);
        loop {
            let execution = self.execution();
            if execution.state == state || Instant::now() > until {
                assert_eq!(execution.state, state, "{execution:?}");
                return execution;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
    }
}

impl Drop for Repo {
    fn drop(&mut self) {
        for group in &self.groups {
            let _ = std::process::Command::new("kill")
                .args(["-KILL", "--", &format!("-{group}")])
                .stderr(std::process::Stdio::null())
                .status();
        }
        let _ = fs::remove_dir_all(&self.root);
    }
}

fn group_alive(pgid: i64) -> bool {
    std::process::Command::new("kill")
        .args(["-0", "--", &format!("-{pgid}")])
        .stderr(std::process::Stdio::null())
        .status()
        .unwrap()
        .success()
}

fn audit_actions(repo: &Repo) -> Vec<String> {
    let connection = repo.store();
    let mut statement = connection
        .prepare("SELECT action FROM audit WHERE action LIKE 'execution-%' ORDER BY seq")
        .unwrap();
    statement
        .query_map([], |row| row.get(0))
        .unwrap()
        .map(Result::unwrap)
        .collect()
}

#[test]
fn a_group_that_ignores_sigterm_is_killed_after_the_grace_and_the_lease_is_released_with_the_exit()
{
    let mut repo = Repo::new("ignores", "trap '' TERM; sleep 30 & wait", 1, 2);
    let supervisor = Arc::new(Supervisor::new(Some(repo.state())));

    let receipt = repo.launch(&supervisor);
    let pgid = receipt["pgid"].as_i64().unwrap();

    let stopping = repo.wait_for(ExecutionState::Stopping, 10);
    assert_eq!(stopping.lease_released_at, None);
    assert!(group_alive(pgid));
    let exited = repo.wait_for(ExecutionState::Exited, 20);
    let exit = exited.exit.unwrap();
    assert_eq!(exit["stopped_by"], "deadline");
    assert_eq!(exit["signal"], 9);
    assert_eq!(exit["source"], "wait-status");
    assert!(exit["term_sent_at"].is_string() && exit["kill_sent_at"].is_string());
    assert_eq!(exit["logs"].as_array().unwrap().len(), 2);
    assert!(exited.lease_released_at.is_some());
    assert!(!group_alive(pgid));
    assert_eq!(
        audit_actions(&repo),
        [
            "execution-launching",
            "execution-running",
            "execution-stopping",
            "execution-exited"
        ]
    );
}

#[test]
fn a_group_that_obeys_sigterm_ends_without_sigkill() {
    let mut repo = Repo::new("obeys", "sleep 30", 1, 10);
    let supervisor = Arc::new(Supervisor::new(Some(repo.state())));

    let receipt = repo.launch(&supervisor);

    let exited = repo.wait_for(ExecutionState::Exited, 10);
    let exit = exited.exit.unwrap();
    assert_eq!(exit["signal"], 15);
    assert_eq!(exit["kill_sent_at"], Value::Null);
    assert!(exited.lease_released_at.is_some());
    assert!(!group_alive(receipt["pgid"].as_i64().unwrap()));
}

#[test]
fn a_natural_exit_records_the_wait_status_and_releases_the_lease() {
    let mut repo = Repo::new("natural", "exit 3", 60, 1);
    let supervisor = Arc::new(Supervisor::new(Some(repo.state())));

    let receipt = repo.launch(&supervisor);
    // A repeat of the launch answers the stored receipt and starts nothing.
    let again = repo.launch(&supervisor);

    assert_eq!(again, receipt);
    let exited = repo.wait_for(ExecutionState::Exited, 10);
    assert_eq!(exited.exit.unwrap()["exit_code"], 3);
    assert!(exited.lease_released_at.is_some());
}

#[test]
fn a_group_that_outlives_its_leader_keeps_the_lease_until_the_deadline_stops_it() {
    let mut repo = Repo::new("outlived", "sleep 30 & exit 3", 2, 1);
    let supervisor = Arc::new(Supervisor::new(Some(repo.state())));

    let receipt = repo.launch(&supervisor);
    let pgid = receipt["pgid"].as_i64().unwrap();

    std::thread::sleep(Duration::from_millis(1_000));
    let running = repo.execution();
    assert_eq!(running.state, ExecutionState::Running);
    assert_eq!(running.lease_released_at, None);
    assert!(group_alive(pgid));
    let exited = repo.wait_for(ExecutionState::Exited, 20);
    let exit = exited.exit.unwrap();
    assert_eq!(exit["exit_code"], 3);
    assert_eq!(exit["stopped_by"], "deadline");
    assert!(exited.lease_released_at.is_some());
    assert!(!group_alive(pgid));
}
