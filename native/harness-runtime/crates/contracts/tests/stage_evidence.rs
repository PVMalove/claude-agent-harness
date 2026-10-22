//! `stage-evidence.v1`: the golden fixture shared with the Python tests, the closed vocabularies
//! against the `systemFields.v1` registry, and the schema's refusals.

use std::collections::BTreeSet;
use std::path::Path;

use contracts::{
    STAGE_EVIDENCE_SCHEMA, StageEvidenceDocument, StageKind, StageResult, StageSelection,
};
use serde_json::{Value, json};

fn read(relative: &str) -> String {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join(relative);
    std::fs::read_to_string(&path)
        .unwrap_or_else(|err| panic!("{} is readable: {err}", path.display()))
}

fn golden() -> String {
    read("tests/fixtures/stage-evidence.v1.json")
}

fn registry_values(field: &str) -> BTreeSet<String> {
    let registry: Value = serde_json::from_str(&read(
        "../../../../harness/orchestration/system_fields.v1.json",
    ))
    .expect("the registry is JSON");
    registry["fields"][field]["values"]
        .as_object()
        .unwrap_or_else(|| panic!("registry field {field} has no values"))
        .keys()
        .cloned()
        .collect()
}

fn rust_values(values: impl IntoIterator<Item = &'static str>) -> BTreeSet<String> {
    values.into_iter().map(str::to_owned).collect()
}

#[test]
fn the_golden_fixture_round_trips_unchanged() {
    let text = golden();
    let document = StageEvidenceDocument::from_json_str(&text).expect("the fixture is valid");

    let written = serde_json::to_value(&document).expect("a document serializes");
    let original: Value = serde_json::from_str(&text).expect("the fixture is JSON");

    assert_eq!(written, original);
    assert_eq!(document.schema, STAGE_EVIDENCE_SCHEMA);
}

#[test]
fn the_golden_fixture_covers_pass_fail_and_not_run() {
    let document = StageEvidenceDocument::from_json_str(&golden()).expect("the fixture is valid");

    let results: Vec<_> = document.stages.iter().map(|stage| stage.result).collect();
    assert_eq!(
        results,
        [StageResult::Pass, StageResult::Fail, StageResult::NotRun]
    );
    let failed = &document.stages[1];
    let (stdout, stderr) = (failed.stdout.as_ref(), failed.stderr.as_ref());
    assert_ne!(
        stdout.expect("a run has stdout").sha256,
        stderr.expect("a run has stderr").sha256
    );
    assert_eq!(failed.exit_code, Some(1));
    assert!(!failed.failed_nodeids.is_empty());
}

#[test]
fn the_vocabularies_match_the_registry() {
    assert_eq!(
        rust_values(StageResult::ALL.into_iter().map(StageResult::as_str)),
        registry_values("report.check_result")
    );
    assert_eq!(
        rust_values(StageSelection::ALL.into_iter().map(StageSelection::as_str)),
        registry_values("verification_plan.selection")
    );
    assert_eq!(
        rust_values(StageKind::ALL.into_iter().map(StageKind::as_str)),
        registry_values("verification_plan.stage_kind")
    );
}

#[test]
fn json_spelling_equals_the_machine_value() {
    for value in StageResult::ALL {
        assert_eq!(serde_json::to_value(value).unwrap(), json!(value.as_str()));
    }
    for value in StageSelection::ALL {
        assert_eq!(serde_json::to_value(value).unwrap(), json!(value.as_str()));
    }
    for value in StageKind::ALL {
        assert_eq!(serde_json::to_value(value).unwrap(), json!(value.as_str()));
    }
}

fn refused(edit: impl FnOnce(&mut Value)) -> bool {
    let mut document: Value = serde_json::from_str(&golden()).expect("the fixture is JSON");
    edit(&mut document);
    StageEvidenceDocument::from_json_str(&document.to_string()).is_err()
}

#[test]
fn a_missing_or_unknown_key_is_refused() {
    assert!(refused(|d| d["note"] = json!("x")));
    assert!(refused(|d| d["stages"][0]["stdout_text"] = json!("inline")));
    for key in [
        "stage_id",
        "kind",
        "selection",
        "candidate_sha",
        "plan_sha256",
        "image_digest",
        "environment_fingerprint",
        "result",
        "exit_code",
        "stdout",
        "stderr",
        "duration_ms",
        "failed_nodeids",
    ] {
        assert!(
            refused(|d| {
                d["stages"][0].as_object_mut().unwrap().remove(key);
            }),
            "a stage without {key} must be refused"
        );
    }
}

#[test]
fn an_invalid_value_is_refused() {
    assert!(refused(|d| d["schema"] = json!("stage-evidence.v2")));
    assert!(refused(|d| d["stages"][0]["result"] = json!("passed")));
    assert!(refused(|d| d["stages"][0]["kind"] = json!("unit")));
    assert!(refused(
        |d| d["stages"][0]["selection"] = json!("failed_only")
    ));
    assert!(refused(
        |d| d["stages"][0]["candidate_sha"] = json!("not-a-sha")
    ));
    assert!(refused(|d| d["stages"][0]["plan_sha256"] = json!("ABC")));
    assert!(refused(|d| d["stages"][0]["duration_ms"] = json!(-1)));
    assert!(refused(|d| d["stages"][0]["duration_ms"] = json!(1.5)));
    assert!(refused(|d| d["stages"][0]["stage_id"] = json!("")));
    assert!(refused(
        |d| d["stages"][0]["stdout"] = json!({"path": "x", "sha256": "short"})
    ));
}

#[test]
fn a_result_must_agree_with_its_exit_code_streams_and_failed_nodeids() {
    assert!(refused(|d| d["stages"][0]["exit_code"] = json!(3)));
    assert!(refused(|d| d["stages"][1]["exit_code"] = json!(0)));
    assert!(refused(
        |d| d["stages"][0]["failed_nodeids"] = json!(["tests/a.py::t"])
    ));
    assert!(refused(|d| d["stages"][2]["exit_code"] = json!(0)));
    assert!(refused(
        |d| d["stages"][2]["failed_nodeids"] = json!(["tests/a.py::t"])
    ));
    assert!(refused(|d| d["stages"][1]["stdout"] = Value::Null));
}

#[test]
fn a_not_run_stage_may_carry_the_exit_code_of_a_run_that_selected_nothing() {
    let mut document: Value = serde_json::from_str(&golden()).expect("the fixture is JSON");
    document["stages"][1]["result"] = json!("not_run");
    document["stages"][1]["exit_code"] = json!(5);
    document["stages"][1]["failed_nodeids"] = json!([]);

    let parsed = StageEvidenceDocument::from_json_str(&document.to_string());

    assert_eq!(parsed.expect("valid").stages[1].exit_code, Some(5));
}

#[test]
fn an_exit_code_follows_the_shared_32_bit_range_of_the_python_writer() {
    for code in [-(1_i64 << 31), -1, 3_221_225_477, (1_i64 << 32) - 1] {
        let mut document: Value = serde_json::from_str(&golden()).expect("the fixture is JSON");
        document["stages"][1]["exit_code"] = json!(code);

        let parsed = StageEvidenceDocument::from_json_str(&document.to_string());

        assert_eq!(parsed.expect("in range").stages[1].exit_code, Some(code));
    }
    for code in [-(1_i64 << 31) - 1, 1_i64 << 32, i64::MAX] {
        assert!(refused(|d| d["stages"][1]["exit_code"] = json!(code)));
    }
}
