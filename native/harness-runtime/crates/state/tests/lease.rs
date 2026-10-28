//! The QA lease of the store: fencing tokens, the owner's identity and the reap of a dead owner
//! (ADR 0020, sections 3 and 7; ADR 0021, section 5).

mod common;

use contracts::{ErrorBody, ErrorCode, PROTOCOL_VERSION, Request};
use rusqlite::Connection;
use serde_json::{Value, json};
use state::migrate::run;
use state::read::read;
use state::write::{Written, write_for};

use common::Repo;

const LEASE: &str = "qa-lane/lease.json";

fn cut_over(name: &str) -> Repo {
    let repo = Repo::new(name);
    run(&repo.root).unwrap();
    repo
}

fn request(id: &str, mutation: Value) -> Request {
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
        payload: Some(json!({"mutations": [mutation]})),
    }
}

fn owner(id: &str) -> Value {
    json!({"owner_kind": "dispatch", "owner_id": id})
}

fn with(mut lease: Value, key: &str, value: Value) -> Value {
    lease[key] = value;
    lease
}

struct Lane<'a> {
    repo: &'a Repo,
    next: std::cell::Cell<u32>,
}

impl<'a> Lane<'a> {
    fn of(repo: &'a Repo) -> Self {
        Self {
            repo,
            next: std::cell::Cell::new(0),
        }
    }

    fn send(&self, mutation: Value, peer: Option<u32>) -> Result<Written, ErrorBody> {
        let n = self.next.get() + 1;
        self.next.set(n);
        write_for(
            &self.repo.state(),
            &request(&format!("op-{n}"), mutation),
            peer,
        )
    }

    fn acquire(&self, id: &str, peer: Option<u32>) -> Result<Written, ErrorBody> {
        self.send(
            json!({"op": "lease_acquire", "path": LEASE,
                   "lease": with(owner(id), "ttl_seconds", json!(300))}),
            peer,
        )
    }

    fn named(&self, op: &str, id: &str, token: i64) -> Result<Written, ErrorBody> {
        let mut mutation = json!({"op": op, "path": LEASE,
                                  "lease": with(owner(id), "fencing_token", json!(token))});
        if op == "lease_heartbeat" {
            mutation["lease"]["ttl_seconds"] = json!(300);
        } else {
            mutation["reason"] = json!(format!("{op} in a test"));
        }
        self.send(mutation, None)
    }

    fn clear(&self, id: &str, token: i64, approved_by: &str) -> Result<Written, ErrorBody> {
        self.send(
            json!({"op": "lease_clear", "path": LEASE, "reason": "the worker sandbox stopped",
                   "lease": with(owner(id), "fencing_token", json!(token)),
                   "decision": {"approved_by": approved_by, "approved_at": "2026-10-28T00:00:00+00:00"}}),
            None,
        )
    }

    fn store(&self) -> Connection {
        Connection::open(self.repo.state().join("runtime.db")).unwrap()
    }

    fn expire(&self) {
        self.store()
            .execute(
                "UPDATE qa_lane SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = 1",
                [],
            )
            .unwrap();
    }

    fn set(&self, column: &str, value: &str) {
        self.store()
            .execute(
                &format!("UPDATE qa_lane SET {column} = ?1 WHERE id = 1"),
                [value],
            )
            .unwrap();
    }

