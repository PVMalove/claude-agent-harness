//! `qa.evidence.verify`: the verdict on the stored QA evidence of one Release Gate pair
//! (`qa_evidence_verify` of ADR 0021, section 7; ADR 0023, section 5; ADR 0026, section 4).
//!
//! Python observes the Git facts of a pair (the head of the branch, the head of the target, the
//! ancestry and the tree after a merge) and decides push and merge. This module judges only what
//! the store holds: whether a full QA run of exactly `{candidate_sha, target_sha}` is stored and
//! verifies. It reads one snapshot of the store, writes nothing and records no operation, so a
//! repeat is always safe and the revision of the store does not move.
//!
//! A pair is confirmed by one of three sources:
//! - `original-qa`: an accepted QA dispatch of a batch whose integration base is the target, on
//!   exactly the candidate, whose completed report lists the approved commands as passed and
//!   links its log (`sha256:<hex>` in `output`); the trailer of the log links the
//!   `stage-evidence.v1` document, and every stage of it is full, passed and on the candidate;
//! - `local-qa`: a verified local-QA result of the pair, with the same chain from its log;
//! - `ci`: a CI check of the pair that `integration collect-ci` accepted.
//!
//! Developer and review reports, the `/qa-gate` marker and a stage the store holds no output
//! stream of are never a source: they are cached results (ADR 0023, section 4).

use std::collections::BTreeSet;
use std::path::Path;

use contracts::{
    QaEvidenceVerdict, QaEvidenceViolation, StageEvidenceDocument, StageResult, StageSelection,
};
use serde_json::{Map, Value, json};

use crate::canonical::{canonical_json, compact_sha256, object_sha256, sha256_hex};
use crate::read::{self, ReadError};

/// The record directory of the Release Gate pairs.
pub const PAIR_DIRECTORY: &str = "reports/integration-pair";
/// The fields of a pair record, exactly.
pub const PAIR_FIELDS: [&str; 5] = [
    "pair_id",
    "contract",
    "candidate_sha",
    "target_sha",
    "candidate_tree_sha",
];
const PAIR_CONTRACT: u64 = 1;
const EVIDENCE_DIRECTORY: &str = "reports/integration-evidence/";
const LOCAL_QA_DIRECTORY: &str = "reports/integration-local-qa/";
const QA_ARTIFACTS: &str = "qa-artifacts/";
/// The prefix of the last line of a QA log that links its stage-evidence document.
const TRAILER: &str = "stage-evidence: ";
/// The stage group of the approved commands: `gate-1`, `gate-2`, ... in their order.
const GATE_STAGE: &str = "gate";

/// Whether `text` is a full commit or tree id: 40 or 64 lower-case hex digits.
pub fn is_sha(text: &str) -> bool {
    matches!(text.len(), 40 | 64) && text.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

fn is_sha256(text: &str) -> bool {
    text.len() == 64 && is_sha(text)
}

/// A Release Gate pair: a candidate and the target its QA checked.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Pair {
    pub candidate_sha: String,
    pub target_sha: String,
}

impl Pair {
    /// The pair of a `qa.evidence.verify` payload: exactly `candidate_sha` and `target_sha`.
    ///
    /// # Errors
    /// The reason the payload is malformed.
    pub fn parse(payload: Option<&Value>) -> Result<Self, String> {
        let Some(Value::Object(members)) = payload else {
            return Err("the payload must be an object {candidate_sha, target_sha}".to_owned());
        };
        if let Some(unknown) = members
            .keys()
            .find(|key| !matches!(key.as_str(), "candidate_sha" | "target_sha"))
        {
            return Err(format!("unknown payload field {unknown:?}"));
        }
        let sha = |key: &str| match members.get(key).and_then(Value::as_str) {
            Some(text) if is_sha(text) => Ok(text.to_owned()),
            _ => Err(format!("{key} must be a full lower-case commit id")),
        };
        Ok(Self {
            candidate_sha: sha("candidate_sha")?,
            target_sha: sha("target_sha")?,
        })
    }

