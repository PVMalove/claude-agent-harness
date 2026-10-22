//! The batch-contract checks of `ledger validate` (ADR 0020, section 5, step 1).
//!
//! The Python command validates the generation, then calls `_validate_batch_integrity` for every
//! batch: the sealed records a batch carries (recovery events, startup evidence, context
//! pressure, carried items, attention, `auto` decisions), their binding to the dispatches and
//! the audit records, and the equality of the batch with its immutable plan. Installation 2.0.0
//! has no Python to ask, so this module repeats those checks. A state that `ledger validate`
//! rejects is refused here too, and nothing is changed.
//! `tests/orchestration/test_runtime_store_migrate.py` runs both on the same states.

use std::collections::BTreeSet;
use std::fs;
use std::path::{Component, Path, PathBuf};

use serde_json::{Map, Value, json};

use crate::canonical::object_sha256;
use crate::error::{Error, Result};
use crate::vocabulary::{
    ATTENTION_EVENT_KINDS, ATTENTION_REASONS, ATTENTION_STATE_FIELDS, AUTO_DECISION_FIELDS,
    AUTO_DECISION_KINDS, AUTO_EVIDENCE_BATCH_APPROVE, AUTO_EVIDENCE_CARRY_OVER,
    AUTO_EVIDENCE_CONTINUATION, AUTO_EVIDENCE_DECISION, AUTO_EVIDENCE_DISPATCH, AUTO_REPORT_FIELDS,
    AUTO_STOP_BUDGET_EXHAUSTED, AUTO_STOP_FIELDS, AUTO_STOP_INTEGRITY_FAILURE,
    AUTO_STOP_NO_AUTOMATIC_ROUTE, CARRIED_ITEM_RECORD_FIELDS, CONTEXT_PRESSURE_FIELDS,
    CONTEXT_TELEMETRY_SOURCES, PLAN_FIELDS, PLAN_FIELDS_BEFORE_PIN, PRESERVED_PROGRESS_FIELDS,
    RECOVERY_EVENT_FIELDS, RECOVERY_ROUTES, RETRY_REASONS, STARTUP_EVIDENCE_FIELDS,
};

type Object = Map<String, Value>;
/// The JSON records of the selected generation, keyed by their path in it.
pub type Records<'a> = std::collections::BTreeMap<&'a str, Object>;
/// A failed check names what is wrong, in the words of the Python validator.
type Check = std::result::Result<(), String>;

static NULL: Value = Value::Null;

const AUTO_APPROVER: &str = "policy:auto";
const REMEDY: &str = "restore the record from a backup or the snapshot of installation 1.x; \
    'coordinator.py ledger validate' on installation 1.x names the same defect";

/// The field a plan written before explicit scopes did not have.
const WITHOUT_SCOPE: &str = "allowed_paths";

fn fail<T>(message: impl Into<String>) -> std::result::Result<T, String> {
    Err(message.into())
}

fn field<'a>(object: &'a Object, key: &str) -> &'a Value {
    object.get(key).unwrap_or(&NULL)
}

/// The list at `key`; an absent key is an empty list, any other value is not a list.
fn list<'a>(object: &'a Object, key: &str) -> std::result::Result<&'a [Value], String> {
    match object.get(key) {
        None => Ok(&[]),
        Some(Value::Array(items)) => Ok(items),
        Some(_) => fail(format!("{key} is not a list")),
    }
}

/// Python's `bool(value)`.
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(flag) => *flag,
        Value::Number(number) => number.as_f64().is_some_and(|found| found != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(items) => !items.is_empty(),
        Value::Object(members) => !members.is_empty(),
    }
}

/// How Python prints the value in a message: a string in single quotes.
fn python_repr(value: &Value) -> String {
    value
        .as_str()
        .map_or_else(|| value.to_string(), |text| format!("'{text}'"))
}

/// `_non_empty`: a string with something besides white space.
fn non_empty(value: &Value) -> bool {
    value.as_str().is_some_and(|text| !text.trim().is_empty())
}

fn is_hex(value: &Value, length: &[usize]) -> bool {
    value.as_str().is_some_and(|text| {
        length.contains(&text.len())
            && text
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    })
}

fn exact_keys(object: &Object, keys: &[&str]) -> bool {
    object.len() == keys.len() && keys.iter().all(|key| object.contains_key(*key))
}

