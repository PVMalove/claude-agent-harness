//! Stage evidence `stage-evidence.v1` (ADR 0023, section 5): one typed record of what a
//! verification stage did.
//!
//! The Python writers (`harness/gate_runner/stage_evidence.py`) and the Rust QA executor write the
//! same document. `tests/fixtures/stage-evidence.v1.json` is the golden fixture both sides read, and
//! `tests/stage_evidence.rs` checks the closed vocabularies against the `systemFields.v1` registry.

use std::fmt;

use serde::{Deserialize, Deserializer, Serialize};

/// The `schema` value of a document.
pub const SCHEMA: &str = "stage-evidence.v1";

/// The exit status range shared with the Python writer: signed (POSIX) or unsigned (a Windows
/// NTSTATUS such as `0xC0000005`) 32-bit.
const EXIT_CODE_RANGE: std::ops::RangeInclusive<i64> = -(1_i64 << 31)..=(1_i64 << 32) - 1;

vocabulary! {
    /// Result of a stage; the registry field `report.check_result`.
    StageResult {
        Pass => "pass",
        Fail => "fail",
        NotRun => "not_run",
    }
}

vocabulary! {
    /// How a stage chose its tests; the registry field `verification_plan.selection`.
    StageSelection {
        Full => "full",
        Focused => "focused",
        #[serde(rename = "failed-only")]
        FailedOnly => "failed-only",
    }
}

vocabulary! {
    /// What a stage is; the registry field `verification_plan.stage_kind`.
    StageKind {
        Lint => "lint",
        Typecheck => "typecheck",
        Pytest => "pytest",
        #[serde(rename = "clean-room")]
        CleanRoom => "clean-room",
        Custom => "custom",
    }
}

/// A document that breaks the `stage-evidence.v1` schema.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StageEvidenceError(String);

impl fmt::Display for StageEvidenceError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "stage evidence: {}", self.0)
    }
}

impl std::error::Error for StageEvidenceError {}

/// An optional field that must still be present: `null` is a value, a missing key is an error.
fn required<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de>,
{
    Option::<T>::deserialize(deserializer)
}

/// A stored output stream: where it lies, relative to the evidence root, and its sha256.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FileRef {
    pub path: String,
    pub sha256: String,
}

/// The evidence of one stage. A stage that was never reached has no exit code and no streams.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StageEvidence {
    pub stage_id: String,
    pub kind: StageKind,
    pub selection: StageSelection,
    pub candidate_sha: String,
    #[serde(deserialize_with = "required")]
    pub plan_sha256: Option<String>,
    #[serde(deserialize_with = "required")]
    pub image_digest: Option<String>,
    #[serde(deserialize_with = "required")]
    pub environment_fingerprint: Option<String>,
    pub result: StageResult,
    #[serde(deserialize_with = "required")]
    pub exit_code: Option<i64>,
    #[serde(deserialize_with = "required")]
    pub stdout: Option<FileRef>,
    #[serde(deserialize_with = "required")]
    pub stderr: Option<FileRef>,
    pub duration_ms: u64,
    pub failed_nodeids: Vec<String>,
}

/// The evidence document of one verification run: its stages in execution order.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StageEvidenceDocument {
    pub schema: String,
    pub stages: Vec<StageEvidence>,
}

fn is_lower_hex(text: &str, lengths: &[usize]) -> bool {
    lengths.contains(&text.len()) && text.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

impl StageEvidence {
    fn validate(&self, index: usize) -> Result<(), StageEvidenceError> {
        let refuse = |problem: &str| Err(StageEvidenceError(format!("stages[{index}]: {problem}")));
        if self.stage_id.is_empty() {
            return refuse("stage_id must not be empty");
        }
        if !is_lower_hex(&self.candidate_sha, &[40, 64]) {
            return refuse("candidate_sha must be a lower-case hex commit id");
        }
        let digests = [
            ("plan_sha256", &self.plan_sha256),
            ("image_digest", &self.image_digest),
            ("environment_fingerprint", &self.environment_fingerprint),
        ];
        for (name, value) in digests {
            if value
                .as_deref()
                .is_some_and(|text| !is_lower_hex(text, &[64]))
            {
                return refuse(&format!("{name} must be a lower-case sha256"));
            }
        }
        if self.failed_nodeids.iter().any(String::is_empty) {
            return refuse("failed_nodeids must not hold an empty string");
        }
        for stream in [&self.stdout, &self.stderr].into_iter().flatten() {
            if stream.path.is_empty() || !is_lower_hex(&stream.sha256, &[64]) {
                return refuse("a stream needs a path and a lower-case sha256");
            }
        }
        if self
            .exit_code
            .is_some_and(|code| !EXIT_CODE_RANGE.contains(&code))
        {
            return refuse("exit_code must lie in -2147483648..4294967295 (a 32-bit exit status)");
        }
        let ran = self.exit_code.is_some();
        if ran != self.stdout.is_some() || ran != self.stderr.is_some() {
            return refuse("stdout and stderr are present exactly when the stage has an exit code");
        }
        match (self.result, self.exit_code) {
            (StageResult::Pass, code) if code != Some(0) => refuse("pass needs exit_code 0"),
            (StageResult::Fail, None | Some(0)) => refuse("fail needs a non-zero exit_code"),
            (StageResult::NotRun, Some(0)) => {
                refuse("not_run cannot carry the exit code of a success")
            }
            (StageResult::Pass | StageResult::NotRun, _) if !self.failed_nodeids.is_empty() => {
                refuse("only a failed stage lists failed nodeids")
            }
            _ => Ok(()),
        }
    }
}

impl StageEvidenceDocument {
    /// A document of the current schema.
    #[must_use]
    pub fn new(stages: Vec<StageEvidence>) -> Self {
        Self {
            schema: SCHEMA.to_owned(),
            stages,
        }
    }

    /// Read and validate a document: exact keys, closed vocabularies and cross-field rules.
    ///
    /// # Errors
    ///
    /// Returns [`StageEvidenceError`] when the text is not JSON or breaks the schema.
    pub fn from_json_str(text: &str) -> Result<Self, StageEvidenceError> {
        let document: Self =
            serde_json::from_str(text).map_err(|err| StageEvidenceError(err.to_string()))?;
        if document.schema != SCHEMA {
            return Err(StageEvidenceError(format!("schema must be {SCHEMA:?}")));
        }
        for (index, stage) in document.stages.iter().enumerate() {
            stage.validate(index)?;
        }
        Ok(document)
    }
}