    /// The id of the pair record: `pair-` and the first 32 hex digits of the SHA-256 of the
    /// canonical `{candidate_sha, target_sha}`, as the Python ledger derives it.
    pub fn id(&self) -> String {
        let members = json!({"candidate_sha": self.candidate_sha, "target_sha": self.target_sha});
        let digest = sha256_hex(canonical_json(&members).as_bytes());
        format!("pair-{}", &digest[..32])
    }

    /// The path of the pair record.
    pub fn path(&self) -> String {
        format!("{PAIR_DIRECTORY}/{}.json", self.id())
    }
}

/// Check a pair record a `state.write` creates at `path`: its exact fields, its contract, its
/// three ids and the `pair_id` its pair derives.
///
/// # Errors
/// What breaks the record.
pub fn check_pair_record(path: &str, record: &Map<String, Value>) -> Result<(), String> {
    let keys: BTreeSet<&str> = record.keys().map(String::as_str).collect();
    if keys != PAIR_FIELDS.into_iter().collect() {
        return Err(format!(
            "the pair record {path} must hold exactly the fields {PAIR_FIELDS:?}"
        ));
    }
    if record.get("contract").and_then(Value::as_u64) != Some(PAIR_CONTRACT) {
        return Err(format!(
            "the pair record {path} must have contract {PAIR_CONTRACT}"
        ));
    }
    let text = |key: &str| {
        record
            .get(key)
            .and_then(Value::as_str)
            .filter(|t| is_sha(t))
    };
    let (Some(candidate), Some(target), Some(_)) = (
        text("candidate_sha"),
        text("target_sha"),
        text("candidate_tree_sha"),
    ) else {
        return Err(format!(
            "the pair record {path} must name full lower-case candidate, target and tree ids"
        ));
    };
    let pair = Pair {
        candidate_sha: candidate.to_owned(),
        target_sha: target.to_owned(),
    };
    if record.get("pair_id").and_then(Value::as_str) != Some(pair.id().as_str())
        || path != pair.path()
    {
        return Err(format!(
            "the pair record {path} must be {} with pair_id {}",
            pair.path(),
            pair.id()
        ));
    }
    Ok(())
}

/// What the store holds for the verdict.
pub trait Evidence {
    /// Every JSON record of `batches/`, `dispatches/` and `reports/`, by its legacy path.
    fn records(&self) -> &Map<String, Value>;

    /// The bytes of the artifact at `path` (a QA log, a stage-evidence document, an output
    /// stream), or `None` when the store holds none.
    ///
    /// # Errors
    /// The store cannot be read.
    fn artifact(&self, path: &str) -> Result<Option<Vec<u8>>, ReadError>;

    /// The JSON record at `path`, wherever it lies; by default one of [`Evidence::records`].
    ///
    /// # Errors
    /// The store cannot be read.
    fn record(&self, path: &str) -> Result<Option<Value>, ReadError> {
        Ok(self.records().get(path).cloned())
    }
}

/// The evidence of one read snapshot of the store.
struct Stored {
    snapshot: read::Snapshot,
    records: Map<String, Value>,
}

impl Evidence for Stored {
    fn records(&self) -> &Map<String, Value> {
        &self.records
    }

    fn artifact(&self, path: &str) -> Result<Option<Vec<u8>>, ReadError> {
        self.snapshot.artifact(path)
    }

    fn record(&self, path: &str) -> Result<Option<Value>, ReadError> {
        let wanted = read::wanted(Some(&json!({"paths": [path]})), &[])?;
        Ok(self.snapshot.answer(&wanted)?.records.remove(path))
    }
}

/// Answer one `qa.evidence.verify` request for the store of `state`, from one read snapshot.
///
/// # Errors
/// `NotAttached` in the file-ledger epoch, `Malformed` for a payload other than exactly
/// `{candidate_sha, target_sha}`, and the errors of a read.
pub fn verify(state: &Path, payload: Option<&Value>) -> Result<Value, ReadError> {
    let pair = Pair::parse(payload).map_err(ReadError::Malformed)?;
    let snapshot = read::open(state)?;
    let wanted = read::wanted(
        Some(&json!({"collections": ["batches", "dispatches", "reports"]})),
        &[],
    )?;
    let answer = match snapshot.answer(&wanted) {
        Ok(answer) => answer,
        Err(error) => {
            snapshot.close();
            return Err(error);
        }
    };
    let stored = Stored {
        snapshot,
        records: answer.records,
    };
    let verdict = judge(&stored, &pair);
    stored.snapshot.close();
    verdict
}

