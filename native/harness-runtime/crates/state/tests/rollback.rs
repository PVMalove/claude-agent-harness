//! `rollback --to-legacy` (ADR 0020, section 5): the human approval, the preconditions of the
//! store, the archive and the new generation of format v3, the commit point and the repeats.

mod common;

use std::collections::BTreeMap;
use std::fs;

use contracts::ErrorCode;
use rusqlite::Connection;
use serde_json::{Value, json};
use state::canonical::{canonical_json, sha256_hex};
use state::error::Error;
use state::legacy::{LegacyState, read_pointer};
use state::rollback::{Approval, Outcome, run};

use common::Repo;

fn approval() -> Approval {
    Approval {
        approved_by: "human:operator".to_owned(),
        approved_at: "2026-10-27T10:00:00+00:00".to_owned(),
        reason: "return to 1.x".to_owned(),
    }
}

fn cut_over(name: &str) -> Repo {
    let repo = Repo::new(name);
    state::migrate::run(&repo.root).unwrap();
    repo
}

fn store(repo: &Repo) -> Connection {
    Connection::open(repo.state().join("runtime.db")).unwrap()
}

fn json_file(repo: &Repo, name: &str) -> Value {
    serde_json::from_slice(&fs::read(repo.state().join(name)).unwrap()).unwrap()
}

fn tree(repo: &Repo) -> BTreeMap<String, Vec<u8>> {
    repo.tree()
}

fn refused_code(error: &Error) -> ErrorCode {
    assert_eq!(error.exit_code(), 2, "{error:?}");
    error.code()
}

fn rolled_back(repo: &Repo) -> Outcome {
    run(&repo.root, &approval()).unwrap()
}

#[test]
fn the_rollback_writes_a_v3_generation_that_the_legacy_validation_accepts() {
    let repo = cut_over("success");
    let store_id = json_file(&repo, "AUTHORITY.json")["store_id"]
        .as_str()
        .unwrap()
        .to_owned();

    let outcome = rolled_back(&repo);

    assert!(!outcome.already_rolled_back);
    assert_eq!(outcome.store_id, store_id);
    let pointer = read_pointer(&repo.state()).unwrap().unwrap();
    assert_eq!(pointer.version, 3);
    assert_eq!(pointer.generation, outcome.generation);
    LegacyState::read(&repo.state(), pointer)
        .unwrap()
        .validate()
        .unwrap();
    let authority = json_file(&repo, "AUTHORITY.json");
    assert_eq!(authority["authority"], "file-ledger");
    assert_eq!(authority["rolled_back_from"], store_id.as_str());
    assert_eq!(authority["generation"], outcome.generation.as_str());
    assert_eq!(authority["export_sha256"], outcome.export_sha256.as_str());
}

