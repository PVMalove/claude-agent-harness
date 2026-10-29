//! The write operation of the cut-over store: revisions, idempotency, immutability, the CHECK
//! invariants, one batch per request, the order of the rows and the audit records
//! (ADR 0020, sections 2 to 4; ADR 0021, section 4).

mod common;

use std::collections::BTreeMap;

use contracts::{ErrorBody, ErrorCode, PROTOCOL_VERSION, Request};
use rusqlite::Connection;
use serde_json::{Map, Value, json};
use state::canonical::{canonical_json, sha256_hex};
use state::migrate::run;
use state::read::read;
use state::write::write;

use common::Repo;

const BATCH: &str = "batch-aaaaaaaa-0000-4000-8000-000000000001";
const DISPATCH: &str = "dispatch-11111111-0000-4000-8000-000000000001";
const SECOND: &str = "dispatch-22222222-0000-4000-8000-000000000002";

fn cut_over(name: &str) -> Repo {
    let repo = Repo::new(name);
    run(&repo.root).unwrap();
    repo
}

fn request(id: &str, batch: Option<&str>, expected: Option<u64>, mutations: Value) -> Request {
    Request {
        protocol_version: PROTOCOL_VERSION,
        operation_id: id.to_owned(),
        operation: "state.write".to_owned(),
        request_deadline: u64::MAX,
        batch_id: batch.map(str::to_owned),
        dispatch_id: None,
        candidate_sha: None,
        plan_digest: None,
        expected_revision: expected,
        fencing_token: None,
        payload: Some(json!({"mutations": mutations})),
    }
}

fn canonical(value: &Value) -> String {
    canonical_json(value)
}

fn store(repo: &Repo) -> Connection {
    Connection::open(repo.state().join("runtime.db")).unwrap()
}

fn count(repo: &Repo, table: &str) -> i64 {
    store(repo)
        .query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |row| {
            row.get(0)
        })
        .unwrap()
}

/// The record at `path` and its revision.
fn stored(repo: &Repo, path: &str) -> Option<(Map<String, Value>, u64)> {
    let answer = read(&repo.state(), Some(&json!({"paths": [path]})), None).unwrap();
    let record = answer.records.get(path)?;
    let Value::Object(record) = record.clone() else {
        panic!("a record is an object")
    };
    // A row of an immutable table has no revision; it is written once, so it stands at 1.
    let revision = answer
        .revisions
        .get(path)
        .and_then(Value::as_u64)
        .unwrap_or(1);
    Some((record, revision))
}

fn batch_path() -> String {
    format!("batches/{BATCH}.json")
}

fn status_path(id: &str) -> String {
    format!("dispatch-status/{id}.json")
}

fn code(error: &ErrorBody) -> ErrorCode {
    error.code
}

fn replaced_batch(repo: &Repo, ticket: &str) -> (Value, u64) {
    let (mut record, revision) = stored(repo, &batch_path()).unwrap();
    record.insert("ticket".to_owned(), json!(ticket));
    (Value::Object(record), revision)
}

fn replace_batch(repo: &Repo, id: &str, ticket: &str) -> Result<state::write::Written, ErrorBody> {
    let (record, revision) = replaced_batch(repo, ticket);
    write(
        &repo.state(),
        &request(
            id,
            Some(BATCH),
            Some(revision),
            json!([{"op": "replace", "path": batch_path(), "content": canonical(&record),
                    "expected_revision": revision, "decision": {"kind": "approve"}}]),
        ),
    )
}

fn audit_tail(repo: &Repo, count: i64) -> Vec<(String, Value)> {
    let connection = store(repo);
    let mut statement = connection
        .prepare("SELECT action, details_json FROM audit ORDER BY seq DESC LIMIT ?1")
        .unwrap();
    let mut rows: Vec<(String, Value)> = statement
        .query_map([count], |row| {
            let details: String = row.get(1)?;
            Ok((row.get(0)?, serde_json::from_str(&details).unwrap()))
        })
        .unwrap()
        .map(Result::unwrap)
        .collect();
    rows.reverse();
    rows
}

#[test]
fn a_replace_with_the_revision_the_command_read_commits_and_advances_the_revision_by_one() {
    let repo = cut_over("replace");
    let (_, before) = stored(&repo, &batch_path()).unwrap();

    let written = replace_batch(&repo, "op-1", "#2").unwrap();

    let (record, after) = stored(&repo, &batch_path()).unwrap();
    assert_eq!(after, before + 1);
    assert_eq!(record["ticket"], "#2");
    assert_eq!(written.result["revisions"][batch_path()], after);
    assert_eq!(
        written.revision,
        written.result["audit_seq"].as_u64().unwrap()
    );
}

