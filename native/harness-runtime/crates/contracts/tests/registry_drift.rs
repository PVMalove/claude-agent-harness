//! The Rust vocabularies against the `systemFields.v1` registry: any difference fails.

use std::collections::BTreeSet;
use std::path::Path;

use contracts::{
    ErrorCode, ExecutionState, FailureDomain, OperationState, QaEvidenceVerdict,
    QaEvidenceViolation,
};
use serde_json::Value;

fn registry_values(field: &str) -> BTreeSet<String> {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../../harness/orchestration/system_fields.v1.json");
    let text = std::fs::read_to_string(&path).expect("the registry file is readable");
    let registry: Value = serde_json::from_str(&text).expect("the registry is JSON");
    registry["fields"][field]["values"]
        .as_object()
        .unwrap_or_else(|| panic!("registry field {field} has no values"))
        .keys()
        .cloned()
        .collect()
}

fn rust_values<I: IntoIterator<Item = &'static str>>(values: I) -> BTreeSet<String> {
    values.into_iter().map(str::to_owned).collect()
}

#[test]
fn error_codes_match_the_registry() {
    let rust = rust_values(ErrorCode::ALL.into_iter().map(ErrorCode::as_str));

    assert_eq!(rust, registry_values("runtime.error_code"));
}

#[test]
fn failure_domains_match_the_registry() {
    let rust = rust_values(FailureDomain::ALL.into_iter().map(FailureDomain::as_str));

    assert_eq!(rust, registry_values("runtime.failure_domain"));
}

#[test]
fn operation_states_match_the_registry() {
    let rust = rust_values(OperationState::ALL.into_iter().map(OperationState::as_str));

    assert_eq!(rust, registry_values("runtime.operation_state"));
}

#[test]
fn execution_states_match_the_registry() {
    let rust = rust_values(ExecutionState::ALL.into_iter().map(ExecutionState::as_str));

    assert_eq!(rust, registry_values("runtime.execution_state"));
}

#[test]
fn qa_evidence_verdicts_match_the_registry() {
    let rust = rust_values(
        QaEvidenceVerdict::ALL
            .into_iter()
            .map(QaEvidenceVerdict::as_str),
    );

    assert_eq!(rust, registry_values("qa_evidence.verdict"));
}

#[test]
fn qa_evidence_violations_match_the_registry() {
    let rust = rust_values(
        QaEvidenceViolation::ALL
            .into_iter()
            .map(QaEvidenceViolation::as_str),
    );

    assert_eq!(rust, registry_values("qa_evidence.violation"));
}

#[test]
fn json_spelling_equals_the_machine_value() {
    for state in OperationState::ALL {
        let json = serde_json::to_string(&state).expect("a state serializes");
        assert_eq!(json, format!("\"{}\"", state.as_str()));
    }
    for state in ExecutionState::ALL {
        let json = serde_json::to_string(&state).expect("a state serializes");
        assert_eq!(json, format!("\"{}\"", state.as_str()));
    }
    for code in ErrorCode::ALL {
        let json = serde_json::to_string(&code).expect("a code serializes");
        assert_eq!(json, format!("\"{}\"", code.as_str()));
    }
    for domain in FailureDomain::ALL {
        let json = serde_json::to_string(&domain).expect("a domain serializes");
        assert_eq!(json, format!("\"{}\"", domain.as_str()));
    }
    for verdict in QaEvidenceVerdict::ALL {
        let json = serde_json::to_string(&verdict).expect("a verdict serializes");
        assert_eq!(json, format!("\"{}\"", verdict.as_str()));
    }
    for violation in QaEvidenceViolation::ALL {
        let json = serde_json::to_string(&violation).expect("a violation serializes");
        assert_eq!(json, format!("\"{}\"", violation.as_str()));
    }
}