/// One violation: its code, the record or artifact it was found in, and why.
fn violation(code: QaEvidenceViolation, evidence: &str, detail: impl Into<String>) -> Value {
    json!({"code": code.as_str(), "evidence": evidence, "detail": detail.into()})
}

/// One source of evidence of the pair and what keeps it from confirming the pair.
struct Source {
    name: &'static str,
    evidence: String,
    violations: Vec<Value>,
}

/// The verdict on the stored evidence of `pair`.
///
/// `ok` holds exactly when the pair record names a `candidate_tree_sha` and one source confirms
/// a full QA run of the pair: then `violations` is empty and `confirmed_by` names the source.
///
/// # Errors
/// The store cannot be read.
pub fn judge(evidence: &impl Evidence, pair: &Pair) -> Result<Value, ReadError> {
    let records = evidence.records();
    let pair_path = pair.path();
    let tree = records
        .get(&pair_path)
        .and_then(|record| record.get("candidate_tree_sha"))
        .and_then(Value::as_str)
        .filter(|tree| is_sha(tree));
    let mut sources = original_qa(evidence, pair)?;
    sources.extend(local_qa(evidence, pair)?);
    sources.extend(ci(records, pair));
    let confirmed = sources.iter().find(|source| source.violations.is_empty());
    let mut violations = Vec::new();
    if tree.is_none() {
        violations.push(violation(
            QaEvidenceViolation::PairNotRecorded,
            &pair_path,
            "the store holds no pair record with a candidate_tree_sha for the pair",
        ));
    }
    if confirmed.is_none() {
        if sources.is_empty() {
            violations.push(violation(
                QaEvidenceViolation::NoFullQa,
                &pair_path,
                "no accepted QA run, verified local-QA run or collected CI check of exactly the \
                 pair is stored",
            ));
        }
        for source in &sources {
            violations.extend(source.violations.iter().cloned());
        }
    }
    let verdict = if violations.is_empty() {
        QaEvidenceVerdict::Ok
    } else {
        QaEvidenceVerdict::Refused
    };
    let confirmed_by = match (verdict, confirmed) {
        (QaEvidenceVerdict::Ok, Some(source)) => {
            json!({"source": source.name, "evidence": source.evidence})
        }
        _ => Value::Null,
    };
    Ok(json!({
        "verdict": verdict.as_str(),
        "violations": violations,
        "pair": {
            "candidate_sha": pair.candidate_sha,
            "target_sha": pair.target_sha,
            "candidate_tree_sha": tree,
        },
        "confirmed_by": confirmed_by,
    }))
}

fn text<'a>(record: &'a Value, key: &str) -> Option<&'a str> {
    record.get(key).and_then(Value::as_str)
}

fn strings(record: &Value, key: &str) -> Vec<String> {
    record
        .get(key)
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|item| item.as_str().map(str::to_owned))
        .collect()
}

fn under<'a>(
    records: &'a Map<String, Value>,
    prefix: &'a str,
) -> impl Iterator<Item = (&'a String, &'a Value)> {
    records
        .iter()
        .filter(move |(path, _)| path.starts_with(prefix))
}

/// Whether every check of `checks_run` passed and the commands are exactly `commands`.
fn checks_pass(checks_run: Option<&Value>, commands: &[String]) -> bool {
    let checks = checks_run
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    checks.len() == commands.len()
        && checks.iter().zip(commands).all(|(check, command)| {
            text(check, "command") == Some(command.as_str())
                && text(check, "result") == Some("pass")
        })
}

/// The identity a stage of a run carries: the plan and the image its QA brief pinned.
struct Pinned {
    plan_sha256: Option<String>,
    image_digest: Option<String>,
}