#[test]
fn a_replace_appends_one_transition_with_both_hashes_the_states_and_the_decision() {
    let repo = cut_over("transition");
    let (before_record, _) = stored(&repo, &batch_path()).unwrap();
    let audit_before = count(&repo, "audit");

    replace_batch(&repo, "op-1", "#2").unwrap();

    assert_eq!(count(&repo, "audit"), audit_before + 1);
    let (after_record, _) = stored(&repo, &batch_path()).unwrap();
    let (action, details) = &audit_tail(&repo, 1)[0];
    assert_eq!(action, "transition");
    assert_eq!(details["path"], batch_path());
    assert_eq!(
        details["before_sha256"],
        sha256_hex(canonical(&Value::Object(before_record.clone())).as_bytes())
    );
    assert_eq!(
        details["sha256"],
        sha256_hex(canonical(&Value::Object(after_record)).as_bytes())
    );
    assert_eq!(details["from"], before_record["state"]);
    assert_eq!(details["to"], before_record["state"]);
    assert_eq!(details["decision"], json!({"kind": "approve"}));
}

#[test]
fn the_audit_chain_is_readable_as_legacy_audit_files_with_the_decision() {
    let repo = cut_over("chain-read");
    let written = replace_batch(&repo, "op-1", "#2").unwrap();

    let name = format!("audit/audit-chain-{:012}.json", written.revision);
    let answer = read(&repo.state(), Some(&json!({"paths": [name]})), None).unwrap();
    let record = &answer.records[&name];
    assert_eq!(record["action"], "transition");
    assert_eq!(record["details"]["decision"], json!({"kind": "approve"}));
    let all = read(
        &repo.state(),
        Some(&json!({"collections": ["audit"]})),
        None,
    )
    .unwrap();
    assert!(all.records.contains_key(&name));
}

#[test]
fn a_stale_expected_revision_is_a_conflict_that_writes_nothing() {
    let repo = cut_over("stale");
    replace_batch(&repo, "op-1", "#2").unwrap();
    let (before, revision) = stored(&repo, &batch_path()).unwrap();
    let audit_before = count(&repo, "audit");
    let stale = revision - 1;

    let error = write(
        &repo.state(),
        &request(
            "op-2",
            None,
            None,
            json!([{"op": "replace", "path": batch_path(),
                    "content": canonical(&json!({"state": "active"})),
                    "expected_revision": stale}]),
        ),
    )
    .unwrap_err();

    assert_eq!(code(&error), ErrorCode::RevisionConflict);
    assert!(error.retryable);
    assert_eq!(error.facts["path"], batch_path());
    assert_eq!(error.facts["expected_revision"], stale);
    assert_eq!(error.facts["actual_revision"], revision);
    assert_eq!(stored(&repo, &batch_path()).unwrap(), (before, revision));
    assert_eq!(count(&repo, "audit"), audit_before);
}

#[test]
fn the_revision_of_the_batch_in_the_envelope_guards_a_write_that_does_not_touch_the_batch() {
    let repo = cut_over("envelope");
    let (_, revision) = stored(&repo, &batch_path()).unwrap();
    replace_batch(&repo, "op-1", "#2").unwrap();
    let (mut status, status_revision) = stored(&repo, &status_path(SECOND)).unwrap();
    status.insert("note".to_owned(), json!("x"));

    let error = write(
        &repo.state(),
        &request(
            "op-2",
            Some(BATCH),
            Some(revision),
            json!([{"op": "replace", "path": status_path(SECOND),
                    "content": canonical(&Value::Object(status)),
                    "expected_revision": status_revision}]),
        ),
    )
    .unwrap_err();

    assert_eq!(code(&error), ErrorCode::RevisionConflict);
    assert_eq!(error.facts["path"], batch_path());
}

#[test]
fn a_refusal_is_remembered_and_a_repeat_of_the_same_operation_gets_the_same_error() {
    let repo = cut_over("failed");
    let mutations = json!([{"op": "replace", "path": batch_path(),
        "content": canonical(&json!({"state": "active"})), "expected_revision": 99}]);
    let first = write(
        &repo.state(),
        &request("op-1", None, None, mutations.clone()),
    )
    .unwrap_err();

    let again = write(&repo.state(), &request("op-1", None, None, mutations)).unwrap_err();

    assert_eq!(first, again);
    let (state, id): (String, String) = store(&repo)
        .query_row("SELECT state, operation_id FROM operations", [], |row| {
            Ok((row.get(0)?, row.get(1)?))
        })
        .unwrap();
    assert_eq!((state.as_str(), id.as_str()), ("failed", "op-1"));
}

