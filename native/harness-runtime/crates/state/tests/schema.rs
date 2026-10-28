//! The schema of the store, version 3: what `create` writes, what its CHECKs refuse, and how a
//! store of version 1 or 2 is brought forward (ADR 0020, sections 3, 4 and 7; ADR 0021, sections
//! 4 and 6).

use std::collections::BTreeMap;

use contracts::{ExecutionState, OperationState};
use rusqlite::Connection;
use state::schema::{SCHEMA, SCHEMA_VERSION, create, upgrade};

const SCHEMA_V1: &str = include_str!("fixtures/schema_v1.sql");
const SCHEMA_V2: &str = include_str!("fixtures/schema_v2.sql");

fn version(connection: &Connection) -> i64 {
    connection
        .pragma_query_value(None, "user_version", |row| row.get(0))
        .unwrap()
}

/// Every table and its columns with their types, NOT NULL flags and defaults.
fn columns(connection: &Connection) -> BTreeMap<String, Vec<String>> {
    let mut tables = BTreeMap::new();
    let mut names = connection
        .prepare("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        .unwrap();
    let names: Vec<String> = names
        .query_map([], |row| row.get(0))
        .unwrap()
        .map(Result::unwrap)
        .collect();
    for name in names {
        let mut info = connection
            .prepare(&format!(
                "SELECT name, type, \"notnull\", dflt_value, pk FROM pragma_table_info('{name}')"
            ))
            .unwrap();
        let mut found: Vec<String> = info
            .query_map([], |row| {
                Ok(format!(
                    "{}:{}:{}:{:?}:{}",
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, i64>(2)?,
                    row.get::<_, Option<String>>(3)?,
                    row.get::<_, i64>(4)?
                ))
            })
            .unwrap()
            .map(Result::unwrap)
            .collect();
        found.sort();
        tables.insert(name, found);
    }
    tables
}

fn fresh() -> Connection {
    let directory = tempdir("fresh");
    create(&directory.join("runtime.db")).unwrap()
}

fn tempdir(name: &str) -> std::path::PathBuf {
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let n = NEXT.fetch_add(1, Ordering::Relaxed);
    let path = std::env::temp_dir().join(format!("hr-schema-{name}-{}-{n}", std::process::id()));
    let _ = std::fs::remove_dir_all(&path);
    std::fs::create_dir_all(&path).unwrap();
    path
}

fn insert_dispatch(connection: &Connection) {
    connection
        .execute_batch(
            "INSERT INTO batch (id, state, body_json) VALUES ('b', 'active', '{}');
             INSERT INTO dispatch (id, batch_id, state, body_json) VALUES ('d', 'b', 'working', '{}');
             INSERT INTO dispatch_status (id, state, body_json) VALUES ('d', 'working', '{}');",
        )
        .unwrap();
}

fn is_check_violation(error: &rusqlite::Error) -> bool {
    matches!(
        error,
        rusqlite::Error::SqliteFailure(failure, _)
            if failure.extended_code == rusqlite::ffi::SQLITE_CONSTRAINT_CHECK
    )
}

#[test]
fn a_new_store_has_the_schema_version_three_and_its_generation_row() {
    let connection = fresh();

    assert_eq!(SCHEMA_VERSION, 3);
    assert_eq!(version(&connection), 3);
    let generation: i64 = connection
        .query_row("SELECT revision FROM generation WHERE id = 1", [], |row| {
            row.get(0)
        })
        .unwrap();
    assert_eq!(generation, 1);
}

#[test]
fn the_owner_columns_of_the_qa_lane_are_all_set_or_all_null() {
    let connection = fresh();
    for broken in [
        "INSERT INTO qa_lane (id, body_json, owner_id) VALUES (1, '{}', 'd')",
        "INSERT INTO qa_lane (id, body_json, owner_kind, owner_id, acquired_at, expires_at)
         VALUES (1, '{}', 'dispatch', 'd', 't', 't')",
        "INSERT INTO qa_lane (id, body_json, host, pid, start_time) VALUES (1, '{}', 'h', 1, 's')",
        "INSERT INTO qa_lane (id, body_json, owner_kind, owner_id, acquired_at, expires_at,
                              heartbeat_at, host, pid)
         VALUES (1, '{}', 'dispatch', 'd', 't', 't', 't', 'h', 1)",
    ] {
        let error = connection.execute(broken, []).unwrap_err();
        assert!(is_check_violation(&error), "{broken}: {error}");
    }
    connection
        .execute(
            "INSERT INTO qa_lane (id, body_json, owner_kind, owner_id, acquired_at, expires_at,
                                  heartbeat_at, host, pid, start_time)
             VALUES (1, '{}', 'dispatch', 'd', 't', 't', 't', 'h', 1, 's')",
            [],
        )
        .unwrap();
}

