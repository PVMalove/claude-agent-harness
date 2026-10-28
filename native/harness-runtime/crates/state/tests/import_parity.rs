//! The import of a state the Python 1.x ledger wrote, and the parity check of the result.

mod common;

use std::fs;

use rusqlite::Connection;
use state::canonical::sha256_hex;
use state::error::Error;
use state::import::{import, manifest};
use state::parity;

use common::Scratch;

const AT: &str = "2026-10-22T07:10:19.972174+00:00";

fn imported(scratch: &Scratch) -> state::import::Imported {
    let legacy = scratch.read();
    legacy.validate().unwrap();
    import(&legacy, &scratch.db(), "store-under-test", AT).unwrap()
}

fn count(connection: &Connection, table: &str) -> i64 {
    connection
        .query_row(&format!("SELECT count(*) FROM {table}"), [], |row| {
            row.get(0)
        })
        .unwrap()
}

#[test]
fn a_state_written_by_python_validates() {
    let scratch = Scratch::new("validate");

    scratch.read().validate().unwrap();
}

#[test]
fn every_record_of_the_selected_generation_lands_in_one_table() {
    let scratch = Scratch::new("tables");
    imported(&scratch);

    let connection = Connection::open(scratch.db()).unwrap();

    assert_eq!(count(&connection, "batch"), 1);
    assert_eq!(count(&connection, "plan"), 1);
    assert_eq!(count(&connection, "dispatch"), 2);
    assert_eq!(count(&connection, "dispatch_status"), 2);
    assert_eq!(count(&connection, "risk_assessment"), 1);
    assert_eq!(count(&connection, "context_package"), 1);
    assert_eq!(count(&connection, "checkpoint"), 1);
    assert_eq!(count(&connection, "qa_lane"), 1);
    assert_eq!(count(&connection, "qa_queue"), 1);
    // The report, the integration report, the sequence and the recovery are JSON records the
    // store has no typed table for; the .md report and the QA log are bytes.
    assert_eq!(count(&connection, "record"), 4);
    assert_eq!(count(&connection, "artifact"), 2);
    let version: i64 = connection
        .pragma_query_value(None, "user_version", |row| row.get(0))
        .unwrap();
    assert_eq!(version, state::schema::SCHEMA_VERSION);
    let mode: String = connection
        .pragma_query_value(None, "journal_mode", |row| row.get(0))
        .unwrap();
    assert_eq!(mode, "wal");
}

#[test]
fn the_imported_store_has_the_parity_of_the_legacy_state() {
    let scratch = Scratch::new("parity");
    imported(&scratch);

    parity::check(&scratch.read(), &scratch.db()).unwrap();
}

#[test]
fn a_store_in_wal_mode_leaves_no_side_files_once_it_is_closed() {
    let scratch = Scratch::new("sidefiles");
    imported(&scratch);
    parity::check(&scratch.read(), &scratch.db()).unwrap();

    let mut names: Vec<String> = fs::read_dir(&scratch.base)
        .unwrap()
        .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    names.sort();

    assert_eq!(names, ["runtime.db.importing", "state"]);
}

