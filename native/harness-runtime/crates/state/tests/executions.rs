//! The executions of the supervisor in the store, schema version 4: the transition matrix that
//! the trigger and the runtime share, the terminal states, the lease, the denormalised
//! `execution_state` of the dispatch and the audit record of every transition (ADR 0021,
//! section 6; ADR 0022, section 2).

use contracts::{ErrorCode, ExecutionState};
use rusqlite::Connection;
use serde_json::{Value, json};
use state::executions::{self, Change, NewExecution, allowed, observed};
use state::identity::{Identity, Verdict};
use state::schema::{SCHEMA_VERSION, create, upgrade};

fn tempdir(name: &str) -> std::path::PathBuf {
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    let n = NEXT.fetch_add(1, Ordering::Relaxed);
    let path =
        std::env::temp_dir().join(format!("hr-executions-{name}-{}-{n}", std::process::id()));
    let _ = std::fs::remove_dir_all(&path);
    std::fs::create_dir_all(&path).unwrap();
    path
}

/// A store with one working dispatch and the anchor of its audit chain.
fn store(name: &str) -> Connection {
    let connection = create(&tempdir(name).join("runtime.db")).unwrap();
    connection
        .execute_batch(
            "INSERT INTO batch (id, state, body_json) VALUES ('b', 'active', '{}');
             INSERT INTO dispatch (id, batch_id, state, body_json) VALUES ('d', 'b', 'working', '{}');
             INSERT INTO dispatch_status (id, state, body_json) VALUES ('d', 'working', '{}');",
        )
        .unwrap();
    state::audit::append(
        &connection,
        "2026-10-01T00:00:00+00:00",
        "legacy-import",
        &json!({}),
    )
    .unwrap();
    connection
}

fn identity() -> Identity {
    Identity {
        host: "h".to_owned(),
        boot_id: Some("boot".to_owned()),
        pid_namespace: Some("pid:[1]".to_owned()),
        pid: 42,
        start_time: "100".to_owned(),
    }
}

fn launching(connection: &Connection) -> executions::Execution {
    executions::insert(
        connection,
        &NewExecution {
            id: "op",
            dispatch_id: "d",
            launch: &json!({"argv": ["sh"], "cwd": "/"}),
            execution_deadline: "2026-10-01T04:00:00+00:00",
            stop_grace_seconds: 30,
            poll_interval_seconds: 1,
            lease_owner: &json!({"pid": 7}),
        },
        "2026-10-01T00:00:00+00:00",
    )
    .unwrap()
}

fn running(connection: &Connection) -> executions::Execution {
    let intent = launching(connection);
    executions::transition(
        connection,
        &intent,
        ExecutionState::Running,
        Change {
            identity: Some(identity()),
            pgid: Some(42),
            receipt: Some(json!({"adopted": false})),
            ..Change::default()
        },
        "2026-10-01T00:00:01+00:00",
    )
    .unwrap()
}

fn audit_tail(connection: &Connection) -> (i64, String, Value) {
    connection
        .query_row(
            "SELECT seq, action, details_json FROM audit ORDER BY seq DESC LIMIT 1",
            [],
            |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    serde_json::from_str(&row.get::<_, String>(2)?).unwrap(),
                ))
            },
        )
        .unwrap()
}

fn execution_state_of_dispatch(connection: &Connection) -> Option<String> {
    connection
        .query_row(
            "SELECT execution_state FROM dispatch_status WHERE id = 'd'",
            [],
            |row| row.get(0),
        )
        .unwrap()
}

#[test]
fn the_trigger_of_the_store_allows_exactly_the_transitions_of_the_runtime() {
    for from in ExecutionState::ALL {
        for to in ExecutionState::ALL {
            if from == to {
                continue;
            }
            let connection = store("matrix");
            // Every CHECK holds in both states: the trigger alone decides.
            connection
                .execute(
                    "INSERT INTO executions (id, dispatch_id, state, launch_json,
                         execution_deadline, stop_grace_seconds, poll_interval_seconds, host,
                         pid, start_time, exit_json, lease_owner_json, created_at, updated_at)
                     VALUES ('e', 'd', ?1, '{}', 't', 1, 1, 'h', 1, '1', '{}', '{}', 't', 't')",
                    [from.as_str()],
                )
                .unwrap();

            let updated = connection.execute(
                "UPDATE executions SET state = ?1 WHERE id = 'e'",
                [to.as_str()],
            );

            assert_eq!(
                updated.is_ok(),
                allowed(from, to),
                "{from:?} -> {to:?}: {updated:?}"
            );
        }
    }
}

