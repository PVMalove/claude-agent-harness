//! `qa.evidence.verify` (`qa_evidence_verify` of ADR 0021, section 7): the verdict on the stored
//! QA evidence of a Release Gate pair and its violations (ADR 0023, section 5; ADR 0026,
//! section 4), on evidence held in memory and on a real store.

mod common;

use std::collections::BTreeMap;

use common::Repo;
use common::qa_store::{
    self, BATCH, CANDIDATE, COMMANDS, QA_DISPATCH, Run, TARGET, TREE, accepted_qa, passed_stage,
    passed_stages,
};
use contracts::{StageEvidence, StageResult, StageSelection};
use serde_json::{Map, Value, json};
use state::canonical::{canonical_json, sha256_hex};
use state::qa_evidence::{self, Evidence, Pair};
use state::read::{ReadError, read};

/// Evidence held in memory: JSON records by path and the bytes of the artifacts.
struct Memory {
    records: Map<String, Value>,
    artifacts: BTreeMap<String, Vec<u8>>,
}

impl Evidence for Memory {
    fn records(&self) -> &Map<String, Value> {
        &self.records
    }

    fn artifact(&self, path: &str) -> Result<Option<Vec<u8>>, ReadError> {
        Ok(self.artifacts.get(path).cloned())
    }
}

impl Memory {
    /// The store of `run`: the batch of the fixture with its entry, and its files.
    fn of(run: &Run) -> Self {
        let mut memory = Self {
            records: Map::new(),
            artifacts: BTreeMap::new(),
        };
        memory.records.insert(
            format!("batches/{BATCH}.json"),
            json!({"batch_id": BATCH, "base_commit": TARGET, "dispatches": [run.entry]}),
        );
        for (path, bytes) in &run.files {
            if path.starts_with("qa-artifacts/") {
                memory.artifacts.insert(path.clone(), bytes.clone());
            } else {
                let record = serde_json::from_slice(bytes).unwrap();
                memory.records.insert(path.clone(), record);
            }
        }
        memory
    }

    fn passed() -> Self {
        Self::of(&accepted_qa(passed_stages()))
    }

    fn without_qa() -> Self {
        let mut memory = Self::passed();
        memory.records = memory
            .records
            .into_iter()
            .filter(|(path, _)| path.starts_with("reports/integration-pair/"))
            .collect();
        memory
    }
}

fn pair() -> Pair {
    Pair {
        candidate_sha: CANDIDATE.to_owned(),
        target_sha: TARGET.to_owned(),
    }
}

fn judged(memory: &Memory) -> Value {
    qa_evidence::judge(memory, &pair()).unwrap()
}

fn codes(answer: &Value) -> Vec<String> {
    answer["violations"]
        .as_array()
        .unwrap()
        .iter()
        .map(|violation| violation["code"].as_str().unwrap().to_owned())
        .collect()
}

fn refused_with(answer: &Value, code: &str) {
    assert_eq!(answer["verdict"], "refused", "{answer}");
    assert!(codes(answer).contains(&code.to_owned()), "{answer}");
    assert_eq!(answer["confirmed_by"], Value::Null);
}

fn with_stages(stages: Vec<StageEvidence>) -> Value {
    judged(&Memory::of(&accepted_qa(stages)))
}

#[test]
fn a_full_passed_qa_run_of_exactly_the_pair_is_ok() {
    let answer = judged(&Memory::passed());

    assert_eq!(answer["verdict"], "ok", "{answer}");
    assert_eq!(answer["violations"], json!([]));
    assert_eq!(
        answer["pair"],
        json!({"candidate_sha": CANDIDATE, "target_sha": TARGET, "candidate_tree_sha": TREE})
    );
    assert_eq!(answer["confirmed_by"]["source"], "original-qa");
    assert_eq!(
        answer["confirmed_by"]["evidence"],
        format!("reports/{QA_DISPATCH}.json")
    );
}

#[test]
fn a_pair_without_its_record_is_refused() {
    let mut memory = Memory::passed();
    memory
        .records
        .retain(|path, _| !path.starts_with("reports/integration-pair/"));

    refused_with(&judged(&memory), "pair_not_recorded");
}