fn original_qa(evidence: &impl Evidence, pair: &Pair) -> Result<Vec<Source>, ReadError> {
    let records = evidence.records();
    let mut sources = Vec::new();
    for (_, batch) in under(records, "batches/") {
        let base = text(batch, "integration_base_commit")
            .filter(|base| !base.is_empty())
            .or_else(|| text(batch, "base_commit"));
        if base != Some(pair.target_sha.as_str()) {
            continue;
        }
        let entries = batch.get("dispatches").and_then(Value::as_array);
        for entry in entries.into_iter().flatten() {
            let accepted =
                entry.get("decision").and_then(|d| text(d, "decision")) == Some("accept");
            if text(entry, "role") != Some("qa")
                || text(entry, "state") != Some("reported")
                || !accepted
            {
                continue;
            }
            let Some(dispatch_id) = text(entry, "dispatch_id") else {
                continue;
            };
            let dispatch_path = format!("dispatches/{dispatch_id}.json");
            let Some(dispatch) = records.get(&dispatch_path) else {
                continue;
            };
            if text(dispatch, "candidate_commit") != Some(pair.candidate_sha.as_str()) {
                continue;
            }
            let report_path = text(entry, "report").unwrap_or_default().to_owned();
            let report = records.get(&report_path).and_then(Value::as_object);
            let Some(report) = report.filter(|report| {
                text(entry, "report_sha256") == Some(object_sha256(report).as_str())
            }) else {
                sources.push(Source {
                    name: "original-qa",
                    evidence: dispatch_path,
                    violations: vec![violation(
                        QaEvidenceViolation::EvidenceUnverifiable,
                        &report_path,
                        "the accepted QA report is missing or does not match its report_sha256",
                    )],
                });
                continue;
            };
            let report = Value::Object(report.clone());
            // A failed run is not a run that confirms anything; Python skips it as well.
            if text(&report, "outcome") != Some("completed") {
                continue;
            }
            let commands = strings(dispatch, "verification_commands");
            let mut violations = Vec::new();
            if !checks_pass(report.get("checks_run"), &commands) {
                violations.push(violation(
                    QaEvidenceViolation::StageNotPassed,
                    &report_path,
                    "the report does not list exactly the approved commands as passed",
                ));
            }
            let pinned = Pinned {
                plan_sha256: dispatch
                    .get("verification_plan")
                    .filter(|plan| plan.is_object())
                    .map(compact_sha256),
                image_digest: dispatch
                    .get("runtime_images")
                    .filter(|images| images.is_object())
                    .map(compact_sha256),
            };
            let log = text(&report, "output").and_then(log_reference);
            violations.extend(run_chain(
                evidence,
                pair,
                &report_path,
                log.as_deref(),
                &commands,
                &pinned,
            )?);
            sources.push(Source {
                name: "original-qa",
                evidence: report_path,
                violations,
            });
        }
    }
    Ok(sources)
}

fn of_pair(link: &Value, pair: &Pair) -> bool {
    text(link, "candidate_sha") == Some(pair.candidate_sha.as_str())
        && text(link, "target_sha") == Some(pair.target_sha.as_str())
}

fn local_qa(evidence: &impl Evidence, pair: &Pair) -> Result<Vec<Source>, ReadError> {
    let records = evidence.records();
    let mut sources = Vec::new();
    for (path, link) in under(records, EVIDENCE_DIRECTORY) {
        if text(link, "kind") != Some("local-qa") || !of_pair(link, pair) {
            continue;
        }
        let request = text(link, "local_qa_request_id");
        let digest = text(link, "local_qa_result_sha256");
        let result = under(records, LOCAL_QA_DIRECTORY).find(|(_, result)| {
            text(result, "event") == Some("result")
                && request.is_some()
                && text(result, "request_id") == request
                && result
                    .as_object()
                    .is_some_and(|record| Some(object_sha256(record).as_str()) == digest)
        });
        let verified = text(link, "verification") == Some("verified");
        let Some((result_path, result)) = result.filter(|_| verified) else {
            sources.push(Source {
                name: "local-qa",
                evidence: path.clone(),
                violations: vec![violation(
                    QaEvidenceViolation::EvidenceUnverifiable,
                    path,
                    "the local-qa check is not linked to a verified local-QA result it matches",
                )],
            });
            continue;
        };
        let commands: Vec<String> = result
            .get("checks_run")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(|check| text(check, "command").map(str::to_owned))
            .collect();
        let mut violations = Vec::new();
        let passed = text(result, "state") == Some("completed")
            && text(result, "verification") == Some("verified")
            && !commands.is_empty()
            && checks_pass(result.get("checks_run"), &commands);
        if !passed || !of_pair(result, pair) {
            violations.push(violation(
                QaEvidenceViolation::StageNotPassed,
                result_path,
                "the local-QA result of the pair is not a completed, verified run of passed commands",
            ));
        }
        // A local-QA run has no brief: its stages pin no plan and no image.
        let pinned = Pinned {
            plan_sha256: None,
            image_digest: None,
        };
        let log = text(result, "artifact_sha256").filter(|sha| is_sha256(sha));
        violations.extend(run_chain(
            evidence,
            pair,
            result_path,
            log,
            &commands,
            &pinned,
        )?);
        sources.push(Source {
            name: "local-qa",
            evidence: result_path.clone(),
            violations,
        });
    }
    Ok(sources)
}