#[test]
fn only_exited_and_lost_are_terminal_and_unknown_leaves_only_by_an_observation() {
    let terminal: Vec<ExecutionState> = ExecutionState::ALL
        .into_iter()
        .filter(|state| executions::is_terminal(*state))
        .collect();
    assert_eq!(terminal, [ExecutionState::Exited, ExecutionState::Lost]);
    let from_unknown: Vec<ExecutionState> = ExecutionState::ALL
        .into_iter()
        .filter(|to| allowed(ExecutionState::Unknown, *to))
        .collect();
    assert_eq!(
        from_unknown,
        [
            ExecutionState::Running,
            ExecutionState::Exited,
            ExecutionState::Lost
        ]
    );
    for state in terminal {
        assert!(ExecutionState::ALL.iter().all(|to| !allowed(state, *to)));
    }
}

#[test]
fn a_terminal_execution_never_changes_again() {
    let connection = store("terminal");
    let execution = running(&connection);
    let exited = executions::transition(
        &connection,
        &execution,
        ExecutionState::Exited,
        Change {
            exit: Some(json!({"exit_code": 0})),
            ..Change::default()
        },
        "2026-10-01T00:00:02+00:00",
    )
    .unwrap();

    for sql in [
        "UPDATE executions SET revision = revision + 1",
        "UPDATE executions SET observation_json = '{}'",
        "UPDATE executions SET state = 'running'",
    ] {
        assert!(connection.execute(sql, []).is_err(), "{sql}");
    }
    let error = executions::transition(
        &connection,
        &exited,
        ExecutionState::Running,
        Change::default(),
        "2026-10-01T00:00:03+00:00",
    )
    .unwrap_err();
    assert_eq!(error.code, ErrorCode::InvariantViolation);
    assert_eq!(
        executions::load(&connection, "op").unwrap().unwrap().state,
        ExecutionState::Exited
    );
}

#[test]
fn a_lease_is_released_only_with_an_exited_or_lost_execution() {
    let connection = store("lease");
    let execution = running(&connection);

    let early = connection.execute(
        "UPDATE executions SET lease_released_at = 't' WHERE id = 'op'",
        [],
    );
    assert!(early.is_err());
    assert_eq!(execution.lease_released_at, None);
    let stopping = executions::transition(
        &connection,
        &execution,
        ExecutionState::Stopping,
        Change::default(),
        "2026-10-01T00:00:02+00:00",
    )
    .unwrap();
    assert_eq!(stopping.lease_released_at, None);

    let lost = executions::transition(
        &connection,
        &stopping,
        ExecutionState::Lost,
        Change::default(),
        "2026-10-01T00:00:03+00:00",
    )
    .unwrap();

    assert_eq!(
        lost.lease_released_at.as_deref(),
        Some("2026-10-01T00:00:03+00:00")
    );
}

#[test]
fn an_exited_execution_carries_its_exit_evidence() {
    let connection = store("evidence");
    let execution = running(&connection);

    let error = executions::transition(
        &connection,
        &execution,
        ExecutionState::Exited,
        Change::default(),
        "2026-10-01T00:00:02+00:00",
    )
    .unwrap_err();

    assert_eq!(error.code, ErrorCode::InvariantViolation);
}