#[test]
fn a_pair_without_any_qa_run_is_no_full_qa() {
    let answer = judged(&Memory::without_qa());

    refused_with(&answer, "no_full_qa");
    assert_eq!(codes(&answer), ["no_full_qa"]);
}

#[test]
fn passed_developer_or_review_checks_are_a_cache_and_never_confirm_the_pair() {
    let mut memory = Memory::without_qa();
    for role in ["developer", "code-review"] {
        let id = format!("dispatch-{role}");
        memory.records.insert(
            format!("dispatches/{id}.json"),
            json!({"dispatch_id": id, "role": role, "candidate_commit": CANDIDATE,
                   "verification_commands": COMMANDS}),
        );
        memory.records.insert(
            format!("reports/{id}.json"),
            json!({"outcome": "completed", "checks_run": COMMANDS.iter()
                .map(|command| json!({"command": command, "result": "pass"}))
                .collect::<Vec<_>>()}),
        );
    }

    refused_with(&judged(&memory), "no_full_qa");
}

#[test]
fn a_qa_run_of_another_target_does_not_confirm_the_pair() {
    let answer = qa_evidence::judge(
        &Memory::passed(),
        &Pair {
            candidate_sha: CANDIDATE.to_owned(),
            target_sha: "2".repeat(40),
        },
    )
    .unwrap();

    refused_with(&answer, "no_full_qa");
}

#[test]
fn focused_and_failed_only_stages_are_not_full() {
    for selection in [StageSelection::Focused, StageSelection::FailedOnly] {
        let mut stages = passed_stages();
        stages[1].selection = selection;

        refused_with(&with_stages(stages), "selection_not_full");
    }
}

#[test]
fn a_failed_or_missing_stage_is_not_passed() {
    let mut failed = passed_stages();
    failed[0].result = StageResult::Fail;
    failed[0].exit_code = Some(1);
    refused_with(&with_stages(failed), "stage_not_passed");

    let missing = vec![passed_stage("gate-1", CANDIDATE)];
    refused_with(&with_stages(missing), "stage_not_passed");
}

#[test]
fn a_stage_of_another_candidate_is_a_candidate_mismatch() {
    let mut stages = passed_stages();
    stages[0].candidate_sha = "d".repeat(40);

    refused_with(&with_stages(stages), "stage_candidate_mismatch");
}

#[test]
fn a_stage_of_another_plan_or_environment_is_a_plan_mismatch() {
    let mut plan = passed_stages();
    plan[0].plan_sha256 = Some("a".repeat(64));
    refused_with(&with_stages(plan), "plan_mismatch");

    let mut environment = passed_stages();
    environment[1].environment_fingerprint = Some("b".repeat(64));
    refused_with(&with_stages(environment), "plan_mismatch");
}

#[test]
fn a_missing_or_changed_output_stream_is_unverifiable() {
    let stdout = passed_stages()[0].stdout.clone().unwrap().path;
    let mut missing = Memory::passed();
    missing.artifacts.remove(&stdout);
    refused_with(&judged(&missing), "evidence_unverifiable");

    let mut changed = Memory::passed();
    changed
        .artifacts
        .insert(stdout, b"another output\n".to_vec());
    refused_with(&judged(&changed), "evidence_unverifiable");
}

#[test]
fn a_missing_log_or_document_is_unverifiable() {
    for prefix in [".log", ".stage-evidence.json"] {
        let mut memory = Memory::passed();
        memory.artifacts.retain(|path, _| !path.ends_with(prefix));

        refused_with(&judged(&memory), "evidence_unverifiable");
    }
}

fn link(memory: &mut Memory, name: &str, link: Value) {
    memory
        .records
        .insert(format!("reports/integration-evidence/{name}.json"), link);
}

fn ci_link(verification: &str, result: &str) -> Value {
    let collector = json!({"source": "github", "repository": "o/r", "merge_commit_sha": "m"});
    json!({
        "kind": "ci",
        "candidate_sha": CANDIDATE,
        "target_sha": TARGET,
        "result": result,
        "verification": verification,
        "artifact_sha256": sha256_hex(canonical_json(&collector).as_bytes()),
        "collector": collector,
    })
}