#[test]
fn the_archive_is_kept_and_its_hash_is_in_the_audit_record_of_the_store() {
    let repo = cut_over("archive");

    let outcome = rolled_back(&repo);

    assert_eq!(
        outcome.export,
        format!("runtime-export/{}.tar", outcome.store_id)
    );
    let archive = repo.state().join(&outcome.export);
    assert_eq!(
        sha256_hex(&fs::read(&archive).unwrap()),
        outcome.export_sha256
    );
    assert!(fs::metadata(&archive).unwrap().permissions().readonly());
    let (action, details): (String, String) = store(&repo)
        .query_row(
            "SELECT action, details_json FROM audit ORDER BY seq DESC LIMIT 1",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    let details: Value = serde_json::from_str(&details).unwrap();
    assert_eq!(action, "runtime-rollback");
    assert_eq!(details["export_sha256"], outcome.export_sha256.as_str());
    assert_eq!(details["generation"], outcome.generation.as_str());
    assert_eq!(details["approved_by"], "human:operator");
}

#[test]
fn the_store_is_kept_read_only_and_its_schema_version_is_not_lowered() {
    let repo = cut_over("read-only");

    rolled_back(&repo);

    let path = repo.state().join("runtime.db");
    assert!(fs::metadata(&path).unwrap().permissions().readonly());
    let version: i64 = store(&repo)
        .pragma_query_value(None, "user_version", |row| row.get(0))
        .unwrap();
    assert_eq!(version, state::schema::SCHEMA_VERSION);
    assert!(!repo.state().join("runtime.db-wal").exists());
}

#[test]
fn a_dispatch_sent_or_working_without_a_live_execution_is_exported_as_it_is() {
    let repo = cut_over("stopped");
    let connection = store(&repo);
    let ids: Vec<String> = connection
        .prepare("SELECT id FROM dispatch_status ORDER BY id LIMIT 2")
        .unwrap()
        .query_map([], |row| row.get(0))
        .unwrap()
        .collect::<Result<_, _>>()
        .unwrap();
    connection
        .execute(
            "UPDATE dispatch_status SET state = 'dispatched', execution_state = NULL WHERE id = ?1",
            [&ids[0]],
        )
        .unwrap();
    connection
        .execute(
            "UPDATE dispatch_status SET state = 'working', execution_state = 'exited' WHERE id = ?1",
            [&ids[1]],
        )
        .unwrap();
    drop(connection);

    let outcome = rolled_back(&repo);

    let generation = repo.state().join("generations").join(&outcome.generation);
    for (id, expected) in ids.iter().zip(["dispatched", "working"]) {
        let status: Value = serde_json::from_slice(
            &fs::read(generation.join(format!("dispatch-status/{id}.json"))).unwrap(),
        )
        .unwrap();
        assert_eq!(status["state"], expected);
    }
}

fn assert_refused_without_a_change(repo: &Repo, approval: &Approval, code: ErrorCode) {
    let before = tree(repo);

    let error = run(&repo.root, approval).unwrap_err();

    assert_eq!(refused_code(&error), code, "{error}");
    assert_eq!(tree(repo), before, "the state directory changed");
}

#[test]
fn an_approval_that_is_blank_or_no_time_is_refused_without_a_change() {
    let repo = cut_over("approval");
    for change in [
        |a: &mut Approval| a.approved_by = " ".to_owned(),
        |a: &mut Approval| a.approved_at = String::new(),
        |a: &mut Approval| a.reason = String::new(),
        |a: &mut Approval| a.approved_at = "yesterday".to_owned(),
        |a: &mut Approval| a.approved_at = "2999-01-01T00:00:00+00:00".to_owned(),
    ] {
        let mut asked = approval();
        change(&mut asked);
        assert_refused_without_a_change(&repo, &asked, ErrorCode::MalformedRequest);
    }
}

#[test]
fn a_policy_approver_is_refused_without_a_change() {
    let repo = cut_over("policy");
    let mut asked = approval();
    asked.approved_by = "policy:auto".to_owned();

    assert_refused_without_a_change(&repo, &asked, ErrorCode::PolicyDenied);
}

#[test]
fn a_qa_lease_or_a_queued_run_in_the_store_is_refused_without_a_change() {
    for (name, sql) in [
        (
            "lease",
            "INSERT INTO qa_lane (id, body_json, owner_kind, owner_id, acquired_at, expires_at,
                                  heartbeat_at)
             VALUES (1, '{}', 'dispatch', 'd', 't', 't', 't')",
        ),
        (
            "queue",
            "INSERT INTO qa_queue (id, body_json) VALUES ('q-1', '{}')",
        ),
    ] {
        let repo = cut_over(name);
        store(&repo).execute(sql, []).unwrap();

        assert_refused_without_a_change(&repo, &approval(), ErrorCode::LeaseHeld);
    }
}

/// The lane row outlives its leases (schema version 3): a row without an owner is a free lane,
/// and its fencing token goes into the archive.
#[test]
fn a_free_qa_lane_row_is_no_lease_and_its_fencing_token_is_archived() {
    let repo = cut_over("free-lane");
    store(&repo)
        .execute(
            "INSERT INTO qa_lane (id, revision, fencing_token, body_json) VALUES (1, 4, 7, '{}')",
            [],
        )
        .unwrap();

    let outcome = rolled_back(&repo);

    assert!(!outcome.already_rolled_back);
    let pointer = read_pointer(&repo.state()).unwrap().unwrap();
    assert!(
        !repo
            .state()
            .join("generations")
            .join(&pointer.generation)
            .join("qa-lane/lease.json")
            .exists()
    );
}