/// A record whose `record_sha256` is the hash of the rest of it (`sealed`).
fn is_sealed(record: &Object) -> bool {
    let mut body = record.clone();
    let Some(checksum) = body.remove("record_sha256") else {
        return false;
    };
    checksum.as_str() == Some(object_sha256(&body).as_str())
}

/// `_check_sealed`: a sealed record of exactly `keys`.
fn sealed_record<'a>(
    entry: &'a Value,
    keys: &[&str],
) -> std::result::Result<&'a Object, &'static str> {
    match entry.as_object() {
        Some(record) if exact_keys(record, keys) => {
            if is_sealed(record) {
                Ok(record)
            } else {
                Err("failed immutable integrity check")
            }
        }
        _ => Err("schema mismatch"),
    }
}

/// `_safe_id`: a coordinator-generated id.
fn is_safe_id(value: &Value) -> bool {
    let Some(text) = value.as_str() else {
        return false;
    };
    ["batch", "dispatch", "risk", "context-package", "checkpoint"]
        .iter()
        .filter_map(|prefix| text.strip_prefix(prefix)?.strip_prefix('-'))
        .any(|rest| {
            !rest.is_empty()
                && rest.bytes().all(|byte| {
                    byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte) || byte == b'-'
                })
        })
}

/// `_load_dispatch`: the dispatch record as `DispatchRecord.to_dict` writes it.
fn load_dispatch(records: &Records<'_>, id: &Value) -> std::result::Result<Object, String> {
    if !is_safe_id(id) {
        return fail("dispatch is not a valid coordinator ID");
    }
    let path = format!("dispatches/{}.json", id.as_str().unwrap_or_default());
    let Some(record) = records.get(path.as_str()) else {
        return fail(format!("dispatch record is not valid JSON: {path}"));
    };
    let mut brief = record.clone();
    brief
        .entry("coordinator_approval".to_owned())
        .or_insert(Value::Null);
    Ok(brief)
}

/// The audit records of the generation, in path order.
fn audit_records<'a>(records: &'a Records<'a>) -> Vec<&'a Object> {
    records
        .iter()
        .filter(|(path, _)| crate::legacy::is_direct(path, "audit"))
        .map(|(_, record)| record)
        .collect()
}

/// Whether an audit record names `decision` for the batch (`details.path` and `details.decision`).
fn has_audit(audits: &[&Object], batch_id: &str, decision: &Value) -> bool {
    let path = format!("batches/{batch_id}.json");
    audits.iter().any(|audit| {
        audit
            .get("details")
            .and_then(Value::as_object)
            .is_some_and(|details| {
                details.get("path").and_then(Value::as_str) == Some(path.as_str())
                    && details.get("decision") == Some(decision)
            })
    })
}

/// Python's `fnmatch.fnmatchcase`: `*` and `?` match any character, `/` included.
fn fnmatch(text: &[char], pattern: &[char]) -> bool {
    let Some((&head, rest)) = pattern.split_first() else {
        return text.is_empty();
    };
    match head {
        '*' => (0..=text.len()).any(|skip| fnmatch(&text[skip..], rest)),
        '?' => !text.is_empty() && fnmatch(&text[1..], rest),
        '[' => match class_end(pattern) {
            Some(end) => {
                !text.is_empty()
                    && in_class(text[0], &pattern[1..end])
                    && fnmatch(&text[1..], &pattern[end + 1..])
            }
            None => text.first() == Some(&'[') && fnmatch(&text[1..], rest),
        },
        literal => text.first() == Some(&literal) && fnmatch(&text[1..], rest),
    }
}

/// The index of the `]` that closes the class that starts `pattern`.
fn class_end(pattern: &[char]) -> Option<usize> {
    let mut index = 1;
    if pattern.get(index) == Some(&'!') {
        index += 1;
    }
    if pattern.get(index) == Some(&']') {
        index += 1;
    }
    pattern[index.min(pattern.len())..]
        .iter()
        .position(|&found| found == ']')
        .map(|offset| index + offset)
}

