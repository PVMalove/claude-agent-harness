//! `migrate` on a repository whose state the Python 1.x ledger wrote.

mod common;

use std::fs;

use serde_json::{Value, json};
use state::authority::{Epoch, epoch};
#[cfg(target_os = "linux")]
use state::canonical::canonical_json;
use state::canonical::sha256_hex;
use state::error::Error;
use state::legacy::{self, LegacyState};
use state::migrate::run;
use state::parity;
use state::platform;

use common::Repo;

fn read_json(path: &std::path::Path) -> Value {
    serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
}

fn code(result: &Result<state::migrate::Outcome, Error>) -> u8 {
    result.as_ref().map_or_else(Error::exit_code, |_| 0)
}

#[test]
fn migrate_cuts_the_state_over_to_the_store() {
    let repo = Repo::new("cutover");
    let before = repo.tree();

    let outcome = run(&repo.root).unwrap();

    assert!(!outcome.already_cut_over);
    let state = repo.state();
    let pointer = read_json(&state.join("ledger.json"));
    assert_eq!(pointer["version"], 4);
    assert_eq!(pointer["generation"], "generation-7a4a6d6e");
    assert_eq!(pointer.as_object().unwrap().len(), 3);
    let authority = read_json(&state.join("AUTHORITY.json"));
    assert_eq!(authority["authority"], "runtime-store");
    assert_eq!(authority["store_id"], outcome.authority.store_id.as_str());
    assert_eq!(
        authority["manifest_sha256"],
        outcome.authority.manifest_sha256.as_str()
    );
    assert!(state.join("runtime.db").is_file());
    assert!(!state.join("runtime.db.importing").exists());
    assert!(!state.join(".coordinator.lock").exists());
    assert!(matches!(epoch(&state), Epoch::Store(_)));
    // The legacy generations and audit are exactly what they were; only the pointer changed and
    // the store files were added.
    let after = repo.tree();
    for (path, bytes) in &before {
        if path != "ledger.json" {
            assert_eq!(after.get(path), Some(bytes), "{path}");
        }
    }
    let added: Vec<&String> = after
        .keys()
        .filter(|path| !before.contains_key(*path))
        .collect();
    assert!(
        added
            .iter()
            .all(|path| path.starts_with("runtime.db") || *path == "AUTHORITY.json"),
        "{added:?}"
    );
}

#[test]
fn the_snapshot_is_a_verified_copy_with_its_manifests() {
    let repo = Repo::new("snapshot");
    let before = repo.tree();

    let outcome = run(&repo.root).unwrap();

    let snapshot = repo.root.join(&outcome.authority.snapshot);
    assert!(snapshot.starts_with(repo.snapshots()));
    let manifest = read_json(&snapshot.join("snapshot-manifest.json"));
    let files = manifest["files"].as_array().unwrap();
    assert_eq!(files.len(), before.len());
    for entry in files {
        let path = entry["path"].as_str().unwrap();
        let copied = fs::read(snapshot.join("state").join(path)).unwrap();
        assert_eq!(Some(&copied), before.get(path), "{path}");
        assert_eq!(entry["sha256"], sha256_hex(&copied).as_str());
    }
    let import_manifest = fs::read_to_string(snapshot.join("import-manifest.json")).unwrap();
    assert_eq!(
        sha256_hex(import_manifest.as_bytes()),
        outcome.authority.manifest_sha256
    );
}

#[test]
fn the_store_has_the_parity_of_the_snapshot() {
    let repo = Repo::new("parity");
    let outcome = run(&repo.root).unwrap();
    let copy = repo.root.join(&outcome.authority.snapshot).join("state");
    let pointer = legacy::read_pointer(&copy).unwrap().unwrap();
    let legacy = LegacyState::read(&copy, pointer).unwrap();
    let live = repo.state().join("runtime.db");

    parity::check(&legacy, &live).unwrap();
}

#[test]
fn a_repeat_after_the_commit_point_reports_already_cut_over_and_changes_nothing() {
    let repo = Repo::new("repeat");
    let first = run(&repo.root).unwrap();
    let before = repo.tree();
    let snapshots = fs::read_dir(repo.snapshots()).unwrap().count();

    let second = run(&repo.root).unwrap();

    assert!(second.already_cut_over);
    assert_eq!(second.authority, first.authority);
    let after = repo.tree();
    assert_eq!(
        before.keys().collect::<Vec<_>>(),
        after
            .keys()
            .filter(|path| !path.starts_with("runtime.db-"))
            .collect::<Vec<_>>()
    );
    for (path, bytes) in &before {
        if path != "runtime.db" {
            assert_eq!(after.get(path), Some(bytes), "{path}");
        }
    }
    assert_eq!(fs::read_dir(repo.snapshots()).unwrap().count(), snapshots);
}

