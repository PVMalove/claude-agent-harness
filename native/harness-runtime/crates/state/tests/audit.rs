//! The audit chain of the cut-over store: the checkpoint, the ordinary verification of the tail
//! and the full verification of `state.validate` (ADR 0020, section 4). The tests play the
//! attacker: the runtime is not running, and they change the store file with SQLite directly.

mod common;

use contracts::{ErrorBody, ErrorCode, PROTOCOL_VERSION, Request};
use rusqlite::Connection;
use serde_json::{Value, json};
use state::audit::{GENESIS_CHAIN, chain_sha256, record_sha256};
use state::canonical::canonical_json;
use state::migrate::run;
use state::read::{ReadError, read};
use state::validate::validate;
use state::write::write;

use common::Repo;

const BATCH: &str = "batch-aaaaaaaa-0000-4000-8000-000000000001";

fn cut_over(name: &str) -> Repo {
    let repo = Repo::new(name);
    run(&repo.root).unwrap();
    repo
}

fn store(repo: &Repo) -> Connection {
    Connection::open(repo.state().join("runtime.db")).unwrap()
}

fn request(id: &str, mutations: Value) -> Request {
    Request {
        protocol_version: PROTOCOL_VERSION,
        operation_id: id.to_owned(),
        operation: "state.write".to_owned(),
        request_deadline: u64::MAX,
        batch_id: None,
        dispatch_id: None,
        candidate_sha: None,
        plan_digest: None,
        expected_revision: None,
        fencing_token: None,
        payload: Some(json!({"mutations": mutations})),
    }
}

/// A report record written through the runtime: one audit record, and the checkpoint moves.
fn add_report(repo: &Repo, name: &str) {
    write(
        &repo.state(),
        &request(
            &format!("op-{name}"),
            json!([{"op": "create", "path": format!("reports/{name}.json"),
                    "content": canonical_json(&json!({"name": name}))}]),
        ),
    )
    .unwrap();
}

fn replace_batch(repo: &Repo, id: &str, ticket: &str) {
    let answer = read(
        &repo.state(),
        Some(&json!({"paths": [format!("batches/{BATCH}.json")]})),
        None,
    )
    .unwrap();
    let path = format!("batches/{BATCH}.json");
    let mut record = answer.records[&path].clone();
    record["ticket"] = json!(ticket);
    write(
        &repo.state(),
        &request(
            id,
            json!([{"op": "replace", "path": path, "content": canonical_json(&record),
                    "expected_revision": answer.revisions[&path]}]),
        ),
    )
    .unwrap();
}

fn full(repo: &Repo) -> Result<Value, ErrorBody> {
    validate(&repo.state(), true)
}

fn tail(repo: &Repo) -> i64 {
    store(repo)
        .query_row("SELECT MAX(seq) FROM audit", [], |row| row.get(0))
        .unwrap()
}

