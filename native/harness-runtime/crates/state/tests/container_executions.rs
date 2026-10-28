//! Schema version 5: an execution is a `process` or a `container` of an engine, and a running
//! container is proven by its container id instead of a host pid (ADR 0022, section 4).

use contracts::ExecutionState;
use rusqlite::Connection;
use serde_json::json;
use state::executions::{self, Change, NewExecution};
use state::schema::{SCHEMA_VERSION, create, upgrade};

fn tempdir(name: &str) -> std::path::PathBuf {
    let path = std::env::temp_dir().join(format!(
        "hr-container-executions-{name}-{}",
        std::process::id()
    ));
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

fn container(
    connection: &Connection,
    engine: Option<&str>,
) -> Result<executions::Execution, String> {
    executions::insert(
        connection,
        &NewExecution {
            id: "op",
            dispatch_id: "d",
            kind: "container",
            engine,
            launch: &json!({"objects": [["container", "harness-d"]]}),
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
fn a_running_container_needs_its_container_id_and_no_host_pid() {
    let connection = store("running");
    let intent = container(&connection, Some("podman")).expect("intent");
    assert_eq!(
        (intent.kind.as_str(), intent.engine.as_deref()),
        ("container", Some("podman"))
    );

    let refused = executions::transition(
        &connection,
        &intent,
        ExecutionState::Running,
        Change::default(),
        "2026-10-01T00:00:01+00:00",
    );
    assert!(
        refused.is_err(),
        "a running container without an id: {refused:?}"
    );

    let running = executions::transition(
        &connection,
        &intent,
        ExecutionState::Running,
        Change {
            container_id: Some("c".repeat(64)),
            receipt: Some(json!({"engine": "podman"})),
            ..Change::default()
        },
        "2026-10-01T00:00:01+00:00",
    )
    .expect("running");

    let shown = running.to_json();
    assert_eq!(shown["kind"], "container");
    assert_eq!(shown["engine"], "podman");
    assert_eq!(shown["container_id"], "c".repeat(64));
    assert_eq!(shown["identity"], serde_json::Value::Null);
}

#[test]
fn a_container_names_its_engine_and_a_process_names_none() {
    let connection = store("engine");
    assert!(container(&connection, None).is_err());
    assert!(container(&connection, Some("lxc")).is_err());
}

/// The executions table of schema version 4, as release 4 created it.
const EXECUTIONS_V4: &str = "
CREATE TABLE executions (
    id TEXT PRIMARY KEY, dispatch_id TEXT NOT NULL REFERENCES dispatch (id),
    attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    state TEXT NOT NULL CHECK (state IN
        ('launching', 'running', 'stopping', 'exited', 'lost', 'unknown')),
    revision INTEGER NOT NULL DEFAULT 1, launch_json TEXT NOT NULL,
    execution_deadline TEXT NOT NULL, stop_grace_seconds INTEGER NOT NULL,
    poll_interval_seconds INTEGER NOT NULL, host TEXT, boot_id TEXT, pid_namespace TEXT,
    pid INTEGER, start_time TEXT, pgid INTEGER, receipt_json TEXT, exit_json TEXT,
    observation_json TEXT, lease_owner_json TEXT NOT NULL, lease_released_at TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE (dispatch_id, attempt),
    CHECK ((pid IS NULL) = (start_time IS NULL)),
    CHECK (state NOT IN ('running', 'stopping', 'exited') OR pid IS NOT NULL),
    CHECK (state <> 'exited' OR exit_json IS NOT NULL),
    CHECK (lease_released_at IS NULL OR state IN ('exited', 'lost'))
) STRICT;
CREATE TRIGGER executions_terminal BEFORE UPDATE ON executions
WHEN OLD.state IN ('exited', 'lost')
BEGIN SELECT RAISE(ABORT, 'a terminal execution never changes'); END;
CREATE TRIGGER executions_transition BEFORE UPDATE OF state ON executions
WHEN OLD.state <> NEW.state AND NOT (
       (OLD.state = 'launching' AND NEW.state IN ('running', 'exited', 'lost', 'unknown'))
    OR (OLD.state = 'running' AND NEW.state IN ('stopping', 'exited', 'lost', 'unknown'))
    OR (OLD.state = 'stopping' AND NEW.state IN ('exited', 'lost', 'unknown'))
    OR (OLD.state = 'unknown' AND NEW.state IN ('running', 'exited', 'lost')))
BEGIN SELECT RAISE(ABORT, 'the execution state does not allow this transition'); END;";

#[test]
fn a_store_of_version_four_keeps_its_process_executions_at_version_five() {
    let path = tempdir("v4").join("runtime.db");
    let mut old = create(&path).unwrap();
    old.execute_batch(
        "DROP TRIGGER executions_transition; DROP TRIGGER executions_terminal;
         DROP TABLE executions; PRAGMA user_version = 4;",
    )
    .unwrap();
    old.execute_batch(EXECUTIONS_V4).unwrap();
    old.execute_batch(SEED).unwrap();
    old.execute_batch(
        "INSERT INTO executions (id, dispatch_id, state, launch_json, execution_deadline,
             stop_grace_seconds, poll_interval_seconds, host, pid, start_time, pgid,
             lease_owner_json, created_at, updated_at)
         VALUES ('op', 'd', 'running', '{\"argv\":[\"sh\"]}', '2026-10-01T04:00:00+00:00', 30, 1,
                 'h', 42, '100', 42, '{}', '2026-10-01T00:00:00+00:00', '2026-10-01T00:00:01+00:00');",
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
    assert_eq!((SCHEMA_VERSION, version), (5, 5));
    let kept = executions::load(&old, "op")
        .unwrap()
        .expect("the row is kept");
    assert_eq!(
        (kept.kind.as_str(), kept.engine, kept.container_id),
        ("process", None, None)
    );
    assert_eq!(kept.identity.map(|identity| identity.pid), Some(42));
    assert_eq!(kept.state, ExecutionState::Running);
    let triggers: i64 = old
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'executions'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(triggers, 2);
    let action: (String, String) = old
        .query_row(
            "SELECT action, details_json FROM audit ORDER BY seq DESC LIMIT 1",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    assert_eq!(action.0, "schema-migrate");
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&action.1).unwrap(),
        json!({"from": 4, "to": 5})
    );
    // The rebuilt table still refuses a transition out of a terminal state.
    old.execute(
        "UPDATE executions SET state = 'lost', lease_released_at = 'x' WHERE id = 'op'",
        [],
    )
    .unwrap();
    assert!(
        old.execute(
            "UPDATE executions SET state = 'running' WHERE id = 'op'",
            []
        )
        .is_err()
    );
}
