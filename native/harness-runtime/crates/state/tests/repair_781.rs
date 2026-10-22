//! The #781 repair at import: a batch that owes a risk reassessment but names no candidate gets
//! `risk_reassessment_pending_next = true`, and the repair is audited (ADR 0020, section 5).

mod common;

use std::fs;

use rusqlite::Connection;
use serde_json::{Value, json};
use state::canonical::{canonical_json, sha256_hex};
use state::import::import;
use state::parity;

use common::Scratch;

const AT: &str = "2026-10-22T07:10:19.972174+00:00";
const BATCH: &str = "batches/batch-aaaaaaaa-0000-4000-8000-000000000001.json";

/// The fixture batch with `edit` applied to its JSON object.
fn edit_batch(scratch: &Scratch, edit: impl FnOnce(&mut serde_json::Map<String, Value>)) -> String {
    let path = scratch.generation().join(BATCH);
    let before = fs::read_to_string(&path).unwrap();
    let Value::Object(mut batch) = serde_json::from_str(&before).unwrap() else {
        panic!("a batch is an object")
    };
    edit(&mut batch);
    fs::write(&path, canonical_json(&Value::Object(batch))).unwrap();
    before
}

fn broken(batch: &mut serde_json::Map<String, Value>) {
    batch.remove("risk_reassessment_candidate");
    batch.remove("risk_reassessment_triggers");
}

fn run(scratch: &Scratch) -> Connection {
    let legacy = scratch.read();
    legacy.validate().unwrap();
    import(&legacy, &scratch.db(), "store-under-test", AT).unwrap();
    parity::check(&legacy, &scratch.db()).unwrap();
    Connection::open(scratch.db()).unwrap()
}

#[test]
fn a_batch_that_owes_a_reassessment_without_a_candidate_becomes_pending() {
    let scratch = Scratch::new("repair");
    edit_batch(&scratch, broken);

    let connection = run(&scratch);

    let (required, candidate, pending): (i64, Option<String>, i64) = connection
        .query_row(
            "SELECT risk_reassessment_required, risk_reassessment_candidate, \
             risk_reassessment_pending_next FROM batch",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .unwrap();
    assert_eq!((required, candidate, pending), (1, None, 1));
}

#[test]
fn the_repair_is_audited_with_the_hashes_before_and_after_and_the_rule() {
    let scratch = Scratch::new("audit");
    edit_batch(&scratch, broken);
    let before_record: Value =
        serde_json::from_str(&fs::read_to_string(scratch.generation().join(BATCH)).unwrap())
            .unwrap();
    let mut after_record = before_record.clone();
    after_record["risk_reassessment_pending_next"] = json!(true);
    let generation = scratch.read().pointer.generation;

    let connection = run(&scratch);

    let records: Vec<(i64, String, String)> = connection
        .prepare("SELECT seq, action, details_json FROM audit ORDER BY seq")
        .unwrap()
        .query_map([], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))
        .unwrap()
        .map(Result::unwrap)
        .collect();
    assert_eq!(records.len(), 2);
    assert_eq!((records[0].0, records[0].1.as_str()), (1, "legacy-import"));
    assert_eq!(
        (records[1].0, records[1].1.as_str()),
        (2, "migration-repair")
    );
    let details: Value = serde_json::from_str(&records[1].2).unwrap();
    assert_eq!(details["rule"], "781-risk-reassessment-pending-next");
    assert_eq!(details["path"], format!("generations/{generation}/{BATCH}"));
    assert_eq!(
        details["before_sha256"],
        sha256_hex(canonical_json(&before_record).as_bytes()).as_str()
    );
    assert_eq!(
        details["after_sha256"],
        sha256_hex(canonical_json(&after_record).as_bytes()).as_str()
    );
}

#[test]
fn the_legacy_batch_file_is_not_rewritten_by_the_repair() {
    let scratch = Scratch::new("untouched");
    edit_batch(&scratch, broken);
    let path = scratch.generation().join(BATCH);
    let before = fs::read(&path).unwrap();

    run(&scratch);

    assert_eq!(fs::read(&path).unwrap(), before);
}

#[test]
fn a_batch_with_a_candidate_or_a_pending_marker_is_not_repaired() {
    let scratch = Scratch::new("healthy");
    let connection = run(&scratch);
    let repairs: i64 = connection
        .query_row(
            "SELECT count(*) FROM audit WHERE action = 'migration-repair'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(repairs, 0);

    let pending = Scratch::new("already-pending");
    edit_batch(&pending, |batch| {
        broken(batch);
        batch.insert("risk_reassessment_pending_next".to_owned(), json!(true));
    });
    let connection = run(&pending);
    let repairs: i64 = connection
        .query_row(
            "SELECT count(*) FROM audit WHERE action = 'migration-repair'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(repairs, 0);
}

#[test]
fn the_store_refuses_a_batch_that_owes_a_reassessment_without_a_candidate_or_a_marker() {
    let scratch = Scratch::new("check");
    let connection = run(&scratch);

    let unmarked = connection.execute("UPDATE batch SET risk_reassessment_candidate = NULL", []);
    let emptied = connection.execute("UPDATE batch SET risk_reassessment_candidate = ''", []);

    assert!(unmarked.is_err());
    assert!(emptied.is_err());
    connection
        .execute(
            "UPDATE batch SET risk_reassessment_candidate = NULL, risk_reassessment_pending_next = 1",
            [],
        )
        .unwrap();
}

#[test]
fn a_difference_outside_the_repair_still_fails_the_parity() {
    let scratch = Scratch::new("outside");
    edit_batch(&scratch, broken);
    let connection = run(&scratch);
    connection
        .execute("UPDATE batch SET state = 'failed'", [])
        .unwrap();

    let error = parity::check(&scratch.read(), &scratch.db()).unwrap_err();

    assert_eq!(error.exit_code(), 4, "{error}");
}

#[test]
fn a_repaired_row_that_lost_its_marker_fails_the_parity() {
    let scratch = Scratch::new("lost-marker");
    edit_batch(&scratch, broken);
    let connection = run(&scratch);
    // The marker cannot be dropped, and the candidate cannot be invented without the parity noticing.
    connection
        .execute(
            "UPDATE batch SET risk_reassessment_candidate = 'invented'",
            [],
        )
        .unwrap();

    assert_eq!(
        parity::check(&scratch.read(), &scratch.db())
            .unwrap_err()
            .exit_code(),
        4
    );
}