fn in_class(character: char, class: &[char]) -> bool {
    let (negated, class) = match class.split_first() {
        Some(('!', rest)) => (true, rest),
        _ => (false, class),
    };
    let mut found = false;
    let mut index = 0;
    while index < class.len() {
        if class.get(index + 1) == Some(&'-') && index + 2 < class.len() {
            found |= class[index] <= character && character <= class[index + 2];
            index += 3;
        } else {
            found |= class[index] == character;
            index += 1;
        }
    }
    found != negated
}

/// `_valid_path` of the preserved progress: a relative POSIX path with no `.` or `..` part.
fn is_scope_path(path: &str) -> bool {
    !path.is_empty()
        && !path.contains('\\')
        && !path.starts_with('/')
        && path.split('/').all(|part| !matches!(part, "" | "." | ".."))
}

/// `_inside_scope`: every path is valid and matches one pattern of `scope`.
fn inside_scope(paths: &Object, scope: &Value) -> std::result::Result<bool, String> {
    let patterns: Vec<Vec<char>> = match scope {
        Value::Null => Vec::new(),
        Value::Array(items) => items
            .iter()
            .map(|item| {
                item.as_str()
                    .map(|text| text.chars().collect())
                    .ok_or_else(|| "a write path is not a string".to_owned())
            })
            .collect::<std::result::Result<_, _>>()?,
        _ => return fail("write_paths is not a list"),
    };
    Ok(paths.keys().all(|path| {
        let text: Vec<char> = path.chars().collect();
        is_scope_path(path) && patterns.iter().any(|pattern| fnmatch(&text, pattern))
    }))
}

/// `Path.resolve()`: symbolic links resolved for the part that exists, `.` and `..` removed.
fn resolve(path: &str) -> PathBuf {
    let path = Path::new(path);
    let mut existing = path.to_path_buf();
    let mut tail = Vec::new();
    let base = loop {
        if let Ok(found) = fs::canonicalize(&existing) {
            break found;
        }
        match existing.file_name() {
            Some(name) => tail.push(name.to_owned()),
            None => break existing.clone(),
        }
        if !existing.pop() {
            break PathBuf::new();
        }
    };
    let mut resolved = base;
    for name in tail.into_iter().rev() {
        match Path::new(&name).components().next() {
            Some(Component::ParentDir) => {
                resolved.pop();
            }
            Some(Component::CurDir) | None => {}
            Some(_) => resolved.push(name),
        }
    }
    resolved
}

// --- recovery events ---

fn preserved_progress(
    records: &Records<'_>,
    batch: &Object,
    event: &Object,
    evidence: &Object,
) -> Check {
    let progress = match evidence.get("preserved_progress") {
        None | Some(Value::Null) => return Ok(()),
        Some(Value::Object(progress)) => progress,
        Some(_) => return fail("invalid preserved writer evidence"),
    };
    let source = field(progress, "source_dispatch_id");
    let entry = list(batch, "dispatches")?
        .iter()
        .filter_map(Value::as_object)
        .find(|entry| field(entry, "dispatch_id") == source);
    let brief = match (entry, is_safe_id(source)) {
        (Some(_), true) => load_dispatch(records, source)?,
        _ => Object::new(),
    };
    let next_action = field(event, "next_action").as_str().unwrap_or_default();
    let role = match next_action {
        "developer" => Some("developer"),
        "resolve-conflict" => Some("conflict-resolver"),
        _ => None,
    };
    let superseded = evidence
        .get("superseded_dispatches")
        .and_then(Value::as_array)
        .is_some_and(|items| items.contains(source));
    let cancellation = match entry.and_then(|entry| entry.get("cancellation")) {
        None => None,
        Some(Value::Object(found)) => Some(found),
        Some(_) => return fail("preserved writer evidence has an invalid cancellation"),
    };
    let head = field(progress, "head_commit");
    let files = progress.get("files").and_then(Value::as_object);
    let diverged = !exact_keys(progress, &PRESERVED_PROGRESS_FIELDS)
        || field(event, "kind").as_str() != Some("rewind")
        || role.is_none()
        || !superseded
        || field(&brief, "batch_id") != field(batch, "batch_id")
        || field(&brief, "role").as_str() != role
        || field(&brief, "purpose").as_str() != Some("work")
        || entry.is_some_and(|entry| truthy(field(entry, "report")))
        || entry
            .and_then(|entry| entry.get("state"))
            .and_then(Value::as_str)
            != Some("cancelled")
        || !cancellation.is_some_and(|found| truthy(field(found, "runtime_stopped")))
        || field(progress, "snapshot_commit") != field(&brief, "snapshot_commit")
        || head != field(evidence, "writer_start_commit")
        || !is_hex(head, &[40, 64])
        || files.is_none_or(|paths| {
            !inside_scope(paths, field(&brief, "write_paths")).unwrap_or(false)
        })
        || ["index_sha256", "worktree_sha256"]
            .iter()
            .any(|key| !is_hex(field(progress, key), &[64]));
    if diverged {
        return fail("preserved writer evidence diverged from its stopped dispatch");
    }
    Ok(())
}

