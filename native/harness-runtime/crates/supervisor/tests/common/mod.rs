//! The shared fixture of the launch and QA tests: a brief whose hook policy is pinned and bound
//! to its approval as `dispatch create` pins it (`hook_policy_sha256`, the configuration snapshot
//! in `orchestration_policy.configuration`, and both digests in the approved transition).

#![allow(dead_code)]

use serde_json::{Map, Value, json};
use state::canonical::{canonical_json, compact_sha256, sha256_hex};
use supervisor::hook_policy;

/// Pin `project_hook_policy`, the `hook_policy` key of the pinned project (`Value::Null` for a
/// project without one), into `brief`, and bind the approval to the result.
pub fn pin_hook_policy(brief: &mut Map<String, Value>, project_hook_policy: &Value) {
    let role = brief.get("role").cloned().unwrap_or(Value::Null);
    let project = if project_hook_policy.is_null() {
        json!({})
    } else {
        json!({"hook_policy": project_hook_policy})
    };
    let policy = hook_policy::normalize(project.get("hook_policy")).unwrap();
    let pinned = hook_policy::sha256(&policy);
    let mut configuration = json!({
        "contract": "systemFields.v1",
        "normalizer": "normalization.v1",
        "config": {},
        "project": project,
        "role": {"name": role},
    });
    let canonical = sha256_hex(canonical_json(&configuration).as_bytes());
    configuration["source_sha256"] = json!(canonical);
    configuration["canonical_sha256"] = json!(canonical);
    let mut orchestration = object(brief, "orchestration_policy");
    orchestration.insert("configuration".to_owned(), configuration);
    brief.insert(
        "orchestration_policy".to_owned(),
        Value::Object(orchestration),
    );
    let mut transition = brief
        .get("transition")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_else(|| transition_of(brief));
    transition.insert("hook_policy_sha256".to_owned(), json!(pinned));
    transition.insert("configuration_sha256".to_owned(), json!(canonical));
    brief.insert("transition".to_owned(), Value::Object(transition));
    brief.insert("hook_policy_sha256".to_owned(), json!(pinned));
    bind_approval(brief);
}

/// The digest of the normalized `project_hook_policy`, as `dispatch create` pins it.
pub fn policy_sha256(project_hook_policy: &Value) -> String {
    hook_policy::sha256(&hook_policy::normalize(Some(project_hook_policy)).unwrap())
}

/// Bind the approval to the transition `brief` holds now: `transition_digest` and
/// `coordinator_approval.transition_digest`.
pub fn bind_approval(brief: &mut Map<String, Value>) {
    let digest = compact_sha256(&brief["transition"]);
    brief.insert("transition_digest".to_owned(), json!(digest));
    let mut approval = object(brief, "coordinator_approval");
    approval.insert("transition_digest".to_owned(), json!(digest));
    brief.insert("coordinator_approval".to_owned(), Value::Object(approval));
}

fn object(brief: &Map<String, Value>, key: &str) -> Map<String, Value> {
    brief
        .get(key)
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default()
}

/// The transition fields every approval binds, for a fixture brief that holds none.
fn transition_of(brief: &Map<String, Value>) -> Map<String, Value> {
    let role = brief.get("role").cloned().unwrap_or(Value::Null);
    json!({
        "batch_id": brief.get("batch_id").cloned().unwrap_or(Value::Null),
        "previous_dispatch_id": null,
        "previous_role": null,
        "reason_category": null,
        "next_role": role,
        "next_action": role,
        "purpose": "work",
        "candidate_sha": null,
        "base_sha": "0".repeat(40),
        "review_scope": [],
        "verification_commands": [],
        "context_package_id": null,
        "required_gates": [],
    })
    .as_object()
    .cloned()
    .unwrap_or_default()
}