#[test]
fn a_transition_sets_the_execution_state_of_the_dispatch_and_appends_one_audit_record() {
    let connection = store("audit");
    let before = audit_tail(&connection).0;

    let intent = launching(&connection);
    assert_eq!(
        execution_state_of_dispatch(&connection).as_deref(),
        Some("launching")
    );
    let (seq, action, details) = audit_tail(&connection);
    assert_eq!((seq, action.as_str()), (before + 1, "execution-launching"));
    assert_eq!(details["from"], Value::Null);
    assert_eq!(details["to"], "launching");
    let running = executions::transition(
        &connection,
        &intent,
        ExecutionState::Running,
        Change {
            identity: Some(identity()),
            pgid: Some(42),
            facts: json!({"adopted": false}),
            ..Change::default()
        },
        "2026-10-01T00:00:01+00:00",
    )
    .unwrap();

    assert_eq!(running.revision, intent.revision + 1);
    assert_eq!(running.identity, Some(identity()));
    assert_eq!(
        execution_state_of_dispatch(&connection).as_deref(),
        Some("running")
    );
    let (seq, action, details) = audit_tail(&connection);
    assert_eq!((seq, action.as_str()), (before + 2, "execution-running"));
    assert_eq!(
        details,
        json!({
            "execution_id": "op",
            "dispatch_id": "d",
            "from": "launching",
            "to": "running",
            "facts": {"adopted": false},
        })
    );
    let verified = state::audit::verify_tail(&connection).unwrap();
    assert_eq!(verified.last.unwrap().0, before + 2);
}

#[test]
fn a_transition_from_a_stale_revision_is_a_conflict_that_writes_nothing() {
    let connection = store("stale");
    let intent = launching(&connection);
    let tail = audit_tail(&connection).0;
    executions::transition(
        &connection,
        &intent,
        ExecutionState::Lost,
        Change::default(),
        "2026-10-01T00:00:01+00:00",
    )
    .unwrap();
    // The row moved on; a second writer still holds the intent it read.
    let error = executions::transition(
        &connection,
        &intent,
        ExecutionState::Unknown,
        Change::default(),
        "2026-10-01T00:00:02+00:00",
    )
    .unwrap_err();

    assert_eq!(error.code, ErrorCode::RevisionConflict);
    assert_eq!(audit_tail(&connection).0, tail + 1);
    assert_eq!(
        execution_state_of_dispatch(&connection).as_deref(),
        Some("lost")
    );
}

#[test]
fn an_observation_maps_the_verdict_on_the_recorded_identity_to_an_execution_state() {
    let cases = [
        (Verdict::Alive, false, ExecutionState::Running),
        (Verdict::Dead, true, ExecutionState::Exited),
        (Verdict::Dead, false, ExecutionState::Lost),
        (Verdict::PidReused, false, ExecutionState::Lost),
        (Verdict::PidReused, true, ExecutionState::Lost),
        (Verdict::OtherEnvironment, false, ExecutionState::Unknown),
        (Verdict::OnAnotherHost, true, ExecutionState::Unknown),
        (Verdict::Unknown, false, ExecutionState::Unknown),
    ];
    for (verdict, evidence, expected) in cases {
        assert_eq!(observed(verdict, evidence), expected, "{verdict:?}");
    }
}

#[test]
fn a_store_of_version_three_is_upgraded_to_version_four_and_keeps_its_rows() {
    let path = tempdir("v3").join("runtime.db");
    let mut old = create(&path).unwrap();
    old.execute_batch(
        "DROP TRIGGER executions_transition;
         DROP TRIGGER executions_terminal;
         DROP TABLE executions;
         PRAGMA user_version = 3;
         INSERT INTO batch (id, state, body_json) VALUES ('b', 'active', '{\"a\":1}');
         INSERT INTO dispatch (id, batch_id, state, body_json) VALUES ('d', 'b', 'working', '{}');
         INSERT INTO dispatch_status (id, state, body_json, execution_state)
             VALUES ('d', 'working', '{\"s\":1}', NULL);",
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
    assert_eq!((SCHEMA_VERSION, version), (4, 4));
    let body: String = old
        .query_row("SELECT body_json FROM batch WHERE id = 'b'", [], |row| {
            row.get(0)
        })
        .unwrap();
    assert_eq!(body, "{\"a\":1}");
    let (action, details) = {
        let (_, action, details) = audit_tail(&old);
        (action, details)
    };
    assert_eq!(action, "schema-migrate");
    assert_eq!(details, json!({"from": 3, "to": 4}));
    let triggers: i64 = old
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'executions'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(triggers, 2);
    // The upgraded store takes an execution like a new one.
    launching(&old);
}