#[test]
fn a_repeat_with_the_same_id_and_request_returns_the_stored_result_without_a_second_effect() {
    let repo = cut_over("repeat");
    let (record, revision) = replaced_batch(&repo, "#2");
    let repeat = request(
        "op-1",
        Some(BATCH),
        Some(revision),
        json!([{"op": "replace", "path": batch_path(), "content": canonical(&record),
                "expected_revision": revision}]),
    );
    let first = write(&repo.state(), &repeat).unwrap();
    let audit_after_first = count(&repo, "audit");

    let second = write(&repo.state(), &repeat).unwrap();

    assert_eq!(second.result, first.result);
    assert_eq!(second.revision, first.revision);
    assert_eq!(count(&repo, "audit"), audit_after_first);
    let state: String = store(&repo)
        .query_row(
            "SELECT state FROM operations WHERE operation_id = 'op-1'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(state, "committed");
}

#[test]
fn the_same_id_with_another_request_is_an_idempotency_conflict() {
    let repo = cut_over("conflict");
    replace_batch(&repo, "op-1", "#2").unwrap();
    let (mut other, revision) = stored(&repo, &batch_path()).unwrap();
    other.insert("ticket".to_owned(), json!("#3"));
    let audit_before = count(&repo, "audit");

    let error = write(
        &repo.state(),
        &request(
            "op-1",
            Some(BATCH),
            Some(revision),
            json!([{"op": "replace", "path": batch_path(),
                    "content": canonical(&Value::Object(other)), "expected_revision": revision}]),
        ),
    )
    .unwrap_err();

    assert_eq!(code(&error), ErrorCode::IdempotencyConflict);
    assert!(!error.retryable);
    assert_eq!(count(&repo, "audit"), audit_before);
}

#[test]
fn a_new_row_has_revision_one_and_creating_it_again_is_a_conflict() {
    let repo = cut_over("create");
    let dispatch =
        canonical(&json!({"dispatch_id": "dispatch-new", "batch_id": BATCH, "state": "approved"}));
    let status = canonical(&json!({"dispatch_id": "dispatch-new", "state": "approved"}));
    let create = json!([
        {"op": "create", "path": "dispatches/dispatch-new.json", "content": dispatch},
        {"op": "create", "path": status_path("dispatch-new"), "content": status},
    ]);

    let written = write(
        &repo.state(),
        &request("op-1", Some(BATCH), None, create.clone()),
    )
    .unwrap();

    assert_eq!(
        written.result["revisions"]["dispatches/dispatch-new.json"],
        1
    );
    assert_eq!(written.result["revisions"][status_path("dispatch-new")], 1);
    assert_eq!(
        audit_tail(&repo, 2)
            .iter()
            .map(|(a, _)| a.as_str())
            .collect::<Vec<_>>(),
        ["immutable-record", "immutable-record"]
    );
    let error = write(&repo.state(), &request("op-2", Some(BATCH), None, create)).unwrap_err();
    assert_eq!(code(&error), ErrorCode::RevisionConflict);
    assert_eq!(error.facts["actual_revision"], 1);
}

#[test]
fn a_batch_that_owes_a_reassessment_without_candidate_or_pending_is_an_invariant_violation() {
    let repo = cut_over("check-batch");
    let (mut record, revision) = stored(&repo, &batch_path()).unwrap();
    record.remove("risk_reassessment_candidate");
    let audit_before = count(&repo, "audit");

    let error = write(
        &repo.state(),
        &request(
            "op-1",
            Some(BATCH),
            Some(revision),
            json!([{"op": "replace", "path": batch_path(),
                    "content": canonical(&Value::Object(record)), "expected_revision": revision}]),
        ),
    )
    .unwrap_err();

    assert_eq!(code(&error), ErrorCode::InvariantViolation);
    assert!(!error.retryable);
    assert!(error.remedy.contains("ledger validate --full"));
    assert_eq!(stored(&repo, &batch_path()).unwrap().1, revision);
    assert_eq!(count(&repo, "audit"), audit_before);
}

#[test]
fn a_terminal_dispatch_state_while_the_execution_may_run_is_an_invariant_violation() {
    let repo = cut_over("check-status");
    store(&repo)
        .execute(
            "UPDATE dispatch_status SET execution_state = 'running' WHERE id = ?1",
            [SECOND],
        )
        .unwrap();
    let (mut record, revision) = stored(&repo, &status_path(SECOND)).unwrap();
    record.insert("state".to_owned(), json!("failed"));

    let error = write(
        &repo.state(),
        &request(
            "op-1",
            Some(BATCH),
            None,
            json!([{"op": "replace", "path": status_path(SECOND),
                    "content": canonical(&Value::Object(record)), "expected_revision": revision}]),
        ),
    )
    .unwrap_err();

    assert_eq!(code(&error), ErrorCode::InvariantViolation);
    assert_eq!(stored(&repo, &status_path(SECOND)).unwrap().1, revision);
}

#[test]
fn immutable_evidence_is_never_replaced_or_deleted() {
    let repo = cut_over("immutable");
    for (path, op) in [
        (format!("plans/{BATCH}.json"), "replace"),
        (format!("dispatches/{DISPATCH}.json"), "replace"),
        ("checkpoints/checkpoint-0001.json".to_owned(), "replace"),
        (format!("reports/{DISPATCH}.json"), "replace"),
        (batch_path(), "delete"),
        (status_path(DISPATCH), "delete"),
    ] {
        let (record, revision) = stored(&repo, &path).unwrap();
        let mutation = if op == "delete" {
            json!({"op": "delete", "path": path, "reason": "x", "expected_revision": revision})
        } else {
            json!({"op": "replace", "path": path,
                   "content": canonical(&Value::Object(record)), "expected_revision": revision})
        };

        let error = write(
            &repo.state(),
            &request(&format!("op-{path}"), None, None, json!([mutation])),
        )
        .unwrap_err();

        assert_eq!(code(&error), ErrorCode::InvariantViolation, "{op} {path}");
    }
}

#[test]
fn a_request_for_the_rows_of_two_batches_is_malformed_and_writes_nothing() {
    let repo = cut_over("two-batches");
    let audit_before = count(&repo, "audit");
    let other = |id: &str| {
        canonical(&json!({"dispatch_id": id, "batch_id": "batch-other", "state": "approved"}))
    };

    let error = write(
        &repo.state(),
        &request(
            "op-1",
            Some(BATCH),
            None,
            json!([
                {"op": "create", "path": "dispatches/dispatch-x.json", "content": other("dispatch-x")},
            ]),
        ),
    )
    .unwrap_err();

    assert_eq!(code(&error), ErrorCode::MalformedRequest);
    assert_eq!(count(&repo, "audit"), audit_before);
}

#[test]
fn a_malformed_mutation_is_refused_before_anything_is_written() {
    let repo = cut_over("malformed");
    let audit_before = count(&repo, "audit");
    let object = canonical(&json!({"state": "active"}));
    let cases = [
        json!({"op": "create", "path": "audit/x.json", "content": object}),
        json!({"op": "create", "path": "reports/../x.json", "content": object}),
        json!({"op": "create", "path": "/etc/x.json", "content": object}),
        json!({"op": "create", "path": "x.json", "content": object}),
        json!({"op": "create", "path": "unknown/x.json", "content": object}),
        json!({"op": "create", "path": "reports/x.json", "content": "{\"b\": 1,\n \"a\": 2}"}),
        json!({"op": "create", "path": "reports/x.json", "content": "[]"}),
        json!({"op": "mutate", "path": "reports/x.json", "content": object}),
        json!({"op": "create", "path": "reports/x.json", "content": object, "surprise": 1}),
        json!({"op": "create", "path": "reports/x.json"}),
        json!({"op": "delete", "path": "qa-lane/lease.json"}),
        json!({"op": "create_artifact", "path": "dispatches/x.json", "content": object}),
        json!({"op": "replace", "path": batch_path(), "content": canonical(&json!({"state": "active"}))}),
    ];
    for (index, case) in cases.iter().enumerate() {
        let error = write(
            &repo.state(),
            &request(&format!("op-{index}"), None, None, json!([case])),
        )
        .unwrap_err();
        assert_eq!(code(&error), ErrorCode::MalformedRequest, "{case}");
    }
    assert_eq!(count(&repo, "audit"), audit_before);
}

#[test]
fn rows_are_touched_batch_then_qa_lane_then_generation_whatever_the_request_order() {
    let repo = cut_over("order");
    let queue = canonical(&json!({"dispatch_id": "d", "state": "queued"}));
    let report = canonical(&json!({"dispatch_id": "d"}));
    let first = write(
        &repo.state(),
        &request(
            "op-1",
            None,
            None,
            json!([
                {"op": "create", "path": "qa-lane/queue/q1.json", "content": queue},
                {"op": "create", "path": "reports/r1.json", "content": report},
            ]),
        ),
    )
    .unwrap();
    let second = write(
        &repo.state(),
        &request(
            "op-2",
            None,
            None,
            json!([
                {"op": "create", "path": "reports/r2.json", "content": report},
                {"op": "create", "path": "qa-lane/queue/q2.json", "content": queue},
            ]),
        ),
    )
    .unwrap();

    assert_eq!(first.touched, ["batch", "qa_lane", "generation"]);
    assert_eq!(second.touched, ["batch", "qa_lane", "generation"]);
    // The audit records follow the order of the request, not the order of the rows.
    let tail = audit_tail(&repo, 4);
    let paths: Vec<&str> = tail
        .iter()
        .map(|(_, d)| d["path"].as_str().unwrap())
        .collect();
    assert_eq!(
        paths,
        [
            "qa-lane/queue/q1.json",
            "reports/r1.json",
            "reports/r2.json",
            "qa-lane/queue/q2.json"
        ]
    );
}

#[test]
fn two_writers_in_opposite_orders_both_commit() {
    let repo = cut_over("threads");
    let queue = |id: &str| canonical(&json!({"dispatch_id": id, "state": "queued"}));
    let report = |id: &str| canonical(&json!({"dispatch_id": id}));
    let state = repo.state();
    let handles: Vec<_> = (0..2)
        .map(|n| {
            let state = state.clone();
            let (queue_body, report_body) = (queue(&format!("d{n}")), report(&format!("d{n}")));
            std::thread::spawn(move || {
                let mutations = if n == 0 {
                    json!([
                        {"op": "create", "path": "qa-lane/queue/t0.json", "content": queue_body},
                        {"op": "create", "path": "reports/t0.json", "content": report_body},
                    ])
                } else {
                    json!([
                        {"op": "create", "path": "reports/t1.json", "content": report_body},
                        {"op": "create", "path": "qa-lane/queue/t1.json", "content": queue_body},
                    ])
                };
                write(&state, &request(&format!("op-{n}"), None, None, mutations))
            })
        })
        .collect();

    for handle in handles {
        assert_eq!(
            handle.join().unwrap().unwrap().touched,
            ["batch", "qa_lane", "generation"]
        );
    }
    assert_eq!(count(&repo, "operations"), 2);
}

/// The batch `id`, a copy of the fixture batch in the state `state`.
fn batch_record(repo: &Repo, id: &str, state: &str) -> String {
    let (mut record, _) = stored(repo, &batch_path()).unwrap();
    record.insert("batch_id".to_owned(), json!(id));
    record.insert("state".to_owned(), json!(state));
    canonical(&Value::Object(record))
}

const OTHER_BATCH: &str = "batch-bbbbbbbb-0000-4000-8000-000000000002";

fn generation_revision(repo: &Repo) -> u64 {
    read(&repo.state(), Some(&json!({"paths": []})), None)
        .unwrap()
        .generation_revision
}

fn guarded(id: &str, batch: &str, expected_generation: Option<u64>, mutations: Value) -> Request {
    let mut request = request(id, Some(batch), None, mutations.clone());
    request.payload = Some(json!({
        "mutations": mutations,
        "expected_generation_revision": expected_generation,
    }));
    request
}

#[test]
fn a_new_batch_or_a_new_batch_state_moves_the_generation_and_nothing_else_does() {
    let repo = cut_over("generation");
    let start = generation_revision(&repo);

    let created = write(
        &repo.state(),
        &request(
            "op-1",
            Some(OTHER_BATCH),
            None,
            json!([{"op": "create", "path": format!("batches/{OTHER_BATCH}.json"),
                    "content": batch_record(&repo, OTHER_BATCH, "planned")}]),
        ),
    )
    .unwrap();
    assert_eq!(created.result["generation_revision"], start + 1);

    let same_state = replace_batch(&repo, "op-2", "#2").unwrap();
    assert_eq!(same_state.result["generation_revision"], start + 1);

    let report = canonical(&json!({"dispatch_id": "d"}));
    let other = write(
        &repo.state(),
        &request(
            "op-3",
            None,
            None,
            json!([{"op": "create", "path": "reports/g.json", "content": report}]),
        ),
    )
    .unwrap();
    assert_eq!(other.result["generation_revision"], start + 1);

    let path = format!("batches/{OTHER_BATCH}.json");
    let changed = write(
        &repo.state(),
        &request(
            "op-4",
            Some(OTHER_BATCH),
            None,
            json!([{"op": "replace", "path": path, "expected_revision": 1,
                    "content": batch_record(&repo, OTHER_BATCH, "active")}]),
        ),
    )
    .unwrap();
    assert_eq!(changed.result["generation_revision"], start + 2);
    assert_eq!(generation_revision(&repo), start + 2);
}

#[test]
fn a_stale_expected_generation_revision_is_a_conflict_on_the_generation_that_writes_nothing() {
    let repo = cut_over("generation-guard");
    let start = generation_revision(&repo);
    let before = count(&repo, "audit");

    let error = write(
        &repo.state(),
        &guarded(
            "op-1",
            OTHER_BATCH,
            Some(start + 7),
            json!([{"op": "create", "path": format!("batches/{OTHER_BATCH}.json"),
                    "content": batch_record(&repo, OTHER_BATCH, "planned")}]),
        ),
    )
    .unwrap_err();

    assert_eq!(code(&error), ErrorCode::RevisionConflict);
    assert_eq!(error.facts["path"], "generation");
    assert_eq!(error.facts["expected_revision"], start + 7);
    assert_eq!(error.facts["actual_revision"], start);
    assert_eq!(count(&repo, "audit"), before);
    assert_eq!(generation_revision(&repo), start);
}

/// Two requests in opposite orders over the typed rows of two batches and the QA queue, each
/// moving its batch to another state. Without the guard both commit; with the generation
/// revision both read, exactly one commits and the other meets the conflict on the generation.
#[test]
fn opposite_requests_over_two_batches_and_the_qa_queue_have_one_winner_under_the_guard() {
    for guard in [false, true] {
        let repo = cut_over(if guard { "guarded" } else { "unguarded" });
        write(
            &repo.state(),
            &request(
                "op-setup",
                Some(OTHER_BATCH),
                None,
                json!([{"op": "create", "path": format!("batches/{OTHER_BATCH}.json"),
                        "content": batch_record(&repo, OTHER_BATCH, "planned")}]),
            ),
        )
        .unwrap();
        let read_generation = generation_revision(&repo);
        let expected = guard.then_some(read_generation);
        let (_, revision_a) = stored(&repo, &batch_path()).unwrap();
        let first = json!([
            {"op": "replace", "path": batch_path(), "expected_revision": revision_a,
             "content": batch_record(&repo, BATCH, "paused")},
            {"op": "create", "path": "qa-lane/queue/qa.json",
             "content": canonical(&json!({"dispatch_id": "a"}))},
        ]);
        let second = json!([
            {"op": "create", "path": "qa-lane/queue/qb.json",
             "content": canonical(&json!({"dispatch_id": "b"}))},
            {"op": "replace", "path": format!("batches/{OTHER_BATCH}.json"),
             "expected_revision": 1, "content": batch_record(&repo, OTHER_BATCH, "active")},
        ]);
        let state = repo.state();
        let handles: Vec<_> = [(BATCH, first), (OTHER_BATCH, second)]
            .into_iter()
            .enumerate()
            .map(|(n, (batch, mutations))| {
                let state = state.clone();
                std::thread::spawn(move || {
                    write(
                        &state,
                        &guarded(&format!("op-{n}"), batch, expected, mutations),
                    )
                })
            })
            .collect();
        let outcomes: Vec<_> = handles
            .into_iter()
            .map(|handle| handle.join().unwrap())
            .collect();

        let won: Vec<_> = outcomes.iter().filter_map(|o| o.as_ref().ok()).collect();
        for written in &won {
            assert_eq!(written.touched, ["batch", "qa_lane", "generation"]);
        }
        if guard {
            assert_eq!(won.len(), 1, "{outcomes:?}");
            let lost = outcomes.iter().find_map(|o| o.as_ref().err()).unwrap();
            assert_eq!(code(lost), ErrorCode::RevisionConflict);
            assert_eq!(lost.facts["path"], "generation");
            assert_eq!(generation_revision(&repo), read_generation + 1);
        } else {
            assert_eq!(won.len(), 2, "{outcomes:?}");
            assert_eq!(generation_revision(&repo), read_generation + 2);
        }
    }
}

/// The QA lease changes only through the lease operations (`tests/lease.rs`): its generic
/// mutations are refused, and nothing is written.
#[test]
fn the_generic_mutations_of_the_qa_lease_are_refused() {
    let repo = cut_over("lease");
    let audit_before = count(&repo, "audit");
    let lease = canonical(&json!({"owner": "a"}));
    for (index, mutation) in [
        json!({"op": "create", "path": "qa-lane/lease.json", "content": lease}),
        json!({"op": "replace", "path": "qa-lane/lease.json", "content": lease,
               "expected_revision": 1}),
        json!({"op": "delete", "path": "qa-lane/lease.json", "reason": "complete QA lease",
               "expected_revision": 1}),
    ]
    .into_iter()
    .enumerate()
    {
        let error = write(
            &repo.state(),
            &request(&format!("op-{index}"), None, None, json!([mutation])),
        )
        .unwrap_err();

        assert_eq!(code(&error), ErrorCode::MalformedRequest, "{mutation}");
        assert!(
            error.message.contains("lease operations"),
            "{}",
            error.message
        );
    }
    assert_eq!(count(&repo, "audit"), audit_before);
    assert_eq!(count(&repo, "qa_lane"), 0);
}

#[test]
fn an_artifact_written_twice_with_the_same_bytes_is_the_same_evidence() {
    let repo = cut_over("artifact");
    let path = "qa-artifacts/log-1.log";
    let artifact = |id: &str, text: &str| {
        write(
            &repo.state(),
            &request(
                id,
                None,
                None,
                json!([{"op": "create_artifact", "path": path, "content": text}]),
            ),
        )
    };

    artifact("op-1", "line\n").unwrap();
    artifact("op-2", "line\n").unwrap();
    let error = artifact("op-3", "other\n").unwrap_err();

    assert_eq!(code(&error), ErrorCode::InvariantViolation);
    let sha: String = store(&repo)
        .query_row(
            "SELECT sha256 FROM artifact WHERE path = ?1",
            [path],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(sha, sha256_hex(b"line\n"));
    let tail = audit_tail(&repo, 2);
    assert!(
        tail.iter()
            .all(|(action, _)| action == "immutable-artifact")
    );
}

#[test]
fn unaccepted_evidence_is_written_once_and_never_replaced_or_deleted() {
    let repo = cut_over("unaccepted-evidence");
    let path = format!("unaccepted-evidence/{DISPATCH}-0001.json");
    let content = canonical(&json!({
        "dispatch_id": DISPATCH,
        "accepted": false,
        "input": {"dispatch_id": DISPATCH},
        "diagnostic": "the batch is held",
        "remedy": "run recovery resume",
    }));
    let evidence = |id: &str| {
        write(
            &repo.state(),
            &request(
                id,
                None,
                None,
                json!([{"op": "create_artifact", "path": path, "content": content}]),
            ),
        )
    };

    evidence("op-1").unwrap();
    evidence("op-2").unwrap();

    let (record, revision) = stored(&repo, &path).unwrap();
    assert_eq!(record["accepted"], json!(false));
    for (id, mutation) in [
        (
            "op-replace",
            json!({"op": "replace", "path": path, "content": content, "expected_revision": revision}),
        ),
        (
            "op-delete",
            json!({"op": "delete", "path": path, "reason": "x", "expected_revision": revision}),
        ),
    ] {
        let error = write(&repo.state(), &request(id, None, None, json!([mutation]))).unwrap_err();
        assert_eq!(code(&error), ErrorCode::InvariantViolation, "{mutation}");
    }
}

#[test]
fn a_json_artifact_outside_the_canonical_form_is_kept_as_its_bytes() {
    let repo = cut_over("artifact-bytes");
    let path = "qa-artifacts/evidence.stage-evidence.json";
    // The keys are in the order of a schema, not sorted: the bytes are what a hash names.
    let text = "{\n  \"schema\": 1,\n  \"candidate\": \"abc\"\n}\n";

    let written = write(
        &repo.state(),
        &request(
            "op-1",
            None,
            None,
            json!([{"op": "create_artifact", "path": path, "content": text}]),
        ),
    )
    .unwrap();

    assert!(written.result["revisions"].get(path).is_none());
    let (bytes, sha): (Vec<u8>, String) = store(&repo)
        .query_row(
            "SELECT bytes, sha256 FROM artifact WHERE path = ?1",
            [path],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .unwrap();
    assert_eq!(bytes, text.as_bytes());
    assert_eq!(sha, sha256_hex(text.as_bytes()));
    assert_eq!(
        state::validate::validate(&repo.state(), true).unwrap()["valid"],
        true
    );
}

#[test]
fn a_write_never_touches_the_legacy_files() {
    let repo = cut_over("legacy-untouched");
    let before: BTreeMap<String, Vec<u8>> = repo
        .tree()
        .into_iter()
        .filter(|(path, _)| !path.starts_with("runtime.db"))
        .collect();

    replace_batch(&repo, "op-1", "#2").unwrap();

    let after: BTreeMap<String, Vec<u8>> = repo
        .tree()
        .into_iter()
        .filter(|(path, _)| !path.starts_with("runtime.db"))
        .collect();
    assert_eq!(before, after);
}

#[test]
fn a_write_to_a_repository_whose_authority_is_the_file_ledger_is_refused() {
    let repo = Repo::new("file-ledger");

    let error = replace_batch_unchecked(&repo).unwrap_err();

    assert_eq!(code(&error), ErrorCode::NotFound);
}

fn replace_batch_unchecked(repo: &Repo) -> Result<state::write::Written, ErrorBody> {
    write(
        &repo.state(),
        &request(
            "op-1",
            None,
            None,
            json!([{"op": "replace", "path": batch_path(),
                    "content": canonical(&json!({"state": "active"})), "expected_revision": 1}]),
        ),
    )
}

#[test]
fn a_read_names_the_revision_of_every_row_that_has_one() {
    let repo = cut_over("revisions");

    let answer = read(
        &repo.state(),
        Some(
            &json!({"collections": ["batches", "dispatch-status", "qa-lane", "reports", "plans"]}),
        ),
        None,
    )
    .unwrap();

    assert_eq!(answer.revisions[&batch_path()], 1);
    assert_eq!(answer.revisions[&status_path(DISPATCH)], 1);
    assert_eq!(answer.revisions["qa-lane/sequence.json"], 1);
    assert!(
        answer
            .revisions
            .get(&format!("plans/{BATCH}.json"))
            .is_none()
    );
}

/// The operational hold of the fixture batch, as the coordinator writes it.
fn hold_record(batch: &str, state: &str) -> String {
    canonical(&json!({
        "batch_id": batch,
        "contract": "operationalHold.v1",
        "failure": null,
        "state": state,
        "updated_at": "2026-10-29T00:00:00+00:00",
    }))
}

fn hold_path(batch: &str) -> String {
    format!("recovery/hold/{batch}.json")
}

#[test]
fn a_recovery_record_of_a_batch_is_created_then_replaced_under_its_revision() {
    let repo = cut_over("recovery-write");
    let intent_path = format!("recovery/intent/{BATCH}.json");
    let intent = canonical(&json!({"batch_id": BATCH, "contract": "recoveryIntent.v1"}));

    let created = write(
        &repo.state(),
        &request(
            "op-1",
            Some(BATCH),
            None,
            json!([
                {"op": "create", "path": hold_path(BATCH), "content": hold_record(BATCH, "held")},
                {"op": "create", "path": intent_path, "content": intent},
            ]),
        ),
    )
    .unwrap();
    let replaced = write(
        &repo.state(),
        &request(
            "op-2",
            Some(BATCH),
            None,
            json!([{"op": "replace", "path": hold_path(BATCH),
                    "content": hold_record(BATCH, "released"), "expected_revision": 1}]),
        ),
    )
    .unwrap();
    let stale = write(
        &repo.state(),
        &request(
            "op-3",
            Some(BATCH),
            None,
            json!([{"op": "replace", "path": hold_path(BATCH),
                    "content": hold_record(BATCH, "held"), "expected_revision": 1}]),
        ),
    )
    .unwrap_err();

    assert_eq!(created.result["revisions"][hold_path(BATCH)], 1);
    assert_eq!(created.result["revisions"][&intent_path], 1);
    assert_eq!(replaced.result["revisions"][hold_path(BATCH)], 2);
    assert_eq!(code(&stale), ErrorCode::RevisionConflict);
    let (record, revision) = stored(&repo, &hold_path(BATCH)).unwrap();
    assert_eq!((record["state"].as_str(), revision), (Some("released"), 2));
    let actions: Vec<String> = audit_tail(&repo, 3).into_iter().map(|(a, _)| a).collect();
    assert_eq!(
        actions,
        ["immutable-record", "immutable-record", "transition"]
    );
}

#[test]
fn a_recovery_record_is_never_deleted_and_names_the_batch_of_its_path() {
    let repo = cut_over("recovery-malformed");
    write(
        &repo.state(),
        &request(
            "op-0",
            Some(BATCH),
            None,
            json!([{"op": "create", "path": hold_path(BATCH), "content": hold_record(BATCH, "held")}]),
        ),
    )
    .unwrap();
    let audit_before = count(&repo, "audit");
    let other_batch = hold_record(OTHER_BATCH, "held");
    let cases = [
        json!({"op": "delete", "path": hold_path(BATCH), "reason": "x", "expected_revision": 1}),
        json!({"op": "create", "path": format!("recovery/note/{BATCH}.json"),
               "content": hold_record(BATCH, "held")}),
        json!({"op": "create", "path": format!("recovery/hold/x/{BATCH}.json"),
               "content": hold_record(BATCH, "held")}),
        json!({"op": "create", "path": format!("recovery/{BATCH}.json"),
               "content": hold_record(BATCH, "held")}),
        json!({"op": "create", "path": format!("recovery/intent/{BATCH}.json"),
               "content": other_batch}),
        json!({"op": "create", "path": format!("recovery/intent/{BATCH}.json"),
               "content": canonical(&json!({"contract": "recoveryIntent.v1"}))}),
        json!({"op": "create_artifact", "path": format!("recovery/intent/{BATCH}.json"),
               "content": hold_record(BATCH, "held")}),
        json!({"op": "create_artifact", "path": "recovery/hold/notes.txt", "content": "x"}),
    ];
    for (index, case) in cases.iter().enumerate() {
        let error = write(
            &repo.state(),
            &request(&format!("op-{}", index + 1), None, None, json!([case])),
        )
        .unwrap_err();
        assert_eq!(code(&error), ErrorCode::MalformedRequest, "{case}");
    }
    assert_eq!(count(&repo, "audit"), audit_before);
    assert_eq!(stored(&repo, &hold_path(BATCH)).unwrap().1, 1);
}

#[test]
fn a_recovery_record_of_a_batch_the_store_does_not_hold_is_not_found() {
    let repo = cut_over("recovery-orphan");
    let audit_before = count(&repo, "audit");

    let error = write(
        &repo.state(),
        &request(
            "op-1",
            None,
            None,
            json!([{"op": "create", "path": hold_path(OTHER_BATCH),
                    "content": hold_record(OTHER_BATCH, "held")}]),
        ),
    )
    .unwrap_err();

    assert_eq!(code(&error), ErrorCode::NotFound);
    assert_eq!(count(&repo, "audit"), audit_before);
    assert!(stored(&repo, &hold_path(OTHER_BATCH)).is_none());
}

#[test]
fn a_recovery_record_belongs_to_its_batch_in_the_one_batch_rule() {
    let repo = cut_over("recovery-one-batch");
    let audit_before = count(&repo, "audit");
    let (record, revision) = replaced_batch(&repo, "T-2");

    let error = write(
        &repo.state(),
        &request(
            "op-1",
            None,
            None,
            json!([
                {"op": "replace", "path": batch_path(), "content": canonical(&record),
                 "expected_revision": revision},
                {"op": "create", "path": hold_path(OTHER_BATCH),
                 "content": hold_record(OTHER_BATCH, "held")},
            ]),
        ),
    )
    .unwrap_err();

    assert_eq!(code(&error), ErrorCode::MalformedRequest);
    assert!(error.message.contains("one batch"), "{}", error.message);
    assert_eq!(count(&repo, "audit"), audit_before);
}