#[test]
fn the_rows_a_command_changes_carry_a_revision_that_starts_at_one() {
    let connection = fresh();
    insert_dispatch(&connection);
    connection
        .execute_batch(
            "INSERT INTO qa_queue (id, body_json) VALUES ('q', '{}');
             INSERT INTO record (path, collection, body_json) VALUES ('qa-lane/x.json', 'qa-lane', '{}');",
        )
        .unwrap();

    for (table, key) in [
        ("batch", "id"),
        ("dispatch", "id"),
        ("dispatch_status", "id"),
        ("qa_queue", "id"),
        ("record", "path"),
    ] {
        let revision: i64 = connection
            .query_row(
                &format!("SELECT revision FROM {table} LIMIT 1"),
                [],
                |row| row.get(0),
            )
            .unwrap_or_else(|err| panic!("{table}.{key}: {err}"));
        assert_eq!(revision, 1, "{table}");
    }
}

#[test]
fn a_terminal_dispatch_state_while_the_execution_may_run_is_refused() {
    let connection = fresh();
    insert_dispatch(&connection);
    for execution in ["unknown", "launching", "running"] {
        connection
            .execute(
                "UPDATE dispatch_status SET execution_state = ?1, state = 'working' WHERE id = 'd'",
                [execution],
            )
            .unwrap();
        for terminal in ["reported", "failed", "blocked", "cancelled", "abandoned"] {
            let error = connection
                .execute(
                    "UPDATE dispatch_status SET state = ?1 WHERE id = 'd'",
                    [terminal],
                )
                .unwrap_err();
            assert!(
                is_check_violation(&error),
                "{execution} {terminal}: {error}"
            );
        }
    }
}

#[test]
fn a_terminal_state_is_allowed_once_the_execution_has_ended_and_a_live_state_always() {
    let connection = fresh();
    insert_dispatch(&connection);
    for ended in ["exited", "lost", "stopping"] {
        connection
            .execute(
                "UPDATE dispatch_status SET execution_state = ?1, state = 'reported' WHERE id = 'd'",
                [ended],
            )
            .unwrap();
    }
    connection
        .execute(
            "UPDATE dispatch_status SET execution_state = NULL, state = 'reported' WHERE id = 'd'",
            [],
        )
        .unwrap();
    connection
        .execute(
            "UPDATE dispatch_status SET execution_state = 'running', state = 'working' WHERE id = 'd'",
            [],
        )
        .unwrap();
}

#[test]
fn an_execution_state_outside_the_vocabulary_is_refused() {
    let connection = fresh();
    insert_dispatch(&connection);

    let error = connection
        .execute(
            "UPDATE dispatch_status SET execution_state = 'Running' WHERE id = 'd'",
            [],
        )
        .unwrap_err();

    assert!(is_check_violation(&error), "{error}");
}