fn assert_refused_and_untouched(
    repo: &Repo,
    before: &std::collections::BTreeMap<String, Vec<u8>>,
    text: &str,
) {
    let result = run(&repo.root);
    assert_eq!(code(&result), 2, "{result:?}");
    let message = result.unwrap_err().to_string();
    assert!(message.contains(text), "{message}");
    assert_eq!(&repo.tree(), before);
    assert!(!repo.snapshots().exists());
}

#[test]
fn a_qa_lease_refuses_without_changing_anything() {
    let repo = Repo::new("lease");
    fs::write(repo.generation().join("qa-lane/lease.json"), "{}\n").unwrap();
    let before = repo.tree();

    assert_refused_and_untouched(&repo, &before, "lease");
}

#[test]
fn a_queued_qa_run_refuses_without_changing_anything() {
    let repo = Repo::new("queue");
    fs::write(repo.generation().join("qa-lane/queue/1-x.json"), "{}\n").unwrap();
    let before = repo.tree();

    assert_refused_and_untouched(&repo, &before, "queued");
}

#[test]
fn a_working_dispatch_refuses_without_changing_anything() {
    let repo = Repo::new("working");
    let status = repo
        .generation()
        .join("dispatch-status/dispatch-22222222-0000-4000-8000-000000000002.json");
    let text = fs::read_to_string(&status).unwrap();
    fs::write(&status, text.replace("\"approved\"", "\"working\"")).unwrap();
    let before = repo.tree();

    assert_refused_and_untouched(&repo, &before, "working");
}

#[test]
fn a_ledger_older_than_version_3_names_the_ledger_migrate_remedy() {
    let repo = Repo::new("old");
    let pointer = repo.state().join("ledger.json");
    let text = fs::read_to_string(&pointer).unwrap();
    fs::write(&pointer, text.replace("\"version\": 3", "\"version\": 2")).unwrap();
    let before = repo.tree();

    let result = run(&repo.root);

    match &result {
        Err(Error::Refused { remedy, .. }) => {
            assert!(remedy.contains("ledger migrate"), "{remedy}")
        }
        other => panic!("{other:?}"),
    }
    assert_eq!(repo.tree(), before);
}

#[test]
fn an_empty_state_has_nothing_to_migrate() {
    let repo = Repo::new("empty");
    fs::remove_dir_all(repo.state()).unwrap();
    fs::create_dir_all(repo.state()).unwrap();

    let result = run(&repo.root);

    assert_eq!(code(&result), 2);
    let error = result.unwrap_err();
    assert!(error.to_string().contains("nothing_to_migrate"));
    assert_eq!(error.code(), contracts::ErrorCode::NotFound);
    assert!(!repo.state().join(".coordinator.lock").exists());
}

#[test]
fn a_ledger_that_does_not_validate_refuses() {
    let repo = Repo::new("invalid");
    fs::remove_file(
        repo.generation()
            .join("plans/batch-aaaaaaaa-0000-4000-8000-000000000001.json"),
    )
    .unwrap();
    let before = repo.tree();

    assert_refused_and_untouched(&repo, &before, "immutable plan");
}

fn write_owner(repo: &Repo, owner: &Value) {
    let lock = repo.state().join(".coordinator.lock");
    fs::create_dir(&lock).unwrap();
    fs::write(lock.join("owner.json"), owner.to_string()).unwrap();
}

#[test]
fn a_live_lock_owner_is_never_removed() {
    let repo = Repo::new("alive");
    let environment = platform::environment();
    write_owner(
        &repo,
        &json!({
            "pid": std::process::id(),
            "host": platform::hostname(),
            "acquired_at": "2026-10-22T00:00:00+00:00",
            "pid_namespace": environment.pid_namespace,
            "boot_id": environment.boot_id,
        }),
    );
    let before = repo.tree();

    let result = run(&repo.root);

    assert_eq!(code(&result), 2, "{result:?}");
    assert!(result.unwrap_err().to_string().contains("locked"));
    assert_eq!(repo.tree(), before);
    assert!(
        !repo
            .state()
            .join("lock-audit")
            .read_dir()
            .unwrap()
            .any(|entry| {
                entry
                    .unwrap()
                    .file_name()
                    .to_string_lossy()
                    .contains("release")
            })
    );
}