fn recovery_events(records: &Records<'_>, batch: &Object) -> Check {
    let events = list(batch, "recovery_events")?;
    let audits = if events.is_empty() {
        Vec::new()
    } else {
        audit_records(records)
    };
    let mut runtime = field(batch, "harness_runtime_sha256").clone();
    for (index, event) in events.iter().enumerate() {
        let Some(event) = event
            .as_object()
            .filter(|event| exact_keys(event, &RECOVERY_EVENT_FIELDS) && is_sealed(event))
        else {
            return fail("recovery event failed integrity validation");
        };
        let kind = field(event, "kind").as_str().unwrap_or_default();
        let evidence = event.get("evidence").and_then(Value::as_object);
        if field(event, "sequence").as_u64() != Some(index as u64 + 1)
            || !matches!(kind, "pause" | "resume-stop" | "rewind")
            || evidence.is_none()
        {
            return fail("invalid recovery event schema");
        }
        let evidence = evidence.unwrap_or(&Object::new()).clone();
        let policy = kind == "pause";
        let upgrade = field(&evidence, "runtime_upgrade");
        if !upgrade.is_null() {
            let valid = !policy
                && upgrade.as_object().is_some_and(|upgrade| {
                    exact_keys(upgrade, &["from", "to", "original"])
                        && field(upgrade, "from") == &runtime
                        && field(upgrade, "original") == field(batch, "harness_runtime_sha256")
                        && is_hex(field(upgrade, "to"), &[64])
                });
            if !valid {
                return fail("invalid control runtime upgrade");
            }
            runtime = field(upgrade.as_object().unwrap_or(&Object::new()), "to").clone();
        }
        let approver = field(event, "approved_by");
        if !non_empty(approver)
            || !non_empty(field(event, "approved_at"))
            || policy != (approver.as_str() == Some(AUTO_APPROVER))
            || (!policy
                && approver
                    .as_str()
                    .is_some_and(|name| name.starts_with("policy:")))
        {
            return fail("invalid recovery approver");
        }
        if !field(event, "dispatch_id").is_null() {
            let brief = load_dispatch(records, field(event, "dispatch_id"))?;
            if field(&brief, "batch_id") != field(batch, "batch_id")
                || ["snapshot_commit", "transition_digest"]
                    .iter()
                    .any(|key| field(event, key) != field(&brief, key))
            {
                return fail("recovery event diverged from source dispatch");
            }
        }
        preserved_progress(records, batch, event, &evidence)?;
        let decision = json!({
            "decision": kind,
            "dispatch_id": field(event, "dispatch_id"),
            "approver": {"kind": if policy { "policy" } else { "human" }, "name": approver},
            "approved_at": field(event, "approved_at"),
            "evidence": {"recovery_event": event},
        });
        let batch_id = field(batch, "batch_id").as_str().unwrap_or_default();
        if !has_audit(&audits, batch_id, &decision) {
            return fail("recovery event has no matching audit transition");
        }
        for (name, entries, key) in [
            ("superseded_dispatches", "dispatches", "dispatch_id"),
            ("superseded_risks", "risk_assessments", "risk_assessment_id"),
            (
                "superseded_candidate_registrations",
                "candidate_registrations",
                "source_dispatch_id",
            ),
        ] {
            let mut known = BTreeSet::new();
            for entry in list(batch, entries)? {
                let Some(id) = entry.as_object().and_then(|entry| entry.get(key)) else {
                    return fail(format!("a {entries} entry has no {key}"));
                };
                known.insert(id.as_str().unwrap_or_default());
            }
            let values = match evidence.get(name) {
                None => &[][..],
                Some(Value::Array(items)) => items,
                Some(_) => return fail("recovery supersedes unknown evidence"),
            };
            if values
                .iter()
                .any(|value| value.as_str().is_none_or(|id| !known.contains(id)))
            {
                return fail("recovery supersedes unknown evidence");
            }
        }
    }
    let recovered = events.iter().any(|event| {
        matches!(
            field(event.as_object().unwrap_or(&Object::new()), "kind").as_str(),
            Some("resume-stop" | "rewind")
        )
    });
    if truthy(field(batch, "manual_recovery")) != recovered {
        return fail("manual recovery flag diverged from audited events");
    }
    match batch.get("environmental_restart") {
        None | Some(Value::Null) => Ok(()),
        Some(Value::Object(restart)) => {
            let empty = Object::new();
            let source = events.last().and_then(Value::as_object).unwrap_or(&empty);
            if field(source, "kind").as_str() == Some("resume-stop")
                && field(restart, "dispatch_id") == field(source, "dispatch_id")
                && field(restart, "snapshot_commit") == field(source, "snapshot_commit")
            {
                Ok(())
            } else {
                fail("environment restart diverged from recovery event")
            }
        }
        Some(_) => fail("environment restart diverged from recovery event"),
    }
}