#[test]
fn an_operation_state_outside_the_vocabulary_is_refused() {
    let connection = fresh();
    for accepted in ["intent", "committed", "failed"] {
        connection
            .execute(
                "INSERT INTO operations (operation_id, operation, request_digest, state, created_at)
                 VALUES (?1, 'state.write', 'd', ?1, 't')",
                [accepted],
            )
            .unwrap();
    }

    let error = connection
        .execute(
            "INSERT INTO operations (operation_id, operation, request_digest, state, created_at)
             VALUES ('x', 'state.write', 'd', 'done', 't')",
            [],
        )
        .unwrap_err();

    assert!(is_check_violation(&error), "{error}");
}

#[test]
fn the_audit_checkpoint_is_one_row() {
    let connection = fresh();
    connection
        .execute(
            "INSERT INTO audit_checkpoint (id, seq, chain_sha256) VALUES (1, 1, 'c')",
            [],
        )
        .unwrap();

    let error = connection
        .execute(
            "INSERT INTO audit_checkpoint (id, seq, chain_sha256) VALUES (2, 2, 'c')",
            [],
        )
        .unwrap_err();

    assert!(is_check_violation(&error), "{error}");
}

#[test]
fn the_check_lists_of_the_schema_are_the_closed_vocabularies() {
    for state in OperationState::ALL {
        assert!(
            SCHEMA.contains(&format!("'{}'", state.as_str())),
            "{state:?}"
        );
    }
    for state in ExecutionState::ALL {
        assert!(
            SCHEMA.contains(&format!("'{}'", state.as_str())),
            "{state:?}"
        );
    }
}

fn store_of_version_one() -> (Connection, std::path::PathBuf) {
    let path = tempdir("v1").join("runtime.db");
    let connection = Connection::open(&path).unwrap();
    connection
        .execute_batch("PRAGMA synchronous = FULL; PRAGMA foreign_keys = ON;")
        .unwrap();
    connection.execute_batch(SCHEMA_V1).unwrap();
    connection.pragma_update(None, "user_version", 1).unwrap();
    connection
        .execute_batch(
            "INSERT INTO batch (id, state, body_json) VALUES ('b', 'active', '{\"a\":1}');
             INSERT INTO dispatch (id, batch_id, state, body_json) VALUES ('d', 'b', 'working', '{}');
             INSERT INTO dispatch_status (id, state, body_json) VALUES ('d', 'working', '{\"s\":1}');
             INSERT INTO qa_queue (id, body_json) VALUES ('q', '{}');
             INSERT INTO record (path, collection, body_json) VALUES ('reports/r.json', 'reports', '{}');",
        )
        .unwrap();
    state::audit::append(
        &connection,
        "2026-10-01T00:00:00+00:00",
        "legacy-import",
        &serde_json::json!({}),
    )
    .unwrap();
    (connection, path)
}

#[test]
fn a_store_of_version_one_is_upgraded_to_the_schema_a_new_store_has() {
    let (mut old, _) = store_of_version_one();

    let changed = upgrade(&mut old).unwrap();

    assert!(changed);
    assert_eq!(version(&old), 3);
    assert_eq!(columns(&old), columns(&fresh()));
}

