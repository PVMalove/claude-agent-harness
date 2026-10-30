//! The hook policy digest of the runtime equals the Python `hook_policy_sha256` of
//! `normalize_policy` (ADR 0024, section 4). `tests/fixtures/hook_policy_golden.json` holds what
//! Python computes; `tests/orchestration/test_hook_policy.py` checks the same file.

use serde_json::Value;
use supervisor::hook_policy;

const GOLDEN: &str = include_str!("fixtures/hook_policy_golden.json");

fn golden() -> Value {
    serde_json::from_str(GOLDEN).unwrap()
}

#[test]
fn the_normalized_policy_and_its_digest_equal_python() {
    for case in golden()["normalized"].as_array().unwrap() {
        let name = &case["name"];

        let policy = hook_policy::normalize(case.get("policy")).unwrap();

        assert_eq!(policy, case["normalized"], "{name}");
        assert_eq!(hook_policy::sha256(&policy), case["sha256"], "{name}");
    }
}

#[test]
fn a_shape_python_refuses_is_refused() {
    for value in golden()["refused"].as_array().unwrap() {
        assert!(
            hook_policy::normalize(Some(value)).is_err(),
            "{value} is not refused"
        );
    }
}

#[test]
fn the_escalation_order_follows_the_python_sort_key_not_utf8() {
    let golden = golden();
    let case = golden["normalized"]
        .as_array()
        .unwrap()
        .iter()
        .find(|case| case["name"] == "non-ascii-and-astral-paths")
        .unwrap();
    let mut reversed = case["policy"].clone();
    reversed["escalations"].as_array_mut().unwrap().reverse();

    let policy = hook_policy::normalize(Some(&reversed)).unwrap();

    assert_eq!(policy, case["normalized"]);
}