// --- startup evidence ---

fn startup_evidence(records: &Records<'_>, batch: &Object) -> Check {
    let points = list(batch, "startup_evidence")?;
    let audits = if points.is_empty() {
        Vec::new()
    } else {
        audit_records(records)
    };
    for point in points {
        let Some(point) = point
            .as_object()
            .filter(|point| exact_keys(point, &STARTUP_EVIDENCE_FIELDS) && is_sealed(point))
        else {
            return fail("startup evidence failed integrity validation");
        };
        let brief = load_dispatch(records, field(point, "dispatch_id"))?;
        let scope: Object = [
            "definition_of_done",
            "dependencies",
            "write_paths",
            "prohibited_changes",
        ]
        .iter()
        .map(|key| {
            brief
                .get(*key)
                .map(|value| ((*key).to_owned(), value.clone()))
                .ok_or_else(|| format!("the dispatch has no {key}"))
        })
        .collect::<std::result::Result<_, _>>()?;
        let same_worktree = match (
            field(point, "worktree").as_str(),
            field(batch, "worktree").as_str(),
        ) {
            (Some(left), Some(right)) => left == right || resolve(left) == resolve(right),
            _ => return fail("startup evidence has no worktree"),
        };
        if field(&brief, "batch_id") != field(batch, "batch_id")
            || field(point, "commit_sha") != field(&brief, "snapshot_commit")
            || field(point, "brief_sha256").as_str() != Some(object_sha256(&brief).as_str())
            || field(point, "scope_sha256").as_str() != Some(object_sha256(&scope).as_str())
            || !same_worktree
        {
            return fail("startup evidence diverged from immutable brief");
        }
        let decision = json!({
            "decision": "startup-evidence",
            "dispatch_id": field(point, "dispatch_id"),
            "evidence": {"startup": point},
        });
        let batch_id = field(batch, "batch_id").as_str().unwrap_or_default();
        if !has_audit(&audits, batch_id, &decision) {
            return fail("startup evidence lacks its recorded audit");
        }
    }
    Ok(())
}

// --- operational fields ---

fn routing_records(batch: &Object) -> Check {
    let mut decisions: Vec<&Value> = Vec::new();
    for entry in list(batch, "dispatches")?
        .iter()
        .filter_map(Value::as_object)
    {
        decisions.push(field(entry, "decision"));
    }
    decisions.extend(list(batch, "coordinator_decisions")?);
    for decision in decisions {
        let Some(routing) = decision
            .as_object()
            .and_then(|decision| decision.get("routing"))
        else {
            continue;
        };
        let Some(routing) = routing.as_object() else {
            continue;
        };
        if let Some(route) = routing.get("route")
            && !route
                .as_str()
                .is_some_and(|text| RECOVERY_ROUTES.contains(&text))
        {
            return fail(format!(
                "batch routing record carries an unknown recovery route {}",
                python_repr(route)
            ));
        }
        if let Some(category) = routing.get("reason_category")
            && !category.is_null()
            && !category
                .as_str()
                .is_some_and(|text| RETRY_REASONS.contains(&text))
        {
            return fail(format!(
                "batch routing record carries an unknown retry reason category {}",
                python_repr(category)
            ));
        }
    }
    Ok(())
}

