//! Deterministic repairs of historically invalid state, applied while the state is imported
//! (ADR 0020, section 5, step 4). A repair changes the imported record, never the legacy file, and
//! every repair is written to the audit chain as `migration-repair` with the hashes of the record
//! before and after and the name of the rule.

use serde_json::{Map, Value};

use crate::canonical::{canonical_json, sha256_hex};
use crate::project::{BATCH, Spec};

/// The rule of issue #781: a batch that owes a risk reassessment (`risk_reassessment_required` is
/// `true`) but names no `risk_reassessment_candidate` owes it for a candidate that is not known
/// yet, which the policy writes as `risk_reassessment_pending_next = true`.
pub const RULE_781: &str = "781-risk-reassessment-pending-next";

/// One applied repair.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Repair {
    /// The record path relative to the state root.
    pub path: String,
    pub rule: &'static str,
    pub before_sha256: String,
    pub after_sha256: String,
}

fn digest(record: &Map<String, Value>) -> String {
    sha256_hex(canonical_json(&Value::Object(record.clone())).as_bytes())
}

fn names_a_candidate(batch: &Map<String, Value>) -> bool {
    batch
        .get("risk_reassessment_candidate")
        .and_then(Value::as_str)
        .is_some_and(|candidate| !candidate.is_empty())
}

/// Repair `record`, a record of the directory of `spec`, in place. `path` is its path relative
/// to the state root. Returns the repair when it changed the record.
pub fn apply(spec: &Spec, path: &str, record: &mut Map<String, Value>) -> Option<Repair> {
    if spec.table != BATCH.table
        || record.get("risk_reassessment_required") != Some(&Value::Bool(true))
        || names_a_candidate(record)
        || record.get("risk_reassessment_pending_next") == Some(&Value::Bool(true))
    {
        return None;
    }
    let before_sha256 = digest(record);
    record.insert(
        "risk_reassessment_pending_next".to_owned(),
        Value::Bool(true),
    );
    Some(Repair {
        path: path.to_owned(),
        rule: RULE_781,
        before_sha256,
        after_sha256: digest(record),
    })
}
