//! The `harness-runtime` binary: every error it prints is followed by a `REMEDY:` line whose
//! first token is the protocol error code in brackets (ADR 0021, section 8; DoD 1 of the
//! failure-domain ticket).

use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicUsize, Ordering};

use contracts::ErrorCode;

const FIXTURE: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../state/tests/fixtures/legacy_v3"
);

/// A scratch directory that is removed when the test ends.
struct Scratch(PathBuf);

impl Scratch {
    fn new(name: &str) -> Self {
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        let n = NEXT.fetch_add(1, Ordering::Relaxed);
        let root = std::env::temp_dir().join(format!("hr-cli-{name}-{}-{n}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        Self(root)
    }

    /// A git working tree root without any orchestration state.
    fn repo(name: &str) -> Self {
        let scratch = Self::new(name);
        fs::create_dir(scratch.0.join(".git")).unwrap();
        scratch
    }

    fn state(&self) -> PathBuf {
        self.0.join(".harness/orchestration/state")
    }

    /// A repository whose state is the legacy fixture: it holds a QA lease and a queued run.
    fn repo_with_ledger(name: &str) -> Self {
        let scratch = Self::repo(name);
        copy_tree(Path::new(FIXTURE), &scratch.state());
        scratch
    }

    fn generation(&self) -> PathBuf {
        let pointer: serde_json::Value =
            serde_json::from_slice(&fs::read(self.state().join("ledger.json")).unwrap()).unwrap();
        self.state()
            .join("generations")
            .join(pointer["generation"].as_str().unwrap())
    }

    fn without_qa_lane_entries(self) -> Self {
        let generation = self.generation();
        fs::remove_file(generation.join("qa-lane/lease.json")).unwrap();
        fs::remove_file(generation.join("qa-lane/queue/00000000000000000001-dispatch.json"))
            .unwrap();
        self
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
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

fn run(args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_harness-runtime"))
        .args(args)
        .env_remove("XDG_RUNTIME_DIR")
        .output()
        .unwrap()
}

fn stderr_lines(output: &Output) -> Vec<String> {
    String::from_utf8_lossy(&output.stderr)
        .lines()
        .map(str::to_owned)
        .collect()
}

/// The exit code, and the first `REMEDY:` line of a failed run, after the `ERROR:` line.
fn refusal(output: &Output) -> (i32, String) {
    let lines = stderr_lines(output);
    assert!(lines[0].starts_with("ERROR: "), "{lines:?}");
    (output.status.code().unwrap(), lines[1].clone())
}

#[test]
fn an_unknown_command_prints_the_malformed_request_code_first() {
    let (code, remedy) = refusal(&run(&["rewind"]));

    assert_eq!(code, 2);
    assert!(
        remedy.starts_with("REMEDY: [malformed_request] "),
        "{remedy}"
    );
}

#[test]
fn migrate_without_a_ledger_prints_the_not_found_code_first() {
    let repo = Scratch::repo("no-ledger");
    fs::create_dir_all(repo.state()).unwrap();

    let (code, remedy) = refusal(&run(&["migrate", "--repo", repo.0.to_str().unwrap()]));

    assert_eq!(code, 2);
    assert!(remedy.starts_with("REMEDY: [not_found] "), "{remedy}");
}

#[test]
fn migrate_while_the_ledger_lock_is_held_prints_the_store_busy_code_first() {
    let repo = Scratch::repo_with_ledger("busy").without_qa_lane_entries();
    // A lock directory without an owner record: its owner cannot be judged, so the lock stays.
    fs::create_dir(repo.state().join(".coordinator.lock")).unwrap();

    let (code, remedy) = refusal(&run(&["migrate", "--repo", repo.0.to_str().unwrap()]));

    assert_eq!(code, 2);
    assert!(remedy.starts_with("REMEDY: [store_busy] "), "{remedy}");
}

#[test]
fn migrate_while_the_qa_lane_holds_a_lease_prints_the_lease_held_code_first() {
    let repo = Scratch::repo_with_ledger("lease");

    let (code, remedy) = refusal(&run(&["migrate", "--repo", repo.0.to_str().unwrap()]));

    assert_eq!(code, 2);
    assert!(remedy.starts_with("REMEDY: [lease_held] "), "{remedy}");
}

#[test]
fn migrate_of_a_newer_ledger_prints_the_protocol_unsupported_code_first() {
    let repo = Scratch::repo_with_ledger("newer");
    let pointer = repo.state().join("ledger.json");
    let text = fs::read_to_string(&pointer).unwrap();
    fs::write(&pointer, text.replace("\"version\": 3", "\"version\": 9")).unwrap();

    let (code, remedy) = refusal(&run(&["migrate", "--repo", repo.0.to_str().unwrap()]));

    assert_eq!(code, 2);
    assert!(
        remedy.starts_with("REMEDY: [protocol_unsupported] "),
        "{remedy}"
    );
}

#[test]
fn serve_in_a_directory_that_is_no_repository_prints_the_not_found_code_first() {
    let dir = Scratch::new("no-repo");

    let (code, remedy) = refusal(&run(&["serve", "--repo", dir.0.to_str().unwrap()]));

    assert_eq!(code, 2);
    assert!(remedy.starts_with("REMEDY: [not_found] "), "{remedy}");
}

/// A cut-over repository (the fixture without its QA lease), and the path of its store.
fn cut_over(name: &str) -> (Scratch, PathBuf) {
    let repo = Scratch::repo_with_ledger(name).without_qa_lane_entries();
    let output = run(&["migrate", "--repo", repo.0.to_str().unwrap()]);
    assert!(output.status.success(), "{:?}", stderr_lines(&output));
    let store = repo.state().join("runtime.db");
    (repo, store)
}

fn user_version(store: &Path) -> i64 {
    rusqlite::Connection::open(store)
        .unwrap()
        .pragma_query_value(None, "user_version", |row| row.get(0))
        .unwrap()
}

/// Write the store back to the schema of version 1, as the first release left it.
fn downgrade_to_version_one(store: &Path) {
    let connection = rusqlite::Connection::open(store).unwrap();
    connection
        .execute_batch(
            "ALTER TABLE dispatch_status DROP COLUMN execution_state;
             ALTER TABLE dispatch_status DROP COLUMN revision;
             ALTER TABLE qa_queue DROP COLUMN revision;
             ALTER TABLE record DROP COLUMN revision;
             DROP TABLE operations;
             DROP TABLE audit_checkpoint;
             CREATE TABLE audit_checkpoint (seq INTEGER NOT NULL, chain_sha256 TEXT NOT NULL) STRICT;
             PRAGMA user_version = 1;",
        )
        .unwrap();
}

/// `harness-runtime serve` until it prints its readiness line or exits; then it is stopped.
fn serve_once(repo: &Scratch) -> Output {
    use std::io::{BufRead, BufReader};
    use std::process::Stdio;

    let runtime_dir = Scratch::new("xdg");
    let mut child = Command::new(env!("CARGO_BIN_EXE_harness-runtime"))
        .args(["serve", "--repo", repo.0.to_str().unwrap()])
        .env("XDG_RUNTIME_DIR", &runtime_dir.0)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut line = String::new();
    BufReader::new(child.stdout.take().unwrap())
        .read_line(&mut line)
        .unwrap();
    let ready = line.contains("\"ready\"");
    if ready {
        child.kill().unwrap();
    }
    child.wait_with_output().unwrap()
}

#[test]
fn serve_upgrades_a_store_of_version_one_and_audits_the_step() {
    let (repo, store) = cut_over("serve-upgrade");
    downgrade_to_version_one(&store);
    assert_eq!(user_version(&store), 1);

    serve_once(&repo);

    assert_eq!(user_version(&store), 2);
    let connection = rusqlite::Connection::open(&store).unwrap();
    let (action, details): (String, String) = connection
        .query_row(
            "SELECT action, details_json FROM audit ORDER BY seq DESC LIMIT 1",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    assert_eq!(action, "schema-migrate");
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&details).unwrap(),
        serde_json::json!({"from": 1, "to": 2})
    );
}

#[test]
fn serve_leaves_a_current_store_unchanged() {
    let (repo, store) = cut_over("serve-current");
    let records = |store: &Path| -> i64 {
        rusqlite::Connection::open(store)
            .unwrap()
            .query_row("SELECT COUNT(*) FROM audit", [], |row| row.get(0))
            .unwrap()
    };
    let before = records(&store);

    serve_once(&repo);

    assert_eq!(records(&store), before);
    assert_eq!(user_version(&store), 2);
}

#[test]
fn serve_of_a_store_of_a_newer_schema_prints_the_protocol_unsupported_code_and_writes_nothing() {
    let (repo, store) = cut_over("serve-newer");
    rusqlite::Connection::open(&store)
        .unwrap()
        .pragma_update(None, "user_version", 3)
        .unwrap();

    let output = serve_once(&repo);

    let (code, remedy) = refusal(&output);
    assert_eq!(code, 2);
    assert!(
        remedy.starts_with("REMEDY: [protocol_unsupported] "),
        "{remedy}"
    );
    assert_eq!(user_version(&store), 3);
}

#[test]
fn every_error_line_is_followed_by_a_remedy_with_a_known_code() {
    let plain = Scratch::new("plain");
    let ledger = Scratch::repo_with_ledger("every");
    let runs = [
        run(&[]),
        run(&["serve", "--socket", "x"]),
        run(&["serve", "--repo"]),
        run(&["serve", "--repo", plain.0.to_str().unwrap()]),
        run(&["migrate", "--repo", plain.0.to_str().unwrap()]),
        run(&["migrate", "--repo", ledger.0.to_str().unwrap()]),
        run(&["rollback", "--repo", ledger.0.to_str().unwrap()]),
        run(&[
            "rollback",
            "--to-legacy",
            "--repo",
            ledger.0.to_str().unwrap(),
        ]),
        run_owned(&rollback_args(&plain, "human:operator")),
        run_owned(&rollback_args(&ledger, "policy:auto")),
        run_owned(&rollback_args(&ledger, "human:operator")),
    ];

    for output in &runs {
        let lines = stderr_lines(output);
        assert!(!output.status.success(), "{lines:?}");
        for (at, line) in lines.iter().enumerate() {
            if !line.starts_with("ERROR: ") {
                continue;
            }
            let remedy = lines.get(at + 1).unwrap_or_else(|| panic!("{lines:?}"));
            let code = remedy
                .strip_prefix("REMEDY: [")
                .and_then(|rest| rest.split_once("] "))
                .map(|(code, _)| code)
                .unwrap_or_else(|| panic!("no code in {remedy:?}"));
            assert!(
                ErrorCode::ALL.iter().any(|known| known.as_str() == code),
                "{code} is not a protocol error code"
            );
        }
    }
}

fn rollback_args(repo: &Scratch, approved_by: &str) -> Vec<String> {
    [
        "rollback",
        "--to-legacy",
        "--repo",
        repo.0.to_str().unwrap(),
        "--approved-by",
        approved_by,
        "--approved-at",
        "2026-10-27T10:00:00+00:00",
        "--reason",
        "return to 1.x",
    ]
    .map(str::to_owned)
    .to_vec()
}

fn run_rollback(repo: &Scratch, approved_by: &str, env: &[(&str, &std::ffi::OsStr)]) -> Output {
    let mut command = Command::new(env!("CARGO_BIN_EXE_harness-runtime"));
    command
        .args(rollback_args(repo, approved_by))
        .env_remove("XDG_RUNTIME_DIR");
    for (key, value) in env {
        command.env(key, value);
    }
    command.output().unwrap()
}

fn pointer(repo: &Scratch) -> serde_json::Value {
    serde_json::from_slice(&fs::read(repo.state().join("ledger.json")).unwrap()).unwrap()
}

#[test]
fn rollback_prints_one_json_line_and_returns_the_state_to_the_file_ledger() {
    let (repo, store) = cut_over("rollback");

    let output = run_rollback(&repo, "human:operator", &[]);

    assert!(output.status.success(), "{:?}", stderr_lines(&output));
    let line: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(line["rolled_back"], true);
    assert_eq!(pointer(&repo)["version"], 3);
    assert_eq!(pointer(&repo)["generation"], line["generation"]);
    assert!(store.exists());
}

#[test]
fn rollback_of_a_policy_approver_prints_the_policy_denied_code_and_changes_nothing() {
    let (repo, _) = cut_over("rb-policy");
    let before = fs::read(repo.state().join("ledger.json")).unwrap();

    let (code, remedy) = refusal(&run_rollback(&repo, "policy:auto", &[]));

    assert_eq!(code, 2);
    assert!(remedy.starts_with("REMEDY: [policy_denied] "), "{remedy}");
    assert_eq!(fs::read(repo.state().join("ledger.json")).unwrap(), before);
}

#[test]
fn rollback_while_serve_runs_prints_the_lease_held_code_and_changes_nothing() {
    use std::io::{BufRead, BufReader};
    use std::process::Stdio;

    let (repo, _) = cut_over("rb-serving");
    let runtime_dir = Scratch::new("xdg-rollback");
    let mut serve = Command::new(env!("CARGO_BIN_EXE_harness-runtime"))
        .args(["serve", "--repo", repo.0.to_str().unwrap()])
        .env("XDG_RUNTIME_DIR", &runtime_dir.0)
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let mut line = String::new();
    BufReader::new(serve.stdout.take().unwrap())
        .read_line(&mut line)
        .unwrap();
    assert!(line.contains("\"ready\""), "{line}");

    let output = run_rollback(
        &repo,
        "human:operator",
        &[("XDG_RUNTIME_DIR", runtime_dir.0.as_os_str())],
    );
    serve.kill().unwrap();
    serve.wait().unwrap();

    let (code, remedy) = refusal(&output);
    assert_eq!(code, 2);
    assert!(remedy.starts_with("REMEDY: [lease_held] "), "{remedy}");
    assert!(remedy.contains("stop `harness-runtime serve`"), "{remedy}");
    assert_eq!(pointer(&repo)["version"], 4);
}

#[test]
fn rollback_that_stops_before_the_pointer_finishes_on_a_repeat() {
    let (repo, _) = cut_over("rb-crash");

    let failed = run_rollback(
        &repo,
        "human:operator",
        &[(
            "HARNESS_RUNTIME_TEST_FAIL_AT",
            std::ffi::OsStr::new("rollback-pointer-replace"),
        )],
    );

    let (code, remedy) = refusal(&failed);
    assert_eq!(code, 3, "{:?}", stderr_lines(&failed));
    assert!(remedy.starts_with("REMEDY: [internal] "), "{remedy}");
    assert_eq!(pointer(&repo)["version"], 4);
    let authority: serde_json::Value =
        serde_json::from_slice(&fs::read(repo.state().join("AUTHORITY.json")).unwrap()).unwrap();
    assert_eq!(authority["authority"], "file-ledger");

    let repeated = run_rollback(&repo, "human:operator", &[]);

    assert!(repeated.status.success(), "{:?}", stderr_lines(&repeated));
    assert_eq!(pointer(&repo)["version"], 3);
    assert_eq!(pointer(&repo)["generation"], authority["generation"]);
}

#[test]
fn rollback_that_fails_before_the_commit_point_replaces_its_archive_on_a_repeat() {
    let (repo, store) = cut_over("rb-stale");
    let authority: serde_json::Value =
        serde_json::from_slice(&fs::read(repo.state().join("AUTHORITY.json")).unwrap()).unwrap();
    let archive = repo.state().join(format!(
        "runtime-export/{}.tar",
        authority["store_id"].as_str().unwrap()
    ));

    let failed = run_rollback(
        &repo,
        "human:operator",
        &[(
            "HARNESS_RUNTIME_TEST_FAIL_AT",
            std::ffi::OsStr::new("rollback-generation"),
        )],
    );

    let (code, _) = refusal(&failed);
    assert_eq!(code, 3, "{:?}", stderr_lines(&failed));
    assert_eq!(pointer(&repo)["version"], 4);
    let stale = fs::read(&archive).unwrap();
    // The store is still the authority and changes before the repeat.
    rusqlite::Connection::open(&store)
        .unwrap()
        .execute(
            "UPDATE record SET revision = revision + 1 WHERE path = (SELECT MIN(path) FROM record)",
            [],
        )
        .unwrap();

    let repeated = run_rollback(&repo, "human:operator", &[]);

    assert!(repeated.status.success(), "{:?}", stderr_lines(&repeated));
    let line: serde_json::Value = serde_json::from_slice(&repeated.stdout).unwrap();
    let replaced = fs::read(&archive).unwrap();
    assert_ne!(replaced, stale);
    assert_eq!(
        line["export_sha256"],
        state::canonical::sha256_hex(&replaced).as_str()
    );
    assert_eq!(pointer(&repo)["version"], 3);
}

fn run_owned(args: &[String]) -> Output {
    run(&args.iter().map(String::as_str).collect::<Vec<_>>())
}