fn context_pressure(batch: &Object) -> Check {
    for entry in list(batch, "context_pressure")? {
        let Some(entry) = entry
            .as_object()
            .filter(|entry| exact_keys(entry, &CONTEXT_PRESSURE_FIELDS))
        else {
            return fail("batch context_pressure record schema mismatch");
        };
        if !is_sealed(entry) {
            return fail("batch context_pressure record failed immutable integrity check");
        }
        let number = |key: &str| field(entry, key).as_i64().filter(|found| *found >= 0);
        let (Some(observed), Some(limit), Some(warning)) = (
            number("observed_tokens"),
            number("context_limit"),
            number("warning_threshold"),
        ) else {
            return fail("batch context_pressure record has invalid token numbers");
        };
        if warning > limit {
            return fail("batch context_pressure record has invalid token numbers");
        }
        let level = if observed >= limit {
            "critical"
        } else if observed >= warning {
            "warning"
        } else {
            "ok"
        };
        if field(entry, "level").as_str() != Some(level)
            || !field(entry, "source")
                .as_str()
                .is_some_and(|source| CONTEXT_TELEMETRY_SOURCES.contains(&source))
        {
            return fail("batch context_pressure level or source does not match its record");
        }
    }
    Ok(())
}

fn attention(batch: &Object) -> Check {
    if let Some(flag) = batch.get("needs_attention") {
        let Some(flag) = flag.as_bool() else {
            return fail("batch needs_attention must be a boolean");
        };
        if flag
            && (!ATTENTION_STATE_FIELDS
                .iter()
                .all(|key| non_empty(field(batch, key)))
                || !field(batch, "attention_reason")
                    .as_str()
                    .is_some_and(|reason| ATTENTION_REASONS.contains(&reason)))
        {
            return fail(
                "a batch that needs attention must record its reason, time, last safe action \
                 and recommended human action",
            );
        }
    }
    for event in list(batch, "attention_events")? {
        let valid = event.as_object().is_some_and(|event| {
            field(event, "event")
                .as_str()
                .is_some_and(|kind| ATTENTION_EVENT_KINDS.contains(&kind))
                && non_empty(field(event, "at"))
        });
        if !valid {
            return fail("batch attention_events entry is malformed");
        }
    }
    for key in ["attention_open_keys", "attention_acknowledged"] {
        if batch.contains_key(key) && !list(batch, key)?.iter().all(non_empty) {
            return fail(format!("batch {key} must be a list of keys"));
        }
    }
    Ok(())
}

fn auto_evidence(kind: &str) -> &'static [&'static str] {
    match kind {
        "batch-approve" => &AUTO_EVIDENCE_BATCH_APPROVE,
        "dispatch" => &AUTO_EVIDENCE_DISPATCH,
        "continuation" => &AUTO_EVIDENCE_CONTINUATION,
        "decision" => &AUTO_EVIDENCE_DECISION,
        _ => &AUTO_EVIDENCE_CARRY_OVER,
    }
}

/// `_auto_decision_bound`: the approval an `auto_decisions` record names is the batch's own.
fn auto_decision_bound(batch: &Object, entry: &Object) -> std::result::Result<bool, String> {
    let kind = field(entry, "kind").as_str().unwrap_or_default();
    let evidence = field(entry, "evidence")
        .as_object()
        .cloned()
        .unwrap_or_default();
    let approver = field(entry, "approved_by");
    if kind == "batch-approve" {
        return Ok(field(batch, "coordinator_approval")
            .as_object()
            .is_some_and(|approval| field(approval, "approved_by") == approver));
    }
    if matches!(kind, "dispatch" | "decision") {
        let dispatch = list(batch, "dispatches")?
            .iter()
            .filter_map(Value::as_object)
            .find(|item| field(item, "dispatch_id") == field(entry, "dispatch_id"));
        let Some(dispatch) = dispatch else {
            return Ok(false);
        };
        if kind == "dispatch" {
            return Ok(field(dispatch, "brief_sha256") == field(&evidence, "brief_sha256"));
        }
        return Ok(field(dispatch, "decision")
            .as_object()
            .is_some_and(|decision| {
                field(decision, "approved_by") == approver
                    && field(dispatch, "report_sha256") == field(&evidence, "report_sha256")
            }));
    }
    let recorded = if kind == "carry-over" {
        "carry-over"
    } else {
        "continue"
    };
    Ok(list(batch, "coordinator_decisions")?
        .iter()
        .filter_map(Value::as_object)
        .any(|item| {
            field(item, "dispatch_id") == field(entry, "dispatch_id")
                && field(item, "decision").as_str() == Some(recorded)
                && field(item, "approved_by") == approver
        }))
}

