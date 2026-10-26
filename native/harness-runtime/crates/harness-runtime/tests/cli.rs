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
    let (code, remedy) = refusal(&run(&["rollback"]));

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
