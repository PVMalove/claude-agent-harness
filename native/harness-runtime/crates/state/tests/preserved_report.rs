//! A human-approved rewind may preserve an unaccepted blocked writer report (#837).

mod common;

use std::fs;
use std::path::Path;

use serde_json::{Value, json};
use state::canonical::object_sha256;
use state::migrate;

use common::Repo;

const FIRST: &str = "dispatch-11111111-0000-4000-8000-000000000001";
const BATCH: &str = "batch-aaaaaaaa-0000-4000-8000-000000000001";

fn read(path: &Path) -> Value {
    serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
}

fn write(path: &Path, value: &Value) {
    fs::write(path, serde_json::to_vec(value).unwrap()).unwrap();
}

fn seal(value: &mut Value) {
    value.as_object_mut().unwrap().remove("record_sha256");
    value["record_sha256"] = json!(object_sha256(value.as_object().unwrap()));
}

fn blocked_rewind() -> Repo {
    let repo = Repo::new("blocked-rewind");
    let generation = repo.generation();
    let batch_path = generation.join(format!("batches/{BATCH}.json"));
    let brief_path = generation.join(format!("dispatches/{FIRST}.json"));
    let report_path = generation.join(format!("reports/{FIRST}.json"));
    let commit = "4".repeat(40);

    let mut brief = read(&brief_path);
    brief["purpose"] = json!("work");
    brief["snapshot_commit"] = json!(commit);
    brief["write_paths"] = json!(["a/**"]);
    write(&brief_path, &brief);

    let mut report = read(&report_path);
    report["outcome"] = json!("blocked");
    report["commit_sha"] = json!(commit);
    write(&report_path, &report);

    let mut event = json!({
        "approved_at": "2026-10-24T19:27:59Z", "approved_by": "operator",
        "before_state": "blocked", "dispatch_id": null,
        "kind": "rewind", "next_action": "developer", "note": "Preserve blocked work",
        "sequence": 1, "snapshot_commit": null, "state": "awaiting-approval",
        "transition_digest": null,
        "evidence": {
            "superseded_dispatches": [FIRST], "retired_dispatches": [],
            "writer_start_commit": commit,
            "preserved_progress": {
                "checkout": {}, "files": {}, "head_commit": commit,
                "index_sha256": "5".repeat(64), "worktree_sha256": "6".repeat(64),
                "snapshot_commit": commit, "source_dispatch_id": FIRST
            }
        }
    });
    seal(&mut event);

    let mut batch = read(&batch_path);
    batch["dispatches"][0]["brief_sha256"] = json!(object_sha256(brief.as_object().unwrap()));
    batch["dispatches"][0]["report_sha256"] = json!(object_sha256(report.as_object().unwrap()));
    batch["manual_recovery"] = json!(true);
    batch["recovery_events"] = json!([event]);
    write(&batch_path, &batch);

    let mut audit = json!({
        "audit_id": "audit-00000000-0000-4000-8000-000000000001",
        "action": "replace-record", "at": "2026-10-24T19:27:59Z",
        "details": {
            "path": format!("batches/{BATCH}.json"),
            "decision": {
                "decision": "rewind", "dispatch_id": null,
                "approver": {"kind": "human", "name": "operator"},
                "approved_at": "2026-10-24T19:27:59Z",
                "evidence": {"recovery_event": event}
            }
        }
    });
    seal(&mut audit);
    write(&generation.join("audit/audit-blocked-rewind.json"), &audit);
    repo
}

fn edit_batch(repo: &Repo, edit: impl FnOnce(&mut Value)) {
    let path = repo.generation().join(format!("batches/{BATCH}.json"));
    let mut batch = read(&path);
    edit(&mut batch);
    seal(&mut batch["recovery_events"][0]);
    write(&path, &batch);
    let audit_path = repo.generation().join("audit/audit-blocked-rewind.json");
    let mut audit = read(&audit_path);
    audit["details"]["decision"]["evidence"]["recovery_event"] =
        batch["recovery_events"][0].clone();
    seal(&mut audit);
    write(&audit_path, &audit);
}