#[test]
fn an_execution_that_may_still_run_is_refused_without_a_change() {
    for execution in ["launching", "running", "stopping", "unknown"] {
        let repo = cut_over(execution);
        store(&repo)
            .execute(
                "UPDATE dispatch_status SET state = 'working', execution_state = ?1
                 WHERE id = (SELECT id FROM dispatch_status ORDER BY id LIMIT 1)",
                [execution],
            )
            .unwrap();

        assert_refused_without_a_change(&repo, &approval(), ErrorCode::LeaseHeld);
    }
}

#[test]
fn a_store_of_a_newer_schema_is_refused_without_a_write() {
    let repo = cut_over("newer");
    store(&repo)
        .pragma_update(None, "user_version", state::schema::SCHEMA_VERSION + 1)
        .unwrap();

    assert_refused_without_a_change(&repo, &approval(), ErrorCode::ProtocolUnsupported);
}

#[test]
fn a_state_that_was_never_cut_over_has_nothing_to_roll_back() {
    let repo = Repo::new("never");

    assert_refused_without_a_change(&repo, &approval(), ErrorCode::NotFound);
}

#[test]
fn a_repeat_after_the_commit_point_answers_already_rolled_back_and_changes_nothing() {
    let repo = cut_over("repeat");
    let first = rolled_back(&repo);
    let before = tree(&repo);

    let second = rolled_back(&repo);

    assert!(second.already_rolled_back);
    assert_eq!(second.generation, first.generation);
    assert_eq!(second.export_sha256, first.export_sha256);
    assert_eq!(tree(&repo), before);
}

#[test]
fn a_repeat_after_a_crash_between_the_authority_and_the_pointer_replaces_the_pointer_only() {
    let repo = cut_over("crash-pointer");
    let cut_over_pointer = fs::read(repo.state().join("ledger.json")).unwrap();
    let first = rolled_back(&repo);
    // The state a crash after step 8 leaves: AUTHORITY.json names the file ledger, and the
    // pointer still names version 4.
    fs::write(repo.state().join("ledger.json"), &cut_over_pointer).unwrap();
    let authority = fs::read(repo.state().join("AUTHORITY.json")).unwrap();

    let second = rolled_back(&repo);

    assert!(!second.already_rolled_back);
    assert_eq!(second.generation, first.generation);
    assert_eq!(
        fs::read(repo.state().join("AUTHORITY.json")).unwrap(),
        authority
    );
    let pointer = read_pointer(&repo.state()).unwrap().unwrap();
    assert_eq!(
        (pointer.version, pointer.generation),
        (3, first.generation.clone())
    );
    assert_eq!(pointer.selected_at, first.rolled_back_at);
}

#[test]
fn a_repeat_after_a_crash_behind_the_audit_record_continues_with_its_generation() {
    let repo = cut_over("crash-audit");
    let pointer = fs::read(repo.state().join("ledger.json")).unwrap();
    let authority = fs::read(repo.state().join("AUTHORITY.json")).unwrap();
    let first = rolled_back(&repo);
    // The state a crash after step 7 leaves: the store has the record and is read-only, and the
    // two files still name the store.
    fs::write(repo.state().join("ledger.json"), &pointer).unwrap();
    fs::write(repo.state().join("AUTHORITY.json"), &authority).unwrap();

    let second = rolled_back(&repo);

    assert!(!second.already_rolled_back);
    assert_eq!(second.generation, first.generation);
    assert_eq!(second.export_sha256, first.export_sha256);
    let records: i64 = store(&repo)
        .query_row(
            "SELECT COUNT(*) FROM audit WHERE action = 'runtime-rollback'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(records, 1);
    assert_eq!(
        json_file(&repo, "AUTHORITY.json")["authority"],
        "file-ledger"
    );
    let generations = fs::read_dir(repo.state().join("generations"))
        .unwrap()
        .count();
    assert_eq!(generations, 3, "the old two and the one the rollback wrote");
}

#[test]
fn the_outcome_names_what_the_rollback_did() {
    let repo = cut_over("outcome");

    let outcome = rolled_back(&repo);

    assert_eq!(
        canonical_json(&outcome.to_json()),
        canonical_json(&json!({
            "rolled_back": true,
            "already_rolled_back": false,
            "store_id": outcome.store_id,
            "generation": outcome.generation,
            "export": outcome.export,
            "export_sha256": outcome.export_sha256,
            "rolled_back_at": outcome.rolled_back_at,
        }))
    );
}