#[test]
fn a_ci_check_collect_ci_accepted_confirms_the_pair() {
    let mut memory = Memory::without_qa();
    link(&mut memory, "ci", ci_link("collector-accepted", "passed"));

    let answer = judged(&memory);

    assert_eq!(answer["verdict"], "ok", "{answer}");
    assert_eq!(answer["confirmed_by"]["source"], "ci");
}

#[test]
fn a_ci_check_collect_ci_did_not_accept_never_confirms_the_pair() {
    let hand_linked = json!({"kind": "ci", "candidate_sha": CANDIDATE, "target_sha": TARGET,
        "result": "passed", "verification": "unverified", "artifact_sha256": null});
    for checked in [
        hand_linked,
        ci_link("collector-failed", "failed"),
        ci_link("collector-accepted", "failed"),
    ] {
        let mut memory = Memory::without_qa();
        link(&mut memory, "ci", checked);

        refused_with(&judged(&memory), "ci_not_collected");
    }
    let mut forged = ci_link("collector-accepted", "passed");
    forged["artifact_sha256"] = json!("0".repeat(64));
    let mut memory = Memory::without_qa();
    link(&mut memory, "ci", forged);
    refused_with(&judged(&memory), "ci_not_collected");
}

fn local_qa(memory: &mut Memory, verified: bool) {
    let mut files = qa_store::Files::new();
    let log = qa_store::run_files(passed_stages(), &mut files);
    for (path, bytes) in files {
        memory.artifacts.insert(path, bytes);
    }
    let result = json!({
        "local_qa_id": "local-qa-r",
        "event": "result",
        "request_id": "local-qa-1",
        "candidate_sha": CANDIDATE,
        "target_sha": TARGET,
        "state": "completed",
        "verification": if verified { "verified" } else { "unverified" },
        "checks_run": COMMANDS.iter()
            .map(|command| json!({"command": command, "result": "pass"}))
            .collect::<Vec<_>>(),
        "artifact_sha256": log,
    });
    let digest = sha256_hex(canonical_json(&result).as_bytes());
    memory.records.insert(
        "reports/integration-local-qa/local-qa-r.json".to_owned(),
        result,
    );
    link(
        memory,
        "local",
        json!({"kind": "local-qa", "candidate_sha": CANDIDATE, "target_sha": TARGET,
               "result": "passed", "verification": if verified { "verified" } else { "unverified" },
               "local_qa_request_id": "local-qa-1", "local_qa_result_sha256": digest}),
    );
}

#[test]
fn a_verified_local_qa_run_of_the_pair_confirms_it() {
    let mut memory = Memory::without_qa();
    local_qa(&mut memory, true);

    let answer = judged(&memory);

    assert_eq!(answer["verdict"], "ok", "{answer}");
    assert_eq!(answer["confirmed_by"]["source"], "local-qa");
}

#[test]
fn an_unverified_local_qa_link_never_confirms_the_pair() {
    let mut memory = Memory::without_qa();
    local_qa(&mut memory, false);

    refused_with(&judged(&memory), "evidence_unverifiable");
}

#[test]
fn a_payload_other_than_exactly_the_pair_is_malformed() {
    let good = json!({"candidate_sha": CANDIDATE, "target_sha": TARGET});
    assert_eq!(Pair::parse(Some(&good)).unwrap(), pair());
    for bad in [
        json!({"candidate_sha": CANDIDATE, "target_sha": TARGET, "extra": 1}),
        json!({"candidate_sha": CANDIDATE}),
        json!({"candidate_sha": CANDIDATE.to_uppercase(), "target_sha": TARGET}),
        json!({"candidate_sha": "abc", "target_sha": TARGET}),
        json!([CANDIDATE, TARGET]),
    ] {
        assert!(Pair::parse(Some(&bad)).is_err(), "{bad}");
    }
    assert!(Pair::parse(None).is_err());
}