fn checkpoint(repo: &Repo) -> Option<(i64, String)> {
    store(repo)
        .query_row(
            "SELECT seq, chain_sha256 FROM audit_checkpoint",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .ok()
}

fn broken_check(error: &ErrorBody) -> (&str, Option<i64>) {
    assert_eq!(error.code, ErrorCode::InvariantViolation, "{error:?}");
    assert!(error.remedy.contains("ledger validate --full"));
    (
        error.facts["check"].as_str().unwrap(),
        error.facts.get("seq").and_then(Value::as_i64),
    )
}

fn read_ok(repo: &Repo) -> Result<(), ReadError> {
    read(
        &repo.state(),
        Some(&json!({"collections": ["batches"]})),
        None,
    )
    .map(|_| ())
}

#[test]
fn a_fresh_store_and_a_store_after_writes_pass_the_full_verification() {
    let repo = cut_over("fresh");
    assert_eq!(full(&repo).unwrap()["mode"], "full");

    add_report(&repo, "a");
    replace_batch(&repo, "op-b", "#2");

    let result = full(&repo).unwrap();
    assert_eq!(result["valid"], true);
    assert_eq!(result["audit_records"], tail(&repo));
}

#[test]
fn a_write_moves_the_checkpoint_to_the_new_tail() {
    let repo = cut_over("moves");
    assert_eq!(checkpoint(&repo), None);

    add_report(&repo, "a");

    let (seq, chain) = checkpoint(&repo).unwrap();
    assert_eq!(seq, tail(&repo));
    let stored: String = store(&repo)
        .query_row(
            "SELECT chain_sha256 FROM audit WHERE seq = ?1",
            [seq],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(chain, stored);
}

#[test]
fn a_record_tampered_with_before_the_checkpoint_is_found_by_the_full_check_only() {
    let repo = cut_over("before");
    add_report(&repo, "a");
    add_report(&repo, "b");
    store(&repo)
        .execute(
            "UPDATE audit SET details_json = '{\"path\":\"reports/x.json\",\"sha256\":\"0\"}' WHERE seq = 2",
            [],
        )
        .unwrap();

    assert!(read_ok(&repo).is_ok());
    assert!(validate(&repo.state(), false).is_ok());
    let error = full(&repo).unwrap_err();

    assert_eq!(broken_check(&error), ("audit_record", Some(2)));
}

#[test]
fn a_record_tampered_with_after_the_checkpoint_is_found_by_the_ordinary_check() {
    let repo = cut_over("after");
    add_report(&repo, "a");
    let connection = store(&repo);
    let seq = tail(&repo) + 1;
    let (prev, _) = checkpoint(&repo).unwrap();
    let prev_chain: String = connection
        .query_row(
            "SELECT chain_sha256 FROM audit WHERE seq = ?1",
            [prev],
            |row| row.get(0),
        )
        .unwrap();
    // A forged record that is consistent in itself, added after the checkpoint ...
    let details = json!({"path": "reports/forged.json", "sha256": "0"});
    let at = "2026-10-01T00:00:00+00:00";
    let record = record_sha256(at, "immutable-record", &details);
    let chain = chain_sha256(&prev_chain, &record);
    connection
        .execute(
            "INSERT INTO audit VALUES (?1, ?2, 'immutable-record', ?3, ?4, ?5, ?6)",
            rusqlite::params![seq, at, details.to_string(), record, prev_chain, chain],
        )
        .unwrap();
    assert!(
        read_ok(&repo).is_ok(),
        "a consistent record passes the chain check"
    );
    // ... and changed afterwards.
    connection
        .execute(
            "UPDATE audit SET details_json = '{\"path\":\"x\"}' WHERE seq = ?1",
            [seq],
        )
        .unwrap();

    let read_error = read_ok(&repo).unwrap_err();
    let write_error = write(
        &repo.state(),
        &request(
            "op-x",
            json!([{"op": "create", "path": "reports/y.json",
        "content": canonical_json(&json!({}))}]),
        ),
    )
    .unwrap_err();
    let validate_error = validate(&repo.state(), false).unwrap_err();

    let ReadError::Broken(broken) = read_error else {
        panic!("{read_error:?}")
    };
    assert_eq!((broken.check, broken.seq), ("audit_record", Some(seq)));
    assert_eq!(broken_check(&write_error), ("audit_record", Some(seq)));
    assert_eq!(broken_check(&validate_error), ("audit_record", Some(seq)));
}

#[test]
fn a_deleted_or_damaged_checkpoint_forces_a_full_check_and_is_rebuilt_without_an_error() {
    let repo = cut_over("rebuilt");
    add_report(&repo, "a");

    store(&repo)
        .execute("DELETE FROM audit_checkpoint", [])
        .unwrap();
    assert!(read_ok(&repo).is_ok());
    assert!(validate(&repo.state(), false).is_ok());
    assert_eq!(
        checkpoint(&repo).unwrap().0,
        tail(&repo),
        "validate rebuilds it"
    );

    store(&repo)
        .execute("UPDATE audit_checkpoint SET chain_sha256 = 'damaged'", [])
        .unwrap();
    assert!(read_ok(&repo).is_ok());
    add_report(&repo, "b");
    let (seq, chain) = checkpoint(&repo).unwrap();
    assert_eq!(seq, tail(&repo));
    assert_ne!(chain, "damaged", "the next write rebuilds it");
}

#[test]
fn the_full_check_does_not_use_a_checkpoint_that_covers_a_damaged_record() {
    let repo = cut_over("covers");
    add_report(&repo, "a");
    store(&repo)
        .execute(
            "UPDATE audit SET record_sha256 = 'forged' WHERE seq = 1",
            [],
        )
        .unwrap();

    let error = full(&repo).unwrap_err();

    assert_eq!(broken_check(&error), ("audit_record", Some(1)));
    assert!(
        read_ok(&repo).is_ok(),
        "the checkpoint covers the record for an ordinary command"
    );
}

#[test]
fn a_deleted_record_is_found() {
    let repo = cut_over("deleted");
    add_report(&repo, "a");
    add_report(&repo, "b");
    store(&repo)
        .execute("DELETE FROM audit WHERE seq = 2", [])
        .unwrap();

    let error = full(&repo).unwrap_err();

    assert_eq!(broken_check(&error), ("audit_sequence", Some(3)));
}

#[test]
fn an_inserted_record_with_a_recomputed_hash_is_found() {
    let repo = cut_over("inserted");
    add_report(&repo, "a");
    add_report(&repo, "b");
    let connection = store(&repo);
    connection
        .execute_batch("UPDATE audit SET seq = -seq WHERE seq >= 2; UPDATE audit SET seq = 1 - seq WHERE seq < 0;")
        .unwrap();
    let details = json!({"path": "reports/forged.json", "sha256": "0"});
    let at = "2026-10-01T00:00:00+00:00";
    let (prev,): (String,) = connection
        .query_row("SELECT chain_sha256 FROM audit WHERE seq = 1", [], |row| {
            Ok((row.get(0)?,))
        })
        .unwrap();
    let record = record_sha256(at, "immutable-record", &details);
    connection
        .execute(
            "INSERT INTO audit VALUES (2, ?1, 'immutable-record', ?2, ?3, ?4, ?5)",
            rusqlite::params![
                at,
                details.to_string(),
                record,
                prev,
                chain_sha256(&prev, &record)
            ],
        )
        .unwrap();

    let error = full(&repo).unwrap_err();

    assert_eq!(broken_check(&error), ("audit_chain", Some(3)));
}

#[test]
fn reordered_records_are_found() {
    let repo = cut_over("reordered");
    add_report(&repo, "a");
    add_report(&repo, "b");
    store(&repo)
        .execute_batch("UPDATE audit SET seq = 0 WHERE seq = 2; UPDATE audit SET seq = 2 WHERE seq = 3; UPDATE audit SET seq = 3 WHERE seq = 0;")
        .unwrap();

    let error = full(&repo).unwrap_err();

    assert_eq!(broken_check(&error), ("audit_chain", Some(2)));
}

#[test]
fn an_action_outside_the_vocabulary_stops_every_ordinary_command_after_the_checkpoint() {
    let repo = cut_over("vocabulary");
    add_report(&repo, "a");
    let connection = store(&repo);
    let (seq, chain) = checkpoint(&repo).unwrap();
    let details = json!({});
    let at = "2026-10-01T00:00:00+00:00";
    let record = record_sha256(at, "rewrite", &details);
    connection
        .execute(
            "INSERT INTO audit VALUES (?1, ?2, 'rewrite', '{}', ?3, ?4, ?5)",
            rusqlite::params![seq + 1, at, record, chain, chain_sha256(&chain, &record)],
        )
        .unwrap();

    let ReadError::Broken(broken) = read_ok(&repo).unwrap_err() else {
        panic!("a broken chain")
    };

    assert_eq!(broken.check, "audit_action");
}

#[test]
fn a_row_changed_outside_the_runtime_is_found_by_the_last_audit_record_of_its_path() {
    let repo = cut_over("row");
    replace_batch(&repo, "op-1", "#2");
    store(&repo)
        .execute(
            "UPDATE batch SET body_json = json_set(body_json, '$.ticket', '#9') WHERE id = ?1",
            [BATCH],
        )
        .unwrap();

    assert!(read_ok(&repo).is_ok());
    let error = full(&repo).unwrap_err();

    assert_eq!(broken_check(&error).0, "row");
}

#[test]
fn a_transition_that_does_not_continue_the_record_before_it_is_found() {
    let repo = cut_over("link");
    replace_batch(&repo, "op-1", "#2");
    replace_batch(&repo, "op-2", "#3");
    let connection = store(&repo);
    let seq = tail(&repo);
    let (prev, details): (String, String) = connection
        .query_row(
            "SELECT prev_chain_sha256, details_json FROM audit WHERE seq = ?1",
            [seq],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    let mut details: Value = serde_json::from_str(&details).unwrap();
    details["before_sha256"] = json!("not-the-hash-before");
    let at = "2026-10-01T00:00:00+00:00";
    let record = record_sha256(at, "transition", &details);
    connection
        .execute(
            "UPDATE audit SET at = ?1, details_json = ?2, record_sha256 = ?3, chain_sha256 = ?4 WHERE seq = ?5",
            rusqlite::params![at, details.to_string(), record, chain_sha256(&prev, &record), seq],
        )
        .unwrap();

    let error = full(&repo).unwrap_err();

    assert_eq!(broken_check(&error), ("audit_link", Some(seq)));
}

#[test]
fn the_legacy_audit_the_import_stored_cannot_change() {
    let repo = cut_over("legacy-audit");
    store(&repo)
        .execute(
            "UPDATE legacy_audit SET bytes = CAST('{}' AS BLOB) WHERE path = (SELECT MIN(path) FROM legacy_audit)",
            [],
        )
        .unwrap();

    let error = full(&repo).unwrap_err();

    assert_eq!(broken_check(&error).0, "legacy_audit");
}

#[test]
fn the_manifest_of_the_import_cannot_change() {
    let repo = cut_over("manifest");
    store(&repo)
        .execute(
            "UPDATE import_manifest SET sha256 = '0' WHERE path = (SELECT MIN(path) FROM import_manifest)",
            [],
        )
        .unwrap();

    let error = full(&repo).unwrap_err();

    assert_eq!(broken_check(&error).0, "import_manifest");
}

#[test]
fn a_row_that_breaks_a_check_constraint_is_found_by_the_integrity_check() {
    let repo = cut_over("integrity");
    let connection = store(&repo);
    connection
        .execute_batch("PRAGMA ignore_check_constraints = ON;")
        .unwrap();
    connection
        .execute(
            "UPDATE batch SET risk_reassessment_candidate = NULL, risk_reassessment_pending_next = NULL WHERE id = ?1",
            [BATCH],
        )
        .unwrap();

    let error = full(&repo).unwrap_err();

    assert_eq!(broken_check(&error).0, "integrity_check");
}

#[test]
fn a_failed_full_check_writes_nothing() {
    let repo = cut_over("writes-nothing");
    add_report(&repo, "a");
    store(&repo)
        .execute(
            "UPDATE audit SET record_sha256 = 'forged' WHERE seq = 1",
            [],
        )
        .unwrap();
    store(&repo)
        .execute("DELETE FROM audit_checkpoint", [])
        .unwrap();
    let before = checkpoint(&repo);

    assert!(full(&repo).is_err());

    assert_eq!(checkpoint(&repo), before);
}

#[test]
fn the_genesis_of_the_chain_is_sixty_four_zeros() {
    assert_eq!(GENESIS_CHAIN.len(), 64);
    let repo = cut_over("genesis");
    let prev: String = store(&repo)
        .query_row(
            "SELECT prev_chain_sha256 FROM audit WHERE seq = 1",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(prev, GENESIS_CHAIN);
}
