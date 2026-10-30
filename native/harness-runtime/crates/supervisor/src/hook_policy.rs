//! The hook policy an approved brief pins (ADR 0024, section 4; ADR 0021, operational
//! consequences): its normalized form and its digest, as Python `hook_policy.normalize_policy`
//! and `hook_policy_sha256` compute them. No I/O: the input is a value of the stored brief, never
//! the live `.harness/project.json` or the environment.
//!
//! The runtime does not validate profile names or apply the strict boundary; that stays in Python.
//! It only refuses a shape Python could never have normalized.
//!
//! `verify` is the one check the launch and QA admissions share: the `hook_policy_sha256` of the
//! brief holds only when the approval chain of the same brief binds it (the approval and the
//! transition digest, the transition, the pinned configuration snapshot) and the policy of that
//! snapshot hashes to it.

use contracts::{ErrorBody, ErrorCode, FailureDomain};
use serde_json::{Map, Value, json};
use state::canonical::{canonical_json, compact_sha256, sha256_hex, sort_key_json};

/// The profile of a project without a `hook_policy` key.
const DEFAULT_PROFILE: &str = "standard";

const MISMATCH_REMEDY: &str = "restore the approved brief and its approval binding, or approve a new dispatch with the current hook policy; nothing was written";

/// Check the hook policy the approved `brief` pins against the facts of that brief alone; the
/// live `.harness/project.json` and the environment are never read.
///
/// # Errors
/// `approval_mismatch` (domain policy, not retryable) naming the `binding` that does not hold,
/// with its `pinned` and `computed` values.
pub fn verify(brief: &Map<String, Value>) -> Result<(), ErrorBody> {
    let pinned = brief.get("hook_policy_sha256").and_then(Value::as_str);
    let pinned = match pinned {
        Some(pinned) if is_sha256(pinned) => pinned,
        _ => {
            return Err(mismatch(
                "the approved brief pins no hook_policy_sha256 of 64 lower-case hex digits",
                "hook_policy_sha256",
                brief.get("hook_policy_sha256").cloned(),
                None,
            ));
        }
    };
    let transition = brief.get("transition").filter(|value| value.is_object());
    let bound = transition.and_then(|transition| transition.get("hook_policy_sha256"));
    if bound.and_then(Value::as_str) != Some(pinned) {
        return Err(mismatch(
            "the transition of the brief does not bind its hook_policy_sha256",
            "transition.hook_policy_sha256",
            Some(json!(pinned)),
            bound.cloned(),
        ));
    }
    let transition = transition.unwrap_or(&Value::Null);
    let digest = compact_sha256(transition);
    for (binding, value) in [
        ("transition_digest", brief.get("transition_digest")),
        (
            "coordinator_approval.transition_digest",
            brief
                .get("coordinator_approval")
                .and_then(|approval| approval.get("transition_digest")),
        ),
    ] {
        if value.and_then(Value::as_str) != Some(digest.as_str()) {
            return Err(mismatch(
                "the approval does not bind the transition the brief holds",
                binding,
                value.cloned(),
                Some(json!(digest)),
            ));
        }
    }
    let configuration = brief
        .get("orchestration_policy")
        .and_then(|policy| policy.get("configuration"))
        .and_then(Value::as_object);
    let Some(configuration) = configuration else {
        return Err(mismatch(
            "the approved brief pins no configuration snapshot",
            "orchestration_policy.configuration",
            None,
            None,
        ));
    };
    let mut body = configuration.clone();
    body.remove("source_sha256");
    let stored = body.remove("canonical_sha256");
    let canonical = sha256_hex(canonical_json(&Value::Object(body)).as_bytes());
    if stored.as_ref().and_then(Value::as_str) != Some(canonical.as_str()) {
        return Err(mismatch(
            "the pinned configuration snapshot does not hash to its canonical_sha256",
            "orchestration_policy.configuration.canonical_sha256",
            stored,
            Some(json!(canonical)),
        ));
    }
    let bound = transition.get("configuration_sha256");
    if bound.and_then(Value::as_str) != Some(canonical.as_str()) {
        return Err(mismatch(
            "the transition does not bind the pinned configuration snapshot",
            "transition.configuration_sha256",
            bound.cloned(),
            Some(json!(canonical)),
        ));
    }
    let project = configuration.get("project").and_then(Value::as_object);
    let computed = project
        .ok_or_else(|| "the pinned configuration holds no project".to_owned())
        .and_then(|project| normalize(project.get("hook_policy")))
        .map(|policy| sha256(&policy));
    match computed {
        Ok(computed) if computed == pinned => Ok(()),
        Ok(computed) => Err(mismatch(
            "the hook policy of the pinned configuration does not hash to the hook_policy_sha256 of the brief",
            "orchestration_policy.configuration.project.hook_policy",
            Some(json!(pinned)),
            Some(json!(computed)),
        )),
        Err(problem) => Err(mismatch(
            &format!("the hook policy of the pinned configuration cannot be normalized: {problem}"),
            "orchestration_policy.configuration.project.hook_policy",
            Some(json!(pinned)),
            None,
        )),
    }
}