#[test]
fn the_upgrade_keeps_every_row_and_appends_one_audit_record() {
    let (mut old, _) = store_of_version_one();

    upgrade(&mut old).unwrap();

    let body: String = old
        .query_row(
            "SELECT body_json FROM dispatch_status WHERE id = 'd'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(body, "{\"s\":1}");
    let revision: i64 = old
        .query_row(
            "SELECT revision FROM record WHERE path = 'reports/r.json'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(revision, 1);
    let last: (i64, String, String) = old
        .query_row(
            "SELECT seq, action, details_json FROM audit ORDER BY seq DESC LIMIT 1",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .unwrap();
    assert_eq!(last.0, 2);
    assert_eq!(last.1, "schema-migrate");
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&last.2).unwrap(),
        serde_json::json!({"from": 1, "to": 3})
    );
}

/// A store of version 2, with a held QA lease when `lease` names its body.
fn store_of_version_two(lease: Option<&str>) -> Connection {
    let path = tempdir("v2").join("runtime.db");
    let connection = Connection::open(&path).unwrap();
    connection
        .execute_batch("PRAGMA synchronous = FULL; PRAGMA foreign_keys = ON;")
        .unwrap();
    connection.execute_batch(SCHEMA_V2).unwrap();
    connection.pragma_update(None, "user_version", 2).unwrap();
    insert_dispatch(&connection);
    if let Some(body) = lease {
        connection
            .execute(
                "INSERT INTO qa_lane (id, revision, fencing_token, body_json) VALUES (1, 3, 0, ?1)",
                [body],
            )
            .unwrap();
    }
    state::audit::append(
        &connection,
        "2026-10-01T00:00:00+00:00",
        "legacy-import",
        &serde_json::json!({}),
    )
    .unwrap();
    connection
}

#[test]
fn a_store_of_version_two_is_upgraded_to_the_schema_a_new_store_has() {
    let mut old = store_of_version_two(None);

    assert!(upgrade(&mut old).unwrap());

    assert_eq!(version(&old), 3);
    assert_eq!(columns(&old), columns(&fresh()));
    let generation: i64 = old
        .query_row("SELECT revision FROM generation WHERE id = 1", [], |row| {
            row.get(0)
        })
        .unwrap();
    assert_eq!(generation, 1);
    let (action, details): (String, String) = old
        .query_row(
            "SELECT action, details_json FROM audit ORDER BY seq DESC LIMIT 1",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    assert_eq!(action, "schema-migrate");
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&details).unwrap(),
        serde_json::json!({"from": 2, "to": 3})
    );
}

#[test]
fn a_lease_of_version_two_keeps_its_body_owner_and_expiry_and_has_no_identity() {
    let body = r#"{"acquired_at":"2026-10-01T00:00:00+00:00","dispatch_id":"d","expires_at":"2026-10-01T00:30:00+00:00","host":"h","pid":7}"#;
    let mut old = store_of_version_two(Some(body));

    upgrade(&mut old).unwrap();

    type Lane = (
        i64,
        String,
        String,
        String,
        String,
        String,
        String,
        Option<i64>,
    );
    let lane: Lane = old
        .query_row(
            "SELECT revision, body_json, owner_kind, owner_id, acquired_at, expires_at,
                    heartbeat_at, pid
             FROM qa_lane WHERE id = 1",
            [],
            |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                    row.get(5)?,
                    row.get(6)?,
                    row.get(7)?,
                ))
            },
        )
        .unwrap();
    assert_eq!(
        lane,
        (
            3,
            body.to_owned(),
            "dispatch".to_owned(),
            "d".to_owned(),
            "2026-10-01T00:00:00+00:00".to_owned(),
            "2026-10-01T00:30:00+00:00".to_owned(),
            "2026-10-01T00:00:00+00:00".to_owned(),
            None,
        )
    );
}

#[test]
fn a_store_that_is_current_is_left_alone() {
    let mut current = fresh();

    assert!(!upgrade(&mut current).unwrap());

    let records: i64 = current
        .query_row("SELECT COUNT(*) FROM audit", [], |row| row.get(0))
        .unwrap();
    assert_eq!(records, 0);
}

#[test]
fn a_store_of_a_newer_version_is_refused_without_a_write() {
    let (mut newer, _) = store_of_version_one();
    newer.pragma_update(None, "user_version", 4).unwrap();

    let error = upgrade(&mut newer).unwrap_err();

    assert_eq!(error.exit_code(), 2);
    assert_eq!(error.code(), contracts::ErrorCode::ProtocolUnsupported);
    assert_eq!(version(&newer), 4);
    let records: i64 = newer
        .query_row("SELECT COUNT(*) FROM audit", [], |row| row.get(0))
        .unwrap();
    assert_eq!(records, 1);
}