#[test]
fn a_lock_of_another_host_or_boot_is_not_removed_either() {
    for owner in [
        json!({"pid": 999_999, "host": "another-host", "acquired_at": "2026-10-22T00:00:00+00:00"}),
        json!({"pid": 999_999, "host": platform::hostname(), "boot_id": "another-boot",
               "pid_namespace": platform::environment().pid_namespace, "acquired_at": "2026-10-22T00:00:00+00:00"}),
    ] {
        let repo = Repo::new("foreign");
        write_owner(&repo, &owner);
        let before = repo.tree();

        let result = run(&repo.root);

        assert_eq!(code(&result), 2, "{owner}: {result:?}");
        assert_eq!(repo.tree(), before);
    }
}

#[cfg(target_os = "linux")]
#[test]
fn a_dead_owner_is_removed_with_a_lock_audit_record_and_the_migration_goes_on() {
    let repo = Repo::new("dead");
    let mut child = std::process::Command::new("true").spawn().unwrap();
    let pid = child.id();
    child.wait().unwrap();
    let environment = platform::environment();
    write_owner(
        &repo,
        &json!({
            "pid": pid,
            "host": platform::hostname(),
            "acquired_at": "2026-10-22T00:00:00+00:00",
            "operation": "runtime-cutover",
            "pid_namespace": environment.pid_namespace,
            "boot_id": environment.boot_id,
        }),
    );

    let outcome = run(&repo.root).unwrap();

    assert!(!outcome.already_cut_over);
    let audit: Vec<Value> = fs::read_dir(repo.state().join("lock-audit"))
        .unwrap()
        .map(|entry| read_json(&entry.unwrap().path()))
        .collect();
    let release: Vec<&Value> = audit
        .iter()
        .filter(|record| record["action"] == "ledger-lock-owner-dead-release")
        .collect();
    assert_eq!(release.len(), 1);
    assert_eq!(release[0]["details"]["verdict"], "owner-dead");
    assert_eq!(release[0]["details"]["lock"]["owner"]["pid"], pid);
    // The record is a valid lock audit record: its hash is the hash of the rest of it.
    let mut body = release[0].clone();
    let checksum = body
        .as_object_mut()
        .unwrap()
        .remove("record_sha256")
        .unwrap();
    assert_eq!(
        checksum,
        sha256_hex(canonical_json(&body).as_bytes()).as_str()
    );
    // The record is part of the snapshot and of the import manifest.
    let snapshot = repo.root.join(&outcome.authority.snapshot);
    assert!(
        fs::read_to_string(snapshot.join("import-manifest.json"))
            .unwrap()
            .contains("lock-audit/")
    );
}

#[test]
fn what_an_interrupted_attempt_left_of_the_store_is_removed() {
    let repo = Repo::new("leftovers");
    let state = repo.state();
    fs::write(state.join("runtime.db.importing"), b"half").unwrap();
    fs::write(state.join("runtime.db"), b"renamed but never committed").unwrap();
    fs::write(state.join("AUTHORITY.json"), b"{}").unwrap();

    let outcome = run(&repo.root).unwrap();

    assert!(!outcome.already_cut_over);
    assert!(!state.join("runtime.db.importing").exists());
    assert!(matches!(epoch(&state), Epoch::Store(_)));
}

#[test]
fn a_store_kept_after_a_rollback_is_not_deleted() {
    let repo = Repo::new("rolled-back");
    let state = repo.state();
    fs::write(state.join("runtime.db"), b"kept for analysis").unwrap();
    fs::write(
        state.join("AUTHORITY.json"),
        "{\"authority\": \"file-ledger\"}\n",
    )
    .unwrap();

    let result = run(&repo.root);

    assert_eq!(code(&result), 2, "{result:?}");
    assert_eq!(
        fs::read(state.join("runtime.db")).unwrap(),
        b"kept for analysis"
    );
}

#[test]
fn a_cut_over_state_whose_store_does_not_match_is_an_invariant_failure() {
    let repo = Repo::new("inconsistent");
    run(&repo.root).unwrap();
    fs::write(repo.state().join("AUTHORITY.json"), "{\"authority\": \"runtime-store\", \"store_id\": \"x\", \"manifest_sha256\": \"y\", \"snapshot\": \"s\", \"cutover_at\": \"t\"}\n").unwrap();

    let result = run(&repo.root);

    assert_eq!(code(&result), 4, "{result:?}");
}

#[test]
fn the_pointer_after_the_cutover_is_refused_by_a_version_1_x_installation() {
    // A 1.x LifecycleLedger accepts only versions 1, 2 and 3 (ADR 0020, section 1).
    let repo = Repo::new("fence");
    run(&repo.root).unwrap();

    let pointer = read_json(&repo.state().join("ledger.json"));

    assert!(![1, 2, 3].contains(&pointer["version"].as_i64().unwrap()));
}