fn auto_records(batch: &Object) -> Check {
    let auto_error = |name: &str, problem: &str| format!("batch {name} record {problem}");
    for (index, item) in list(batch, "auto_decisions")?.iter().enumerate() {
        let entry = sealed_record(item, &AUTO_DECISION_FIELDS)
            .map_err(|problem| auto_error("auto_decisions", problem))?;
        let kind = field(entry, "kind").as_str().unwrap_or_default();
        let evidence = field(entry, "evidence").as_object();
        if field(entry, "sequence").as_u64() != Some(index as u64 + 1)
            || !AUTO_DECISION_KINDS.contains(&kind)
            || field(entry, "approved_by").as_str() != Some(AUTO_APPROVER)
            || !non_empty(field(entry, "approved_at"))
            || !non_empty(field(entry, "rationale"))
            || evidence.is_none_or(|evidence| !exact_keys(evidence, auto_evidence(kind)))
        {
            return fail(auto_error("auto_decisions", "has an invalid entry"));
        }
        if !auto_decision_bound(batch, entry)? {
            return fail(auto_error(
                "auto_decisions",
                &format!(
                    "{} does not match the {kind} approval it records",
                    index + 1
                ),
            ));
        }
    }
    if let Some(stop) = batch.get("auto_stop") {
        let stop = sealed_record(stop, &AUTO_STOP_FIELDS)
            .map_err(|problem| auto_error("auto_stop", problem))?;
        let reasons: &[&str] = match field(stop, "category").as_str() {
            Some("budget-exhausted") => &AUTO_STOP_BUDGET_EXHAUSTED,
            Some("integrity-failure") => &AUTO_STOP_INTEGRITY_FAILURE,
            Some("no-automatic-route") => &AUTO_STOP_NO_AUTOMATIC_ROUTE,
            _ => &[],
        };
        if !field(stop, "reason")
            .as_str()
            .is_some_and(|reason| reasons.contains(&reason))
            || !field(stop, "evidence").is_object()
            || !non_empty(field(stop, "detected_at"))
            || !non_empty(field(stop, "detected_by"))
        {
            return fail(auto_error("auto_stop", "names no listed stop"));
        }
    }
    if let Some(report) = batch.get("auto_report") {
        let report = sealed_record(report, &AUTO_REPORT_FIELDS)
            .map_err(|problem| auto_error("auto_report", problem))?;
        if field(report, "batch_id") != field(batch, "batch_id")
            || field(report, "stop") != field(batch, "auto_stop")
        {
            return fail(auto_error(
                "auto_report",
                "does not match its batch or its recorded stop",
            ));
        }
    }
    Ok(())
}

fn carried_items(batch: &Object) -> Check {
    for entry in list(batch, "carried_items")? {
        let Some(entry) = entry
            .as_object()
            .filter(|entry| exact_keys(entry, &CARRIED_ITEM_RECORD_FIELDS))
        else {
            return fail("batch carried_items record schema mismatch");
        };
        if !is_sealed(entry) {
            return fail("batch carried_items record failed immutable integrity check");
        }
    }
    Ok(())
}

fn operational_fields(batch: &Object) -> Check {
    routing_records(batch)?;
    context_pressure(batch)?;
    carried_items(batch)?;
    attention(batch)?;
    auto_records(batch)
}

// --- the immutable plan ---

fn same_fields(batch: &Object, plan: &Object, fields: &[&str]) -> Option<bool> {
    (fields.iter().all(|key| batch.contains_key(*key))
        && fields.iter().all(|key| plan.contains_key(*key)))
    .then(|| fields.iter().all(|key| batch.get(*key) == plan.get(*key)))
}