fn is_sha256(text: &str) -> bool {
    text.len() == 64
        && text
            .bytes()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
}

fn mismatch(
    message: &str,
    binding: &str,
    pinned: Option<Value>,
    computed: Option<Value>,
) -> ErrorBody {
    ErrorBody::new(
        ErrorCode::ApprovalMismatch,
        FailureDomain::Policy,
        false,
        message,
        MISMATCH_REMEDY,
    )
    .with_fact("binding", binding)
    .with_fact("pinned", pinned.unwrap_or(Value::Null))
    .with_fact("computed", computed.unwrap_or(Value::Null))
}

/// The normalized policy of the `hook_policy` value: absent or `null` is the default profile
/// without escalations; the paths and roles of an escalation are sorted without repeats, a
/// missing list is empty; the escalations are sorted by their default `json.dumps(sort_keys=True)`
/// text, as Python sorts them.
///
/// # Errors
/// What makes the shape one Python refuses.
pub fn normalize(value: Option<&Value>) -> Result<Value, String> {
    let Some(value) = value.filter(|value| !value.is_null()) else {
        return Ok(json!({"profile": DEFAULT_PROFILE, "escalations": []}));
    };
    let policy = value.as_object().ok_or("hook_policy is not an object")?;
    only_fields(policy, &["profile", "escalations"], "hook_policy")?;
    let profile = profile(policy, "hook_policy")?;
    let items: &[Value] = match policy.get("escalations") {
        None => &[],
        Some(Value::Array(items)) => items,
        Some(_) => return Err("hook_policy.escalations is not a list".to_owned()),
    };
    let mut escalations = items
        .iter()
        .map(escalation)
        .collect::<Result<Vec<Value>, String>>()?;
    escalations.sort_by_cached_key(sort_key_json);
    Ok(json!({"profile": profile, "escalations": escalations}))
}

/// The digest of a normalized policy: `hook_policy_sha256` of Python.
pub fn sha256(policy: &Value) -> String {
    compact_sha256(policy)
}

fn escalation(item: &Value) -> Result<Value, String> {
    let item = item
        .as_object()
        .ok_or("a hook_policy escalation is not an object")?;
    only_fields(
        item,
        &["paths", "roles", "profile"],
        "a hook_policy escalation",
    )?;
    Ok(json!({
        "paths": strings(item.get("paths"), "paths")?,
        "roles": strings(item.get("roles"), "roles")?,
        "profile": profile(item, "a hook_policy escalation")?,
    }))
}

fn only_fields(record: &Map<String, Value>, fields: &[&str], what: &str) -> Result<(), String> {
    match record.keys().find(|key| !fields.contains(&key.as_str())) {
        Some(key) => Err(format!("{what} has the unknown field {key:?}")),
        None => Ok(()),
    }
}

fn profile<'a>(record: &'a Map<String, Value>, what: &str) -> Result<&'a str, String> {
    record
        .get("profile")
        .and_then(Value::as_str)
        .ok_or_else(|| format!("{what} names no profile"))
}

/// The sorted strings of a list without repeats; a missing list is empty.
fn strings(value: Option<&Value>, name: &str) -> Result<Vec<String>, String> {
    let Some(value) = value else {
        return Ok(Vec::new());
    };
    let mut items = value
        .as_array()
        .and_then(|items| {
            items
                .iter()
                .map(|item| item.as_str().map(str::to_owned))
                .collect::<Option<Vec<String>>>()
        })
        .ok_or_else(|| {
            format!("the {name} of a hook_policy escalation are not a list of strings")
        })?;
    items.sort();
    items.dedup();
    Ok(items)
}
