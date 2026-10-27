//! The export of the store into a new legacy generation of format v3 and the archive of what
//! format v3 cannot hold (ADR 0020, section 5, rollback).

mod common;

use std::fs;

use rusqlite::Connection;
use serde_json::json;
use state::error::Error;
use state::export::{self, Generation};
use state::legacy::{LegacyState, Pointer, audit_record_is_valid};
use state::migrate::run;
use state::parity::check_export;

use common::Repo;

fn cut_over(name: &str) -> Repo {
    let repo = Repo::new(name);
    run(&repo.root).unwrap();
    repo
}

fn store(repo: &Repo) -> Connection {
    Connection::open(repo.state().join("runtime.db")).unwrap()
}

fn details() -> serde_json::Value {
    json!({"approved_by": "human:operator", "reason": "test"})
}

fn exported(repo: &Repo) -> Generation {
    export::write_generation(
        &store(repo),
        &repo.state(),
        "2026-10-27T00:00:00+00:00",
        &details(),
    )
    .unwrap()
}

fn read_generation(repo: &Repo, generation: &Generation) -> LegacyState {
    LegacyState::read(
        &repo.state(),
        Pointer {
            version: 3,
            generation: generation.name.clone(),
            selected_at: String::new(),
        },
    )
    .unwrap()
}

#[test]
fn the_exported_generation_passes_the_legacy_validation_and_the_parity_check() {
    let repo = cut_over("export-valid");

    let generation = exported(&repo);

    assert!(generation.name.starts_with("generation-"));
    read_generation(&repo, &generation).validate().unwrap();
    check_export(&store(&repo), &repo.state(), &generation).unwrap();
}

#[test]
fn the_generation_holds_every_record_directory_and_one_rollback_audit_record() {
    let repo = cut_over("export-dirs");

    let generation = exported(&repo);

    let legacy = read_generation(&repo, &generation);
    for directory in state::legacy::RECORD_DIRECTORIES {
        assert!(legacy.directories.contains(directory), "{directory}");
    }
    let file = legacy.file(&generation.audit_file).unwrap();
    assert!(audit_record_is_valid(&file.bytes));
    let record: serde_json::Value = serde_json::from_slice(&file.bytes).unwrap();
    assert_eq!(record["action"], "runtime-rollback");
    assert_eq!(record["details"], details());
    // The legacy audit of the imported generation is copied byte for byte.
    let imported = repo.generation();
    for entry in fs::read_dir(imported.join("audit")).unwrap() {
        let entry = entry.unwrap();
        let name = format!("audit/{}", entry.file_name().to_string_lossy());
        assert_eq!(
            legacy.file(&name).unwrap().bytes,
            fs::read(entry.path()).unwrap()
        );
    }
}

#[test]
fn a_row_that_differs_from_the_generation_fails_the_parity_check() {
    let repo = cut_over("export-differs");
    let generation = exported(&repo);
    let batch = fs::read_dir(
        repo.state()
            .join("generations")
            .join(&generation.name)
            .join("batches"),
    )
    .unwrap()
    .next()
    .unwrap()
    .unwrap()
    .path();
    let mut record: serde_json::Value = serde_json::from_slice(&fs::read(&batch).unwrap()).unwrap();
    record["ticket"] = json!("#changed");
    fs::write(&batch, state::canonical::canonical_json(&record)).unwrap();

    let error = check_export(&store(&repo), &repo.state(), &generation).unwrap_err();

    assert!(matches!(error, Error::Invariant(_)), "{error:?}");
    assert!(error.to_string().contains("differs"), "{error}");
}

#[test]
fn a_file_the_store_does_not_hold_fails_the_parity_check() {
    let repo = cut_over("export-extra");
    let generation = exported(&repo);
    fs::write(
        repo.state()
            .join("generations")
            .join(&generation.name)
            .join("reports/stray.json"),
        "{}\n",
    )
    .unwrap();

    let error = check_export(&store(&repo), &repo.state(), &generation).unwrap_err();

    assert!(error.to_string().contains("reports/stray.json"), "{error}");
}

#[test]
fn the_archive_bytes_are_the_same_on_a_repeat() {
    let repo = cut_over("export-archive");
    let connection = store(&repo);

    let first = export::archive(&connection).unwrap();
    let second = export::archive(&connection).unwrap();

    assert_eq!(first, second);
    let names = state::archive::names(&first);
    assert_eq!(
        names,
        [
            "MANIFEST.json",
            "audit.jsonl",
            "operations.jsonl",
            "revisions.jsonl",
            "store_meta.json"
        ]
    );
}

#[test]
fn the_archive_file_is_written_once_and_read_only() {
    let repo = cut_over("export-file");
    let bytes = export::archive(&store(&repo)).unwrap();

    let written = export::write_archive(&repo.state(), "store-1", &bytes).unwrap();
    let repeated = export::write_archive(&repo.state(), "store-1", &bytes).unwrap();

    assert_eq!(written, repeated);
    assert_eq!(written.path, "runtime-export/store-1.tar");
    assert_eq!(written.sha256, state::canonical::sha256_hex(&bytes));
    let file = repo.state().join(&written.path);
    assert_eq!(fs::read(&file).unwrap(), bytes);
    assert!(fs::metadata(&file).unwrap().permissions().readonly());
    let error = export::write_archive(&repo.state(), "store-1", b"other").unwrap_err();
    assert!(matches!(error, Error::Invariant(_)), "{error:?}");
    assert_eq!(error.exit_code(), 4);
}
