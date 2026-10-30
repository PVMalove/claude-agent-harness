//! The hook policy an approved brief pins (ADR 0024, section 4; ADR 0021, operational
//! consequences): its normalized form and its digest, as Python `hook_policy.normalize_policy`
//! and `hook_policy_sha256` compute them. No I/O: the input is a value of the stored brief, never
//! the live `.harness/project.json` or the environment.
//!
//! The runtime does not validate profile names or apply the strict boundary; that stays in Python.
//! It only refuses a shape Python could never have normalized.

use serde_json::{Map, Value, json};
use state::canonical::{compact_sha256, sort_key_json};

/// The profile of a project without a `hook_policy` key.
const DEFAULT_PROFILE: &str = "standard";

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