fn edit_report(repo: &Repo, edit: impl FnOnce(&mut Value)) {
    let path = repo.generation().join(format!("reports/{FIRST}.json"));
    let mut report = read(&path);
    edit(&mut report);
    write(&path, &report);
    edit_batch(repo, |batch| {
        batch["dispatches"][0]["report_sha256"] = json!(object_sha256(report.as_object().unwrap()));
    });
}

fn assert_refused_without_changes(repo: &Repo) {
    let before = repo.tree();

    let error = migrate::run(&repo.root).unwrap_err();

    assert_eq!(error.exit_code(), 2);
    assert!(
        error.to_string().contains("preserved writer evidence"),
        "{error}"
    );
    assert_eq!(repo.tree(), before);
    assert!(!repo.snapshots().exists());
}

#[test]
fn migrate_preserves_a_blocked_report_without_accepting_or_rewriting_it() {
    let repo = blocked_rewind();
    let before = repo.tree();

    let outcome = migrate::run(&repo.root).unwrap();

    assert!(!outcome.already_cut_over);
    assert_eq!(read(&repo.state().join("ledger.json"))["version"], 4);
    let after = repo.tree();
    for (path, bytes) in before {
        if path != "ledger.json" {
            assert_eq!(after[&path], bytes, "{path}");
        }
    }
}

#[test]
fn migrate_preserves_a_retired_blocked_writer() {
    let repo = blocked_rewind();
    edit_batch(&repo, |batch| {
        batch["dispatches"][0]["state"] = json!("abandoned");
        batch["recovery_events"][0]["evidence"]["retired_dispatches"] = json!([FIRST]);
    });

    assert!(migrate::run(&repo.root).is_ok());
}

#[test]
fn migrate_still_preserves_a_cancelled_writer_without_a_report() {
    let repo = blocked_rewind();
    edit_batch(&repo, |batch| {
        let entry = batch["dispatches"][0].as_object_mut().unwrap();
        entry.remove("report");
        entry.remove("report_sha256");
        entry.insert("state".into(), json!("cancelled"));
        entry.insert("cancellation".into(), json!({"runtime_stopped": true}));
    });

    assert!(migrate::run(&repo.root).is_ok());
}

#[test]
fn migrate_refuses_a_preserved_completed_or_failed_report() {
    for outcome in ["completed", "failed"] {
        let repo = blocked_rewind();
        edit_report(&repo, |report| report["outcome"] = json!(outcome));

        assert_refused_without_changes(&repo);
    }
}

#[test]
fn migrate_refuses_preserving_an_accepted_report() {
    for decision in ["accept", "accept-conditional", "override-warning"] {
        let repo = blocked_rewind();
        edit_batch(&repo, |batch| {
            batch["dispatches"][0]["decision"] = json!({"decision": decision});
        });

        assert_refused_without_changes(&repo);
    }
}

#[test]
fn migrate_refuses_preserved_head_that_differs_from_the_report() {
    let repo = blocked_rewind();
    edit_report(&repo, |report| report["commit_sha"] = json!("7".repeat(40)));

    assert_refused_without_changes(&repo);
}

#[test]
fn migrate_refuses_an_abandoned_writer_without_retirement_evidence() {
    let repo = blocked_rewind();
    edit_batch(&repo, |batch| {
        batch["dispatches"][0]["state"] = json!("abandoned")
    });

    assert_refused_without_changes(&repo);
}

#[test]
fn migrate_checks_the_hash_of_a_retired_blocked_report() {
    let repo = blocked_rewind();
    edit_batch(&repo, |batch| {
        batch["dispatches"][0]["state"] = json!("abandoned");
        batch["dispatches"][0]["report_sha256"] = json!("0".repeat(64));
        batch["recovery_events"][0]["evidence"]["retired_dispatches"] = json!([FIRST]);
    });

    assert_refused_without_changes(&repo);
}