fn immutable_plan(records: &Records<'_>, batch: &Object) -> Check {
    let id = field(batch, "batch_id");
    if !is_safe_id(id) {
        return fail("batch is not a valid coordinator ID");
    }
    let path = format!("plans/{}.json", id.as_str().unwrap_or_default());
    let Some(plan) = records.get(path.as_str()) else {
        return fail("the immutable batch plan is missing");
    };
    if field(batch, "goal") != field(plan, "goal") {
        return fail("batch goal does not match its immutable plan");
    }
    if field(batch, "supersedes") != field(plan, "supersedes") {
        return fail("batch supersedes link does not match its immutable plan");
    }
    for key in ["approval_policy", "communication_policy", "allowed_paths"] {
        if batch.contains_key(key) != plan.contains_key(key) {
            return fail("batch record is incomplete");
        }
    }
    if field(batch, "allowed_paths") != field(plan, "allowed_paths") {
        return fail("batch record does not match its immutable plan");
    }
    let current: Vec<&str> = PLAN_FIELDS.to_vec();
    let before_scope: Vec<&str> = PLAN_FIELDS
        .iter()
        .copied()
        .filter(|key| *key != WITHOUT_SCOPE)
        .collect();
    let legacy: Vec<&str> = PLAN_FIELDS
        .iter()
        .copied()
        .filter(|key| !PLAN_FIELDS_BEFORE_PIN.contains(key))
        .collect();
    for fields in [&current, &before_scope, &legacy] {
        if let Some(equal) = same_fields(batch, plan, fields) {
            return if equal {
                Ok(())
            } else {
                fail("batch record does not match its immutable plan")
            };
        }
    }
    let before_approval: Vec<&str> = legacy
        .iter()
        .copied()
        .filter(|key| *key != "approval_policy")
        .collect();
    if !batch.contains_key("approval_policy")
        && !plan.contains_key("approval_policy")
        && same_fields(batch, plan, &before_approval) == Some(true)
    {
        return Ok(());
    }
    fail("batch record is incomplete")
}

/// Check every batch of the selected generation against the batch contract of the ledger.
///
/// # Errors
/// `Error::Refused` names the first batch that `ledger validate` rejects, and why.
pub fn validate_batches(records: &Records<'_>) -> Result<()> {
    for (path, batch) in records
        .iter()
        .filter(|(path, _)| crate::legacy::is_direct(path, "batches"))
    {
        let checked = recovery_events(records, batch)
            .and_then(|()| startup_evidence(records, batch))
            .and_then(|()| operational_fields(batch))
            .and_then(|()| immutable_plan(records, batch));
        if let Err(reason) = checked {
            return Err(Error::refused(format!("{reason}: {path}"), REMEDY));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn matches(text: &str, pattern: &str) -> bool {
        fnmatch(
            &text.chars().collect::<Vec<_>>(),
            &pattern.chars().collect::<Vec<_>>(),
        )
    }

    #[test]
    fn a_glob_matches_as_python_fnmatchcase_does() {
        assert!(matches("a/b/c.py", "a/**"));
        assert!(matches("a/b/c.py", "a/*.py"));
        assert!(matches("a/b", "a/?"));
        assert!(!matches("a/bc", "a/?"));
        assert!(matches("a/x1", "a/x[0-9]"));
        assert!(!matches("a/xa", "a/x[0-9]"));
        assert!(matches("a/xa", "a/x[!0-9]"));
        assert!(matches("a/]", "a/[]]"));
        assert!(matches("a/[", "a/["));
        assert!(!matches("A/b", "a/b"));
        assert!(matches("", "*"));
    }

    #[test]
    fn a_scope_path_has_no_dot_part() {
        assert!(is_scope_path("a/b.py"));
        assert!(!is_scope_path("a/../b.py"));
        assert!(!is_scope_path("/a"));
        assert!(!is_scope_path("a\\b"));
        assert!(!is_scope_path("a//b"));
        assert!(!is_scope_path(""));
    }

    #[test]
    fn a_coordinator_id_is_a_known_prefix_and_lower_case_hex() {
        assert!(is_safe_id(&json!("batch-0a1b-2c")));
        assert!(is_safe_id(&json!("context-package-0001")));
        assert!(!is_safe_id(&json!("batch-old")));
        assert!(!is_safe_id(&json!("batch-")));
        assert!(!is_safe_id(&json!("other-00")));
        assert!(!is_safe_id(&json!(7)));
    }
}