fn ci(records: &Map<String, Value>, pair: &Pair) -> Vec<Source> {
    under(records, EVIDENCE_DIRECTORY)
        .filter(|(_, link)| text(link, "kind") == Some("ci") && of_pair(link, pair))
        .map(|(path, link)| {
            let collected = text(link, "verification") == Some("collector-accepted")
                && text(link, "result") == Some("passed")
                && link
                    .get("collector")
                    .and_then(Value::as_object)
                    .is_some_and(|collector| {
                        text(link, "artifact_sha256") == Some(object_sha256(collector).as_str())
                    });
            let violations = if collected {
                Vec::new()
            } else {
                vec![violation(
                    QaEvidenceViolation::CiNotCollected,
                    path,
                    "integration collect-ci did not accept this CI check of the pair",
                )]
            };
            Source {
                name: "ci",
                evidence: path.clone(),
                violations,
            }
        })
        .collect()
}

/// The SHA-256 of the QA log a QA report links in its `output`: the last
/// `(sha256:<64 hex digits>)` of the text (`qa_lane._qa_report`).
pub fn log_reference(output: &str) -> Option<String> {
    output.rmatch_indices("(sha256:").find_map(|(at, marker)| {
        let rest = &output[at + marker.len()..];
        let digest = rest.get(..64)?;
        (is_sha256(digest) && rest[64..].starts_with(')')).then(|| digest.to_owned())
    })
}

/// The `(path, sha256)` of the stage-evidence document the trailer of a QA log links: the last
/// line `stage-evidence: <path> sha256:<64 hex digits>` (`gate_runner.format_evidence_trailer`).
pub fn trailer(log: &str) -> Option<(String, String)> {
    log.lines().rev().find_map(|line| {
        let rest = line.strip_prefix(TRAILER)?;
        let (path, digest) = rest.split_once(" sha256:")?;
        let valid = !path.is_empty() && !path.contains(char::is_whitespace) && is_sha256(digest);
        valid.then(|| (path.to_owned(), digest.to_owned()))
    })
}

/// The bytes of the stage-evidence document at `path`: an artifact, as a command wrote it after
/// the cutover. The import of the file ledger keeps a JSON document as a record; its text in the
/// order of the schema (`StageEvidenceDocument.dumps` of Python) is the text the trailer hashed.
fn document_bytes(evidence: &impl Evidence, path: &str) -> Result<Option<Vec<u8>>, ReadError> {
    if let Some(bytes) = evidence.artifact(path)? {
        return Ok(Some(bytes));
    }
    Ok(evidence
        .record(path)?
        .and_then(|record| serde_json::from_value::<StageEvidenceDocument>(record).ok())
        .and_then(|document| serde_json::to_string_pretty(&document).ok())
        .map(|text| format!("{text}\n").into_bytes()))
}

