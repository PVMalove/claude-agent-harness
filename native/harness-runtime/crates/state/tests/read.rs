//! The read operation of the cut-over store: the records come back as the legacy files had them.

mod common;

use std::fs;

use serde_json::{Value, json};
use state::migrate::run;
use state::read::{ReadError, read, store_id};

use common::Repo;

fn migrated(name: &str) -> (Repo, state::migrate::Outcome) {
    let repo = Repo::new(name);
    let outcome = run(&repo.root).unwrap();
    (repo, outcome)
}

fn legacy_record(repo: &Repo, outcome: &state::migrate::Outcome, path: &str) -> Value {
    let copy = repo.root.join(&outcome.authority.snapshot).join("state");
    let generation = state::legacy::read_pointer(&copy)
        .unwrap()
        .unwrap()
        .generation;
    let bytes = fs::read(copy.join("generations").join(generation).join(path)).unwrap();
    serde_json::from_slice(&bytes).unwrap()
}

#[test]
fn a_collection_answers_every_record_of_its_directory_as_the_legacy_file_had_it() {
    let (repo, outcome) = migrated("collection");

    let answer = read(
        &repo.state(),
        Some(&json!({"collections": ["batches", "dispatches", "dispatch-status", "checkpoints", "qa-lane", "reports"]})),
        None,
    )
    .unwrap();

    let mut paths: Vec<&String> = answer.records.keys().collect();
    paths.sort();
    assert_eq!(
        paths,
        [
            "batches/batch-aaaaaaaa-0000-4000-8000-000000000001.json",
            "checkpoints/checkpoint-0001.json",
            "dispatch-status/dispatch-11111111-0000-4000-8000-000000000001.json",
            "dispatch-status/dispatch-22222222-0000-4000-8000-000000000002.json",
            "dispatches/dispatch-11111111-0000-4000-8000-000000000001.json",
            "dispatches/dispatch-22222222-0000-4000-8000-000000000002.json",
            "qa-lane/recoveries/r1.json",
            "qa-lane/sequence.json",
            "reports/dispatch-11111111-0000-4000-8000-000000000001.json",
            "reports/integration/int-1.json",
        ]
    );
    for (path, record) in &answer.records {
        assert_eq!(record, &legacy_record(&repo, &outcome, path), "{path}");
    }
}

#[test]
fn the_audit_collection_answers_the_legacy_audit_records_of_the_selected_generation() {
    let (repo, outcome) = migrated("audit");

    let answer = read(
        &repo.state(),
        Some(&json!({"collections": ["audit"]})),
        None,
    )
    .unwrap();

    assert_eq!(answer.records.len(), 17);
    for (path, record) in &answer.records {
        assert!(path.starts_with("audit/audit-"), "{path}");
        assert_eq!(record, &legacy_record(&repo, &outcome, path), "{path}");
    }
}

#[test]
fn a_named_path_answers_its_record_and_a_missing_path_is_left_out() {
    let (repo, outcome) = migrated("paths");
    let present = "plans/batch-aaaaaaaa-0000-4000-8000-000000000001.json";

    let answer = read(
        &repo.state(),
        Some(&json!({"paths": [present, "plans/nobody.json", "qa-lane/lease.json", "reports/integration/int-1.json"]})),
        None,
    )
    .unwrap();

    let mut paths: Vec<&String> = answer.records.keys().collect();
    paths.sort();
    assert_eq!(paths, [present, "reports/integration/int-1.json"]);
    assert_eq!(
        answer.records[present],
        legacy_record(&repo, &outcome, present)
    );
}

#[test]
fn the_numbers_of_a_record_keep_the_text_they_had() {
    let (repo, _) = migrated("numbers");

    let answer = read(
        &repo.state(),
        Some(&json!({"paths": ["dispatches/dispatch-11111111-0000-4000-8000-000000000001.json"]})),
        None,
    )
    .unwrap();

    let text = serde_json::to_string(&answer.records).unwrap();
    assert!(text.contains("1e-05"), "{text}");
    assert!(text.contains("1180591620717411303424"), "{text}");
}

#[test]
fn the_revision_is_the_last_audit_sequence_and_a_stale_expectation_conflicts() {
    let (repo, _) = migrated("revision");

    let answer = read(&repo.state(), None, None).unwrap();
    let stale = read(&repo.state(), None, Some(answer.revision + 1));

    assert_eq!(answer.revision, 1);
    assert!(answer.records.is_empty());
    assert_eq!(
        stale.unwrap_err(),
        ReadError::RevisionConflict {
            expected: 2,
            actual: 1
        }
    );
    assert!(read(&repo.state(), None, Some(1)).is_ok());
}

#[test]
fn a_request_the_protocol_does_not_define_is_refused() {
    let (repo, _) = migrated("malformed");

    for payload in [
        json!({"surprise": 1}),
        json!({"paths": "batches/x.json"}),
        json!({"paths": [1]}),
        json!({"paths": ["../ledger.json"]}),
        json!({"paths": ["/etc/passwd"]}),
        json!({"paths": ["a//b.json"]}),
        json!(["batches"]),
    ] {
        let result = read(&repo.state(), Some(&payload), None);
        assert!(
            matches!(result, Err(ReadError::Malformed(_))),
            "{payload}: {result:?}"
        );
    }
}

#[test]
fn before_the_cutover_there_is_no_store_to_read() {
    let repo = Repo::new("before");

    let result = read(
        &repo.state(),
        Some(&json!({"collections": ["batches"]})),
        None,
    );

    assert!(
        matches!(result, Err(ReadError::NotAttached(_))),
        "{result:?}"
    );
    assert_eq!(store_id(&repo.state()), None);
}

#[test]
fn the_store_id_of_a_cut_over_state_is_the_one_migrate_reported() {
    let (repo, outcome) = migrated("store-id");

    assert_eq!(store_id(&repo.state()), Some(outcome.authority.store_id));
}
