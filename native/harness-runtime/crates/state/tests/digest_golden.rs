//! The compact digest and the default `json.dumps(sort_keys=True)` text of Rust equal those of
//! Python. `tests/fixtures/digest_golden.json` holds what Python writes; the Python test
//! `tests/orchestration/test_runtime_store_canonical.py` checks the same file against
//! `operational_guards._digest` and `json.dumps`.

use serde_json::Value;
use state::canonical::{compact_json, compact_sha256, sort_key_json};

const GOLDEN: &str = include_str!("fixtures/digest_golden.json");

fn cases() -> Vec<Value> {
    let golden: Value = serde_json::from_str(GOLDEN).unwrap();
    golden["cases"].as_array().unwrap().clone()
}

#[test]
fn the_compact_form_and_its_digest_equal_python_digest() {
    for case in cases() {
        let name = &case["name"];

        assert_eq!(compact_json(&case["value"]), case["compact"], "{name}");
        assert_eq!(
            compact_sha256(&case["value"]),
            case["compact_sha256"],
            "{name}"
        );
    }
}

#[test]
fn the_sort_key_form_escapes_like_python_default_dumps() {
    for case in cases() {
        assert_eq!(
            sort_key_json(&case["value"]),
            case["sort_key"],
            "{}",
            case["name"]
        );
    }
}