    fn row(&self) -> (i64, Option<String>) {
        self.store()
            .query_row(
                "SELECT fencing_token, owner_id FROM qa_lane WHERE id = 1",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap()
    }

    fn last_audit(&self) -> (String, Value) {
        self.store()
            .query_row(
                "SELECT action, details_json, at FROM audit ORDER BY seq DESC LIMIT 1",
                [],
                |row| {
                    let details: String = row.get(1)?;
                    let mut details: Value = serde_json::from_str(&details).unwrap();
                    details["at"] = json!(row.get::<_, String>(2)?);
                    Ok((row.get(0)?, details))
                },
            )
            .unwrap()
    }
}

fn token(written: &Written) -> i64 {
    written.result["lease"]["fencing_token"].as_i64().unwrap()
}

fn sleeper() -> std::process::Child {
    #[cfg(unix)]
    let child = std::process::Command::new("sleep").arg("30").spawn();
    #[cfg(windows)]
    let child = std::process::Command::new("ping")
        .args(["-n", "30", "127.0.0.1"])
        .spawn();
    child.unwrap()
}

#[test]
fn an_acquired_lease_is_projected_with_its_token_and_the_peer_identity() {
    let repo = cut_over("acquire");
    let lane = Lane::of(&repo);

    let written = lane.acquire("d1", Some(std::process::id())).unwrap();

    let lease = &written.result["lease"];
    assert_eq!(lease["owner_id"], "d1");
    assert_eq!(lease["fencing_token"], 1);
    assert_eq!(written.touched, ["qa_lane", "generation"]);
    #[cfg(any(target_os = "linux", windows))]
    {
        assert_eq!(lease["identity"]["pid"], std::process::id());
        assert!(lease["identity"]["start_time"].is_string());
    }
    let answer = read(&repo.state(), Some(&json!({"paths": [LEASE]})), None).unwrap();
    assert_eq!(&answer.records[LEASE], lease);
    let (action, details) = lane.last_audit();
    assert_eq!(action, "immutable-record");
    assert_eq!(details["lease_op"], "acquire");
    assert_eq!(details["fencing_token"], 1);
}

#[test]
fn a_held_lane_refuses_a_second_acquire_with_lease_held() {
    let repo = cut_over("held");
    let lane = Lane::of(&repo);
    lane.acquire("d1", None).unwrap();

    let error = lane.acquire("d2", None).unwrap_err();

    assert_eq!(error.code, ErrorCode::LeaseHeld);
    assert_eq!(error.facts["owner"], owner("d1"));
    assert_eq!(error.facts["stale_heartbeat"], false);
}

#[test]
fn the_fencing_token_grows_through_acquire_release_reap_clear_and_a_new_connection() {
    let repo = cut_over("monotonic");
    let lane = Lane::of(&repo);
    let mut tokens = Vec::new();

    let first = lane.acquire("d1", None).unwrap();
    tokens.push(token(&first));
    lane.named("lease_release", "d1", token(&first)).unwrap();
    assert_eq!(lane.row(), (1, None));

    let mut child = sleeper();
    let second = lane.acquire("d2", Some(child.id())).unwrap();
    tokens.push(token(&second));
    child.kill().unwrap();
    child.wait().unwrap();
    lane.expire();
    #[cfg(any(target_os = "linux", windows))]
    lane.named("lease_reap", "d2", token(&second)).unwrap();
    #[cfg(not(any(target_os = "linux", windows)))]
    lane.clear("d2", token(&second), "operator").unwrap();

    let third = lane.acquire("d3", None).unwrap();
    tokens.push(token(&third));
    lane.clear("d3", token(&third), "operator").unwrap();

    // Another lane over the same store: the token lives in the row, not in the process.
    let again = Lane::of(&repo);
    again.next.set(100);
    let fourth = again.acquire("d4", None).unwrap();
    tokens.push(token(&fourth));

    assert_eq!(tokens, [1, 2, 3, 4]);
    // Every lease operation is an ordinary audit record of its path: the full check agrees.
    assert_eq!(
        state::validate::validate(&repo.state(), true).unwrap()["valid"],
        true
    );
}

#[test]
fn a_foreign_owner_or_token_is_a_fencing_mismatch_that_changes_nothing() {
    let repo = cut_over("fencing");
    let lane = Lane::of(&repo);
    let held = lane.acquire("d1", None).unwrap();
    let before = lane.row();

    for (op, id, token) in [
        ("lease_release", "d1", token(&held) + 1),
        ("lease_release", "d2", token(&held)),
        ("lease_heartbeat", "d1", token(&held) + 1),
        ("lease_reap", "d2", token(&held)),
    ] {
        let error = lane.named(op, id, token).unwrap_err();
        assert_eq!(error.code, ErrorCode::FencingMismatch, "{op} {id} {token}");
    }
    assert_eq!(lane.row(), before);
}

#[test]
fn a_lease_cleared_by_an_operator_makes_the_old_owner_release_a_fencing_mismatch() {
    let repo = cut_over("cleared");
    let lane = Lane::of(&repo);
    let held = lane.acquire("d1", None).unwrap();
    assert_eq!(
        lane.clear("d1", token(&held), "policy:auto")
            .unwrap_err()
            .code,
        ErrorCode::PolicyDenied
    );

    lane.clear("d1", token(&held), "operator").unwrap();

    let (action, details) = lane.last_audit();
    assert_eq!(action, "deletion");
    assert_eq!(details["lease_op"], "clear");
    assert_eq!(details["decision"]["approved_by"], "operator");
    assert!(details["verdict"].is_string());
    let error = lane.named("lease_release", "d1", token(&held)).unwrap_err();
    assert_eq!(error.code, ErrorCode::FencingMismatch);
}

#[test]
fn a_heartbeat_moves_the_expiry_and_keeps_the_token() {
    let repo = cut_over("heartbeat");
    let lane = Lane::of(&repo);
    let held = lane.acquire("d1", None).unwrap();
    lane.expire();

    let renewed = lane.named("lease_heartbeat", "d1", token(&held)).unwrap();

    assert_eq!(token(&renewed), token(&held));
    assert_ne!(
        renewed.result["lease"]["expires_at"],
        "2000-01-01T00:00:00+00:00"
    );
    let (action, details) = lane.last_audit();
    assert_eq!(action, "transition");
    assert_eq!(details["lease_op"], "heartbeat");
}

#[test]
fn a_lease_that_has_not_expired_is_not_reaped() {
    let repo = cut_over("fresh");
    let lane = Lane::of(&repo);
    let held = lane.acquire("d1", None).unwrap();

    let error = lane.named("lease_reap", "d1", token(&held)).unwrap_err();

    assert_eq!(error.code, ErrorCode::LeaseHeld);
    assert_eq!(error.facts["stale_heartbeat"], false);
}

#[test]
fn an_owner_without_an_identity_is_never_reaped() {
    let repo = cut_over("unknown");
    let lane = Lane::of(&repo);
    let held = lane.acquire("d1", None).unwrap();
    lane.expire();

    let error = lane.named("lease_reap", "d1", token(&held)).unwrap_err();

    assert_eq!(error.code, ErrorCode::LeaseHeld);
    assert_eq!(error.facts["verdict"], "owner-unknown");
    assert_eq!(error.facts["stale_heartbeat"], true);
    assert_eq!(lane.row(), (1, Some("d1".to_owned())));
}

/// A lease that schema version 2 held has no identity and the token 0: it is never reaped, and an
/// operator clears it by that token.
#[test]
fn a_lease_of_schema_version_two_is_never_reaped_and_an_operator_clears_it() {
    let repo = cut_over("version-two");
    let lane = Lane::of(&repo);
    let old = "2000-01-01T00:00:00+00:00";
    lane.store()
        .execute(
            "INSERT INTO qa_lane (id, revision, fencing_token, body_json, owner_kind, owner_id,
                                  acquired_at, expires_at, heartbeat_at)
             VALUES (1, 1, 0, '{}', 'dispatch', 'd1', ?1, ?1, ?1)",
            [old],
        )
        .unwrap();

