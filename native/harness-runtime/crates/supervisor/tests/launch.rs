//! The Linux processes of the supervisor: a launched process leads its own group, its recorded
//! identity is the one `/proc` shows, it outlives its handle, the label scan finds the group
//! leader only, and an observer of another boot sees an execution it must not touch.
#![cfg(target_os = "linux")]

use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

use contracts::ExecutionState;
use state::executions::observed;
use state::identity::{self, Probe, Verdict};
use supervisor::process::{self, Spec};

/// A scratch directory for the logs, and the process groups a test started, killed at the end.
struct Scratch {
    dir: PathBuf,
    groups: Vec<i64>,
}

impl Scratch {
    fn new(name: &str) -> Self {
        let dir = std::env::temp_dir().join(format!("hr-supervisor-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        Self {
            dir,
            groups: Vec::new(),
        }
    }

    /// Spawn `script` under `sh -c` with the labels of `operation_id`; its group is killed at
    /// the end of the test.
    fn spawn(&mut self, operation_id: &str, script: &str) -> i64 {
        let argv = ["sh".to_owned(), "-c".to_owned(), script.to_owned()];
        let child = process::spawn(&Spec {
            argv: &argv,
            cwd: Path::new("/"),
            operation_id,
            dispatch_id: "dispatch-test",
            stdout: &self.dir.join("stdout.log"),
            stderr: &self.dir.join("stderr.log"),
        })
        .unwrap();
        let pid = i64::from(child.id());
        self.groups.push(pid);
        // The handle is dropped here: the process must not notice.
        drop(child);
        pid
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        for group in &self.groups {
            let _ = Command::new("kill")
                .args(["-KILL", "--", &format!("-{group}")])
                .status();
        }
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

fn alive(pid: i64) -> bool {
    matches!(identity::probe(pid), Probe::Running(_))
}

#[test]
fn a_launched_process_leads_its_group_has_its_proc_identity_and_outlives_its_handle() {
    let mut scratch = Scratch::new("leader");
    let pid = scratch.spawn("op-leader", "sleep 30");

    let observer = identity::observer();
    let (recorded, pgid) = process::identity_of(pid, &observer).unwrap();

    assert_eq!(pgid, pid);
    assert_eq!(identity::observe(pid, &observer), Some(recorded.clone()));
    assert!(recorded.boot_id.is_some() && recorded.pid_namespace.is_some());
    std::thread::sleep(Duration::from_millis(200));
    assert!(alive(pid));
    assert_eq!(
        identity::verdict(Some(&recorded), &observer, &identity::probe(pid)),
        Verdict::Alive
    );
}

#[test]
fn the_label_scan_finds_the_group_leader_and_never_a_labelled_child() {
    let mut scratch = Scratch::new("scan");
    let pid = scratch.spawn("op-scan", "sleep 30 & wait");
    // The labelled child of the leader is in the group too.
    let deadline = Instant::now() + Duration::from_secs(10);
    let children = loop {
        let found =
            std::fs::read_to_string(format!("/proc/{pid}/task/{pid}/children")).unwrap_or_default();
        if !found.trim().is_empty() || Instant::now() > deadline {
            break found;
        }
        std::thread::sleep(Duration::from_millis(20));
    };
    assert!(!children.trim().is_empty(), "the leader has no child");

    assert_eq!(process::scan("op-scan").unwrap(), Some(pid));
    assert_eq!(process::scan("op-nobody").unwrap(), None);
}

#[test]
fn an_observer_of_another_boot_or_namespace_sees_unknown_and_leaves_the_process_alone() {
    let mut scratch = Scratch::new("other");
    let pid = scratch.spawn("op-other", "sleep 30");
    let observer = identity::observer();
    let (recorded, _) = process::identity_of(pid, &observer).unwrap();
    let mut elsewhere = observer.clone();
    elsewhere.boot_id = Some("another-boot".to_owned());

    let verdict = identity::verdict(Some(&recorded), &elsewhere, &identity::probe(pid));

    assert_eq!(verdict, Verdict::OtherEnvironment);
    assert_eq!(observed(verdict, false), ExecutionState::Unknown);
    assert!(alive(pid));
}

#[test]
fn a_reused_pid_proves_the_recorded_process_gone_and_is_lost() {
    let mut scratch = Scratch::new("reused");
    let pid = scratch.spawn("op-reused", "sleep 30");
    let observer = identity::observer();
    let (mut recorded, _) = process::identity_of(pid, &observer).unwrap();
    recorded.start_time = "1".to_owned();

    let verdict = identity::verdict(Some(&recorded), &observer, &identity::probe(pid));

    assert_eq!(verdict, Verdict::PidReused);
    assert_eq!(observed(verdict, false), ExecutionState::Lost);
    assert!(alive(pid));
}

#[test]
fn a_spawn_the_os_refuses_starts_nothing() {
    let scratch = Scratch::new("refused");
    let argv = ["/nonexistent/adapter".to_owned()];

    let error = process::spawn(&Spec {
        argv: &argv,
        cwd: Path::new("/"),
        operation_id: "op-refused",
        dispatch_id: "dispatch-test",
        stdout: &scratch.dir.join("stdout.log"),
        stderr: &scratch.dir.join("stderr.log"),
    })
    .unwrap_err();

    assert_eq!(error.kind(), std::io::ErrorKind::NotFound);
    assert_eq!(process::scan("op-refused").unwrap(), None);
}
