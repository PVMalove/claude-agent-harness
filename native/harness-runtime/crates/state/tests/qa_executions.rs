//! Schema version 6: an execution is a `process`, a `container` or a `qa` run of an engine; a QA
//! dispatch has one execution per attempt, and the latest attempt is the one a dispatch shows
//! (ADR 0022, section 3; ADR 0023, section 5).

use contracts::ExecutionState;
use rusqlite::Connection;
use serde_json::json;
use state::executions::{self, Change, KIND_QA, NewExecution};
use state::schema::{SCHEMA_VERSION, create, upgrade};

fn tempdir(name: &str) -> std::path::PathBuf {
    let path = std::env::temp_dir().join(format!("hr-qa-executions-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&path);
    std::fs::create_dir_all(&path).unwrap();
    path
}

const SEED: &str = "
    INSERT INTO batch (id, state, body_json) VALUES ('b', 'active', '{}');
    INSERT INTO dispatch (id, batch_id, state, body_json) VALUES ('d', 'b', 'working', '{}');
    INSERT INTO dispatch_status (id, state, body_json) VALUES ('d', 'working', '{}');";

fn store(name: &str) -> Connection {
    let connection = create(&tempdir(name).join("runtime.db")).unwrap();
    connection.execute_batch(SEED).unwrap();
    state::audit::append(
        &connection,
        "2026-10-01T00:00:00+00:00",
        "legacy-import",
        &json!({}),
    )
    .unwrap();
    connection
}

fn qa(
    connection: &Connection,
    id: &str,
    attempt: i64,
    engine: Option<&str>,
) -> Result<executions::Execution, String> {
    executions::insert(
        connection,
        &NewExecution {
            id,
            dispatch_id: "d",
            attempt,
            kind: KIND_QA,
            engine,
            launch: &json!({"groups": []}),
            execution_deadline: "2026-10-01T04:00:00+00:00",
            stop_grace_seconds: 30,
            poll_interval_seconds: 1,
            lease_owner: &json!({"pid": 7}),
        },
        "2026-10-01T00:00:00+00:00",
    )
    .map_err(|err| err.message)
}

#[test]
fn a_qa_attempt_names_its_engine_and_runs_only_with_its_receipt() {
    let connection = store("receipt");
    assert!(
        qa(&connection, "op-0", 1, None).is_err(),
        "a QA run needs its engine"
    );
    let intent = qa(&connection, "op-1", 1, Some("podman")).expect("intent");
    assert_eq!((intent.kind.as_str(), intent.attempt), ("qa", 1));

    let refused = executions::transition(
        &connection,
        &intent,
        ExecutionState::Running,
        Change::default(),
        "2026-10-01T00:00:01+00:00",
    );
    assert!(
        refused.is_err(),
        "a running QA run without a receipt: {refused:?}"
    );

    let running = executions::transition(
        &connection,
        &intent,
        ExecutionState::Running,
        Change {
            receipt: Some(json!({"kind": "qa"})),
            ..Change::default()
        },
        "2026-10-01T00:00:01+00:00",
    )
    .expect("running");
    assert_eq!(running.to_json()["container_id"], serde_json::Value::Null);
}

#[test]
fn every_attempt_is_its_own_execution_and_the_dispatch_shows_the_latest() {
    let connection = store("attempts");
    let first = qa(&connection, "op-1", 1, Some("docker")).expect("attempt 1");
    executions::transition(
        &connection,
        &first,
        ExecutionState::Lost,
        Change::default(),
        "2026-10-01T00:00:01+00:00",
    )
    .expect("lost");
    assert!(
        qa(&connection, "op-dup", 1, Some("docker")).is_err(),
        "an attempt is written once"
    );

    qa(&connection, "op-2", 2, Some("docker")).expect("attempt 2");

    let shown = executions::of_dispatch(&connection, "d")
        .unwrap()
        .expect("the dispatch has executions");
    assert_eq!((shown.id.as_str(), shown.attempt), ("op-2", 2));
    let status: String = connection
        .query_row(
            "SELECT execution_state FROM dispatch_status WHERE id = 'd'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(status, "launching");
}

/// The `executions` table of schema version 5, as that release created it.
const EXECUTIONS_V5: &str = "CREATE TABLE executions (
    id                    TEXT PRIMARY KEY,
    dispatch_id           TEXT NOT NULL REFERENCES dispatch (id),
    attempt               INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    state                 TEXT NOT NULL CHECK (state IN
        ('launching', 'running', 'stopping', 'exited', 'lost', 'unknown')),
    revision              INTEGER NOT NULL DEFAULT 1,
    kind                  TEXT NOT NULL DEFAULT 'process' CHECK (kind IN ('process', 'container')),
    engine                TEXT CHECK (engine IN ('docker', 'podman')),
    container_id          TEXT,
    launch_json           TEXT NOT NULL,
    execution_deadline    TEXT NOT NULL,
    stop_grace_seconds    INTEGER NOT NULL,
    poll_interval_seconds INTEGER NOT NULL,
    host                  TEXT,
    boot_id               TEXT,
    pid_namespace         TEXT,
    pid                   INTEGER,
    start_time            TEXT,
    pgid                  INTEGER,
    receipt_json          TEXT,
    exit_json             TEXT,
    observation_json      TEXT,
    lease_owner_json      TEXT NOT NULL,
    lease_released_at     TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    UNIQUE (dispatch_id, attempt),
    CHECK ((pid IS NULL) = (start_time IS NULL)),
    CHECK ((kind = 'container') = (engine IS NOT NULL)),
    CHECK (kind = 'container' OR container_id IS NULL),
    CHECK (state NOT IN ('running', 'stopping', 'exited')
        OR (kind = 'process' AND pid IS NOT NULL)
        OR (kind = 'container' AND container_id IS NOT NULL)),
    CHECK (state <> 'exited' OR exit_json IS NOT NULL),
    CHECK (lease_released_at IS NULL OR state IN ('exited', 'lost'))
) STRICT;";

#[test]
fn a_store_of_version_five_keeps_its_container_executions_at_version_six() {
    let path = tempdir("v5").join("runtime.db");
    let mut old = create(&path).unwrap();
    old.execute_batch(
        "DROP TRIGGER executions_transition; DROP TRIGGER executions_terminal;
         DROP TABLE executions; PRAGMA user_version = 5;",
    )
    .unwrap();
    old.execute_batch(EXECUTIONS_V5).unwrap();
    old.execute_batch(SEED).unwrap();
    old.execute_batch(
        "INSERT INTO executions (id, dispatch_id, state, kind, engine, container_id, launch_json,
             execution_deadline, stop_grace_seconds, poll_interval_seconds, lease_owner_json,
             created_at, updated_at)
         VALUES ('op', 'd', 'running', 'container', 'podman', 'cid', '{}',
                 '2026-10-01T04:00:00+00:00', 30, 1, '{}', '2026-10-01T00:00:00+00:00',
                 '2026-10-01T00:00:01+00:00');",
    )
    .unwrap();
    state::audit::append(
        &old,
        "2026-10-01T00:00:00+00:00",
        "legacy-import",
        &json!({}),
    )
    .unwrap();

    assert!(upgrade(&mut old).unwrap());

    let version: i64 = old
        .pragma_query_value(None, "user_version", |row| row.get(0))
        .unwrap();
    assert_eq!((SCHEMA_VERSION, version), (6, 6));
    let kept = executions::load(&old, "op").unwrap().expect("kept");
    assert_eq!(
        (
            kept.kind.as_str(),
            kept.engine.as_deref(),
            kept.container_id.as_deref()
        ),
        ("container", Some("podman"), Some("cid"))
    );
    qa(&old, "op-qa", 2, Some("podman")).expect("a QA attempt after the upgrade");
    let refused = old.execute(
        "UPDATE executions SET state = 'launching' WHERE id = 'op'",
        [],
    );
    assert!(refused.is_err(), "the triggers are created again");
}