#[test]
fn the_report_reference_and_the_trailer_follow_the_shared_golden_fixture() {
    let text = include_str!("../../contracts/tests/fixtures/qa-evidence-reference.v1.json");
    let fixture: Value = serde_json::from_str(text).unwrap();
    for case in fixture["report_outputs"].as_array().unwrap() {
        assert_eq!(
            qa_evidence::log_reference(case["output"].as_str().unwrap()),
            case["log_sha256"].as_str().map(str::to_owned),
            "{case}"
        );
    }
    for case in fixture["logs"].as_array().unwrap() {
        let expected = case["trailer"].as_object().map(|t| {
            (
                t["path"].as_str().unwrap().to_owned(),
                t["sha256"].as_str().unwrap().to_owned(),
            )
        });
        assert_eq!(
            qa_evidence::trailer(case["log"].as_str().unwrap()),
            expected,
            "{case}"
        );
    }
}

// -- the store ---------------------------------------------------------------------------------

fn store_with(run: &Run) -> Repo {
    let repo = Repo::new("qa-evidence");
    qa_store::write_legacy(&repo.generation(), run);
    state::migrate::run(&repo.root).unwrap();
    repo
}

fn revision(repo: &Repo) -> u64 {
    read(&repo.state(), None, None).unwrap().revision
}

#[test]
fn the_store_answers_ok_for_an_imported_qa_run_and_its_revision_does_not_move() {
    let repo = store_with(&accepted_qa(passed_stages()));
    let before = revision(&repo);
    let payload = json!({"candidate_sha": CANDIDATE, "target_sha": TARGET});

    let answer = qa_evidence::verify(&repo.state(), Some(&payload)).unwrap();
    let again = qa_evidence::verify(&repo.state(), Some(&payload)).unwrap();

    assert_eq!(answer["verdict"], "ok", "{answer}");
    assert_eq!(answer["confirmed_by"]["source"], "original-qa");
    assert_eq!(again, answer);
    assert_eq!(revision(&repo), before);
}

#[test]
fn the_store_refuses_a_focused_run() {
    let mut stages = passed_stages();
    stages[0].selection = StageSelection::Focused;
    let repo = store_with(&accepted_qa(stages));
    let payload = json!({"candidate_sha": CANDIDATE, "target_sha": TARGET});

    let answer = qa_evidence::verify(&repo.state(), Some(&payload)).unwrap();

    refused_with(&answer, "selection_not_full");
}

#[test]
fn the_file_ledger_is_not_attached_and_a_bad_payload_is_malformed() {
    let repo = Repo::new("qa-evidence-file-ledger");
    let payload = json!({"candidate_sha": CANDIDATE, "target_sha": TARGET});

    let file_ledger = qa_evidence::verify(&repo.state(), Some(&payload));
    state::migrate::run(&repo.root).unwrap();
    let malformed = qa_evidence::verify(&repo.state(), Some(&json!({"candidate_sha": CANDIDATE})));

    assert!(
        matches!(file_ledger, Err(ReadError::NotAttached(_))),
        "{file_ledger:?}"
    );
    assert!(
        matches!(malformed, Err(ReadError::Malformed(_))),
        "{malformed:?}"
    );
}

#[test]
fn a_pair_record_is_checked_field_by_field() {
    let (path, record) = qa_store::pair_record();
    let record = record.as_object().unwrap().clone();
    assert_eq!(qa_evidence::check_pair_record(&path, &record), Ok(()));
    let mut extra = record.clone();
    extra.insert("recorded_at".to_owned(), json!("now"));
    let mut bad_sha = record.clone();
    bad_sha.insert("candidate_tree_sha".to_owned(), json!("XYZ"));
    let mut bad_id = record.clone();
    bad_id.insert(
        "pair_id".to_owned(),
        json!(format!("pair-{}", "0".repeat(32))),
    );
    let mut bad_contract = record.clone();
    bad_contract.insert("contract".to_owned(), json!(2));
    for broken in [extra, bad_sha, bad_id, bad_contract] {
        assert!(
            qa_evidence::check_pair_record(&path, &broken).is_err(),
            "{broken:?}"
        );
    }
    let elsewhere = "reports/integration-pair/pair-00000000000000000000000000000000.json";
    assert!(qa_evidence::check_pair_record(elsewhere, &record).is_err());
}