/// The violations of the run whose QA log has the SHA-256 `log`: the log, its trailer, the
/// stage-evidence document and every stage of it.
fn run_chain(
    evidence: &impl Evidence,
    pair: &Pair,
    origin: &str,
    log: Option<&str>,
    commands: &[String],
    pinned: &Pinned,
) -> Result<Vec<Value>, ReadError> {
    let unverifiable = |at: &str, detail: &str| {
        vec![violation(
            QaEvidenceViolation::EvidenceUnverifiable,
            at,
            detail,
        )]
    };
    let Some(log) = log else {
        return Ok(unverifiable(
            origin,
            "the run names no QA log by its sha256",
        ));
    };
    let log_path = format!("{QA_ARTIFACTS}{log}.log");
    let Some(bytes) = evidence.artifact(&log_path)? else {
        return Ok(unverifiable(
            &log_path,
            "the store holds no QA log of the run",
        ));
    };
    let text = match String::from_utf8(bytes) {
        Ok(text) if sha256_hex(text.as_bytes()) == log => text,
        _ => {
            return Ok(unverifiable(
                &log_path,
                "the QA log does not match its sha256",
            ));
        }
    };
    let Some((document_path, digest)) = trailer(&text) else {
        return Ok(unverifiable(
            &log_path,
            "the QA log has no stage-evidence trailer",
        ));
    };
    if !document_path.starts_with(QA_ARTIFACTS) {
        return Ok(unverifiable(
            &log_path,
            "the trailer links a document outside qa-artifacts",
        ));
    }
    let document = document_bytes(evidence, &document_path)?
        .filter(|bytes| sha256_hex(bytes) == digest)
        .and_then(|bytes| String::from_utf8(bytes).ok())
        .map(|text| StageEvidenceDocument::from_json_str(&text));
    let document = match document {
        Some(Ok(document)) => document,
        Some(Err(error)) => {
            return Ok(unverifiable(
                &document_path,
                &format!("the stage-evidence document breaks its schema: {error}"),
            ));
        }
        None => {
            return Ok(unverifiable(
                &document_path,
                "the store holds no stage-evidence document with the sha256 of the trailer",
            ));
        }
    };
    stage_violations(evidence, pair, &document_path, &document, commands, pinned)
}

fn stage_violations(
    evidence: &impl Evidence,
    pair: &Pair,
    at: &str,
    document: &StageEvidenceDocument,
    commands: &[String],
    pinned: &Pinned,
) -> Result<Vec<Value>, ReadError> {
    let mut found = Vec::new();
    for (index, command) in commands.iter().enumerate() {
        let stage_id = format!("{GATE_STAGE}-{}", index + 1);
        if !document
            .stages
            .iter()
            .any(|stage| stage.stage_id == stage_id)
        {
            found.push(violation(
                QaEvidenceViolation::StageNotPassed,
                at,
                format!("the approved command {command:?} has no stage {stage_id}"),
            ));
        }
    }
    let environments: BTreeSet<Option<&str>> = document
        .stages
        .iter()
        .map(|stage| stage.environment_fingerprint.as_deref())
        .collect();
    if environments.len() > 1 {
        found.push(violation(
            QaEvidenceViolation::PlanMismatch,
            at,
            "the stages of the run name different environments",
        ));
    }
    for stage in &document.stages {
        let id = &stage.stage_id;
        if stage.selection != StageSelection::Full {
            found.push(violation(
                QaEvidenceViolation::SelectionNotFull,
                at,
                format!("stage {id} selected {} tests", stage.selection.as_str()),
            ));
        }
        if stage.result != StageResult::Pass {
            found.push(violation(
                QaEvidenceViolation::StageNotPassed,
                at,
                format!("stage {id} is {}", stage.result.as_str()),
            ));
        }
        if stage.candidate_sha != pair.candidate_sha {
            found.push(violation(
                QaEvidenceViolation::StageCandidateMismatch,
                at,
                format!("stage {id} ran on {}", stage.candidate_sha),
            ));
        }
        if stage.plan_sha256 != pinned.plan_sha256 || stage.image_digest != pinned.image_digest {
            found.push(violation(
                QaEvidenceViolation::PlanMismatch,
                at,
                format!("stage {id} names another plan or image than its QA brief pinned"),
            ));
        }
        for stream in [&stage.stdout, &stage.stderr].into_iter().flatten() {
            let held = evidence.artifact(&stream.path)?;
            if held.is_none_or(|bytes| sha256_hex(&bytes) != stream.sha256) {
                found.push(violation(
                    QaEvidenceViolation::EvidenceUnverifiable,
                    &stream.path,
                    format!("the store holds no output stream of stage {id} with its sha256"),
                ));
            }
        }
    }
    Ok(found)
}