#[test]
fn the_legacy_audit_is_imported_byte_for_byte_with_its_manifest() {
    let scratch = Scratch::new("audit");
    let result = imported(&scratch);
    let legacy = scratch.read();
    let connection = Connection::open(scratch.db()).unwrap();

    // 17 audit records of the selected generation, 1 of the old generation, 1 lock audit record.
    assert_eq!(count(&connection, "legacy_audit"), 19);
    assert_eq!(count(&connection, "import_manifest"), 19);
    let invalid: i64 = connection
        .query_row(
            "SELECT count(*) FROM legacy_audit WHERE record_sha256_valid != 1",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(invalid, 0);
    for (path, bytes) in legacy.audit_files() {
        let stored: Vec<u8> = connection
            .query_row(
                "SELECT bytes FROM legacy_audit WHERE path = ?1",
                [&path],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(stored, bytes, "{path}");
    }
    let (text, checksum) = manifest(&legacy);
    assert_eq!(result.manifest_json, text);
    assert_eq!(result.manifest_sha256, checksum);
    assert_eq!(checksum, sha256_hex(text.as_bytes()));
}

#[test]
fn the_first_audit_record_is_the_legacy_import_that_carries_the_manifest_hash() {
    let scratch = Scratch::new("genesis");
    let result = imported(&scratch);
    let connection = Connection::open(scratch.db()).unwrap();

    let (seq, action, details, prev): (i64, String, String, String) = connection
        .query_row(
            "SELECT seq, action, details_json, prev_chain_sha256 FROM audit ORDER BY seq LIMIT 1",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .unwrap();

    assert_eq!((seq, action.as_str()), (1, "legacy-import"));
    assert_eq!(prev, "0".repeat(64));
    let details: serde_json::Value = serde_json::from_str(&details).unwrap();
    assert_eq!(details["manifest_sha256"], result.manifest_sha256.as_str());
    assert_eq!(count(&connection, "audit"), 1);
}

#[test]
fn a_changed_row_fails_the_parity_with_code_4() {
    let scratch = Scratch::new("changed");
    imported(&scratch);
    {
        let connection = Connection::open(scratch.db()).unwrap();
        connection
            .execute("UPDATE dispatch SET state = 'failed'", [])
            .unwrap();
    }

    let error = parity::check(&scratch.read(), &scratch.db()).unwrap_err();

    assert!(matches!(error, Error::Invariant(_)), "{error}");
    assert_eq!(error.exit_code(), 4);
    assert!(
        error.to_string().contains("differs: generations/"),
        "{error}"
    );
}

#[test]
fn a_missing_row_and_an_extra_row_both_fail_the_parity() {
    let scratch = Scratch::new("missing");
    imported(&scratch);
    {
        let connection = Connection::open(scratch.db()).unwrap();
        connection.execute("DELETE FROM qa_queue", []).unwrap();
        connection
            .execute(
                "INSERT INTO record (path, collection, body_json) VALUES ('stray.json', '', '{}')",
                [],
            )
            .unwrap();
    }

    let message = parity::check(&scratch.read(), &scratch.db())
        .unwrap_err()
        .to_string();

    assert!(message.contains("missing in the store"), "{message}");
    assert!(message.contains("not in the legacy state"), "{message}");
}

#[test]
fn a_changed_artifact_byte_fails_the_parity() {
    let scratch = Scratch::new("artifact");
    imported(&scratch);
    {
        let connection = Connection::open(scratch.db()).unwrap();
        connection
            .execute(
                "UPDATE artifact SET bytes = x'00' WHERE path LIKE 'qa-artifacts/%'",
                [],
            )
            .unwrap();
    }

    assert!(parity::check(&scratch.read(), &scratch.db()).is_err());
}

#[test]
fn a_record_that_was_not_utf8_keeps_its_bytes() {
    let scratch = Scratch::new("bytes");
    imported(&scratch);
    let connection = Connection::open(scratch.db()).unwrap();

    let stored: Vec<u8> = connection
        .query_row(
            "SELECT bytes FROM artifact WHERE path LIKE 'qa-artifacts/%'",
            [],
            |row| row.get(0),
        )
        .unwrap();

    assert_eq!(stored, b"line one\n\xff\xfe not utf-8\n");
}

#[test]
fn the_files_of_another_generation_are_kept_for_reading_only() {
    let scratch = Scratch::new("other");
    imported(&scratch);
    let connection = Connection::open(scratch.db()).unwrap();

    let found: Vec<(String, String)> = connection
        .prepare("SELECT generation, path FROM legacy_generation ORDER BY path")
        .unwrap()
        .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))
        .unwrap()
        .map(Result::unwrap)
        .collect();

    assert_eq!(
        found,
        [(
            "generation-00000000".to_owned(),
            "batches/batch-old.json".to_owned()
        )]
    );
}

#[test]
fn a_tampered_audit_record_is_refused_before_anything_is_imported() {
    let scratch = Scratch::new("tamper");
    let audit = scratch.generation().join("audit");
    let victim = fs::read_dir(&audit)
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    let text = fs::read_to_string(&victim).unwrap();
    fs::write(&victim, text.replace("\"action\": \"", "\"action\": \"x")).unwrap();

    let error = scratch.read().validate().unwrap_err();

    assert_eq!(error.exit_code(), 2, "{error}");
    assert!(error.to_string().contains("integrity"), "{error}");
}

#[test]
fn a_dispatch_changed_after_its_brief_hash_was_recorded_is_refused() {
    let scratch = Scratch::new("brief");
    let dispatches = scratch.generation().join("dispatches");
    let victim = dispatches.join("dispatch-11111111-0000-4000-8000-000000000001.json");
    let text = fs::read_to_string(&victim).unwrap();
    fs::write(&victim, text.replace("\"developer\"", "\"qa\"")).unwrap();

    let error = scratch.read().validate().unwrap_err();

    assert_eq!(error.exit_code(), 2, "{error}");
    assert!(error.to_string().contains("brief"), "{error}");
}

#[test]
fn a_batch_that_breaks_the_batch_contract_is_refused_with_the_python_wording() {
    let scratch = Scratch::new("contract");
    let batch = scratch
        .generation()
        .join("batches/batch-aaaaaaaa-0000-4000-8000-000000000001.json");
    let text = fs::read_to_string(&batch).unwrap();
    fs::write(
        &batch,
        text.replacen("{\n", "{\n  \"needs_attention\": \"yes\",\n", 1),
    )
    .unwrap();

    let error = scratch.read().validate().unwrap_err();

    assert_eq!(error.exit_code(), 2, "{error}");
    assert!(
        error
            .to_string()
            .starts_with("batch needs_attention must be a boolean: batches/batch-"),
        "{error}"
    );
}

#[test]
fn an_orphaned_dispatch_is_refused() {
    let scratch = Scratch::new("orphan");
    let generation = scratch.generation();
    for directory in ["dispatches", "dispatch-status"] {
        fs::write(
            generation.join(directory).join("dispatch-orphan.json"),
            "{\"dispatch_id\": \"dispatch-orphan\", \"batch_id\": \"x\", \"state\": \"approved\"}\n",
        )
        .unwrap();
    }

    let error = scratch.read().validate().unwrap_err();

    assert!(error.to_string().contains("orphaned"), "{error}");
}

#[test]
fn the_legacy_files_are_not_changed_by_the_import() {
    let scratch = Scratch::new("readonly");
    let before = scratch.read();
    imported(&scratch);
    let after = scratch.read();

    assert_eq!(before.files, after.files);
    assert_eq!(before.lock_audit, after.lock_audit);
    assert_eq!(before.other_generations, after.other_generations);
}
