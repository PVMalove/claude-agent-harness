//! The stored evidence of an accepted QA run of one Release Gate pair, as the Python coordinator
//! writes it: the QA dispatch and its batch entry, the report that links its log by
//! `sha256:<hex>`, the log whose trailer links the `stage-evidence.v1` document, the output
//! streams, and the pair record. The tests of `qa.evidence.verify` in the state crate and the
//! IPC server share it.

#![allow(dead_code)]

use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

use contracts::{
    FileRef, StageEvidence, StageEvidenceDocument, StageKind, StageResult, StageSelection,
};
use serde_json::{Map, Value, json};
use state::canonical::{canonical_json, object_sha256, sha256_hex};

/// The batch of the legacy fixture, whose `base_commit` is the target of the pair.
pub const BATCH: &str = "batch-aaaaaaaa-0000-4000-8000-000000000001";
pub const TARGET: &str = "1111111111111111111111111111111111111111";
pub const CANDIDATE: &str = "c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0";
pub const TREE: &str = "7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e";
pub const QA_DISPATCH: &str = "dispatch-33333333-0000-4000-8000-000000000003";
pub const COMMANDS: [&str; 2] = ["make lint", "make test"];
const STDOUT: &str = "all checks passed\n";
const STDERR: &str = "";

/// The stored form of one file: its path relative to the generation and its bytes.
pub type Files = BTreeMap<String, Vec<u8>>;

fn stream(kind: &str, text: &str) -> FileRef {
    let sha256 = sha256_hex(text.as_bytes());
    FileRef {
        path: format!("qa-artifacts/{sha256}.{kind}.txt"),
        sha256,
    }
}

/// A full, passed stage of the candidate with stored output streams.
pub fn passed_stage(stage_id: &str, candidate: &str) -> StageEvidence {
    StageEvidence {
        stage_id: stage_id.to_owned(),
        kind: StageKind::Custom,
        selection: StageSelection::Full,
        candidate_sha: candidate.to_owned(),
        plan_sha256: None,
        image_digest: None,
        environment_fingerprint: Some("e".repeat(64)),
        result: StageResult::Pass,
        exit_code: Some(0),
        stdout: Some(stream("stdout", STDOUT)),
        stderr: Some(stream("stderr", STDERR)),
        duration_ms: 10,
        failed_nodeids: Vec::new(),
    }
}

/// The stages of a passed run of every approved command.
pub fn passed_stages() -> Vec<StageEvidence> {
    (1..=COMMANDS.len())
        .map(|n| passed_stage(&format!("gate-{n}"), CANDIDATE))
        .collect()
}

/// The text Python writes for a stage-evidence document (`StageEvidenceDocument.dumps`).
pub fn document_text(stages: Vec<StageEvidence>) -> String {
    let document = StageEvidenceDocument::new(stages);
    format!("{}\n", serde_json::to_string_pretty(&document).unwrap())
}

/// The canonical text of a record.
pub fn record_text(record: &Value) -> Vec<u8> {
    canonical_json(record).into_bytes()
}

fn object(value: &Value) -> &Map<String, Value> {
    value.as_object().unwrap()
}

/// The pair record of `{CANDIDATE, TARGET}`, as `release_gate.record_pair` writes it.
pub fn pair_record() -> (String, Value) {
    let members = json!({"candidate_sha": CANDIDATE, "target_sha": TARGET});
    let id = format!(
        "pair-{}",
        &sha256_hex(canonical_json(&members).as_bytes())[..32]
    );
    let record = json!({
        "pair_id": id,
        "contract": 1,
        "candidate_sha": CANDIDATE,
        "target_sha": TARGET,
        "candidate_tree_sha": TREE,
    });
    (format!("reports/integration-pair/{id}.json"), record)
}

/// The files of a QA log with its trailer, its stage-evidence document and the streams; the
/// SHA-256 of the log.
pub fn run_files(stages: Vec<StageEvidence>, files: &mut Files) -> String {
    let document = document_text(stages);
    let document_sha = sha256_hex(document.as_bytes());
    let document_path = format!("qa-artifacts/{document_sha}.stage-evidence.json");
    let mut log = String::new();
    for command in COMMANDS {
        log.push_str(&format!("$ {command}\nexit_code=0\n{STDOUT}\n"));
    }
    log.push_str(&format!(
        "stage-evidence: {document_path} sha256:{document_sha}\n"
    ));
    let log_sha = sha256_hex(log.as_bytes());
    files.insert(format!("qa-artifacts/{log_sha}.log"), log.into_bytes());
    files.insert(document_path, document.into_bytes());
    for (kind, text) in [("stdout", STDOUT), ("stderr", STDERR)] {
        files.insert(stream(kind, text).path, text.as_bytes().to_vec());
    }
    log_sha
}

/// An accepted QA run of the pair: the files it stores and the entry of its batch.
pub struct Run {
    pub files: Files,
    pub entry: Value,
}

/// The stored evidence of an accepted QA dispatch on `CANDIDATE` whose run has `stages`.
pub fn accepted_qa(stages: Vec<StageEvidence>) -> Run {
    let mut files = Files::new();
    let log_sha = run_files(stages, &mut files);
    let dispatch = json!({
        "batch_id": BATCH,
        "dispatch_id": QA_DISPATCH,
        "role": "qa",
        "state": "approved",
        "candidate_commit": CANDIDATE,
        "verification_commands": COMMANDS,
    });
    let report = json!({
        "dispatch_id": QA_DISPATCH,
        "role": "qa",
        "outcome": "completed",
        "output": format!(
            "QA gate passed; full sanitised output: /repo/.harness/orchestration/state/\
             qa-artifacts/{log_sha}.log (sha256:{log_sha})"
        ),
        "checks_run": COMMANDS
            .iter()
            .map(|command| json!({"command": command, "result": "pass", "evidence": "ok"}))
            .collect::<Vec<_>>(),
    });
    let report_path = format!("reports/{QA_DISPATCH}.json");
    let entry = json!({
        "dispatch_id": QA_DISPATCH,
        "role": "qa",
        "state": "reported",
        "decision": {"decision": "accept"},
        "brief_sha256": object_sha256(object(&dispatch)),
        "report": report_path,
        "report_sha256": object_sha256(object(&report)),
    });
    files.insert(
        format!("dispatches/{QA_DISPATCH}.json"),
        record_text(&dispatch),
    );
    files.insert(
        format!("dispatch-status/{QA_DISPATCH}.json"),
        record_text(&json!({"dispatch_id": QA_DISPATCH, "state": "reported"})),
    );
    files.insert(report_path, record_text(&report));
    let (pair_path, pair) = pair_record();
    files.insert(pair_path, record_text(&pair));
    Run { files, entry }
}

/// Write `run` into the legacy generation `generation` of the fixture, before its import: its
/// files, and its entry appended to the fixture batch.
pub fn write_legacy(generation: &Path, run: &Run) {
    for (path, bytes) in &run.files {
        let target = generation.join(path);
        fs::create_dir_all(target.parent().unwrap()).unwrap();
        fs::write(target, bytes).unwrap();
    }
    let batch_path = generation.join(format!("batches/{BATCH}.json"));
    let mut batch: Value = serde_json::from_slice(&fs::read(&batch_path).unwrap()).unwrap();
    batch["dispatches"]
        .as_array_mut()
        .unwrap()
        .push(run.entry.clone());
    fs::write(batch_path, record_text(&batch)).unwrap();
}