    let error = lane.named("lease_reap", "d1", 0).unwrap_err();
    assert_eq!(error.code, ErrorCode::LeaseHeld);
    assert_eq!(error.facts["verdict"], "owner-unknown");
    lane.clear("d1", 0, "operator").unwrap();

    assert_eq!(lane.row(), (0, None));
    assert_eq!(token(&lane.acquire("d2", None).unwrap()), 1);
}

#[cfg(any(target_os = "linux", windows))]
mod processes {
    use super::*;

    /// Live, reused-pid, other-namespace and other-boot owners keep an expired lease: the reap
    /// answers `lease_held` with the stale heartbeat and the verdict, and changes nothing.
    /// A name, the lane column a test changes with its value, and the verdict.
    type Case = (
        &'static str,
        Option<(&'static str, &'static str)>,
        &'static str,
    );

    #[test]
    fn an_owner_that_is_not_provably_dead_keeps_its_expired_lease() {
        let mut child = sleeper();
        let cases: [Case; 4] = [
            ("live", None, "owner-alive"),
            ("reused", Some(("start_time", "1")), "owner-pid-reused"),
            (
                "namespace",
                Some(("pid_namespace", "pid:[1]")),
                "owner-in-other-environment",
            ),
            (
                "boot",
                Some(("boot_id", "another-boot")),
                "owner-in-other-environment",
            ),
        ];
        for (name, change, verdict) in cases {
            let repo = cut_over(name);
            let lane = Lane::of(&repo);
            let held = lane.acquire("d1", Some(child.id())).unwrap();
            lane.expire();
            if let Some((column, value)) = change {
                lane.set(column, value);
            }

            let error = lane.named("lease_reap", "d1", token(&held)).unwrap_err();

            assert_eq!(error.code, ErrorCode::LeaseHeld, "{name}");
            assert_eq!(error.facts["verdict"], verdict, "{name}");
            assert_eq!(error.facts["stale_heartbeat"], true, "{name}");
            assert_eq!(error.facts["owner"], owner("d1"), "{name}");
            assert!(error.facts["expires_at"].is_string(), "{name}");
            assert!(error.facts["heartbeat_at"].is_string(), "{name}");
            assert_eq!(lane.row(), (1, Some("d1".to_owned())), "{name}");
        }
        child.kill().unwrap();
        child.wait().unwrap();
    }

    #[test]
    fn a_killed_owner_is_reaped_with_the_evidence_of_its_death() {
        let repo = cut_over("killed");
        let lane = Lane::of(&repo);
        let mut child = sleeper();
        let held = lane.acquire("d1", Some(child.id())).unwrap();
        child.kill().unwrap();
        child.wait().unwrap();
        lane.expire();

        let reaped = lane.named("lease_reap", "d1", token(&held)).unwrap();

        assert!(reaped.result["lease"].is_null());
        assert_eq!(lane.row(), (1, None));
        let (action, details) = lane.last_audit();
        assert_eq!(action, "deletion");
        assert_eq!(details["path"], LEASE);
        assert_eq!(details["lease_op"], "reap");
        assert_eq!(details["verdict"], "owner-dead");
        assert_eq!(details["owner"], owner("d1"));
        assert_eq!(details["fencing_token"], 1);
        assert_eq!(details["identity"]["pid"], child.id());
        assert_eq!(details["observed"]["process"]["state"], "absent");
        assert!(details["at"].is_string());
        // The lane is free again for the next owner.
        assert_eq!(token(&lane.acquire("d2", None).unwrap()), 2);
    }
}
