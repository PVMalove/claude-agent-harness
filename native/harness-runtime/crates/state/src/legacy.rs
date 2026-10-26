//! The file ledger of installation 1.x, read for import (ADR 0020, section 5).
//!
//! The state root holds `ledger.json` (the pointer), `generations/generation-<hex>/` (the records
//! of one generation) and `lock-audit/`. This module only reads: the legacy files are never
//! rewritten or deleted (ADR 0020, section 4). `validate` repeats in Rust what `ledger validate`
//! checks in Python, because installation 2.0.0 has no Python writer to call.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use contracts::ErrorCode;
use serde_json::{Map, Value};

use crate::canonical::object_sha256;
use crate::error::{Error, Result};
use crate::policy;
use crate::read::valid_path;
use crate::vocabulary::{BATCH_STATES, DISPATCH_STATES};

pub const POINTER_NAME: &str = "ledger.json";
pub const GENERATIONS: &str = "generations";
pub const LOCK_AUDIT: &str = "lock-audit";
/// The version of the file ledger this build imports.
pub const LEGACY_VERSION: i64 = 3;

/// The record directories of one generation, as `RECORD_DIRECTORIES` in the Python ledger.
pub const RECORD_DIRECTORIES: [&str; 11] = [
    "batches",
    "plans",
    "dispatches",
    "dispatch-status",
    "risk-assessments",
    "context-packages",
    "checkpoints",
    "reports",
    "qa-lane",
    "qa-artifacts",
    "audit",
];

/// A regular file with its bytes. `path` uses `/` and is relative to the directory the owner names.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LegacyFile {
    pub path: String,
    pub bytes: Vec<u8>,
}

/// A file of a generation that is not the selected one.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct GenerationFile {
    pub generation: String,
    pub file: LegacyFile,
}

/// The pointer `ledger.json`: exactly `version`, `generation` and `selected_at`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Pointer {
    pub version: i64,
    pub generation: String,
    pub selected_at: String,
}

/// Everything migrate imports.
#[derive(Debug)]
pub struct LegacyState {
    pub pointer: Pointer,
    /// The files of the selected generation; paths are relative to the generation directory.
    pub files: Vec<LegacyFile>,
    /// The directories of the selected generation, relative to it (empty ones included).
    pub directories: BTreeSet<String>,
    /// The files of the other generations.
    pub other_generations: Vec<GenerationFile>,
    /// The files of `lock-audit/`; paths are relative to the state root (`lock-audit/<name>`).
    pub lock_audit: Vec<LegacyFile>,
}

fn invalid(message: impl Into<String>, remedy: impl Into<String>) -> Error {
    Error::refused(ErrorCode::InvariantViolation, message, remedy)
}

/// Read `ledger.json`. `Ok(None)` when the file does not exist.
pub fn read_pointer(root: &Path) -> Result<Option<Pointer>> {
    let path = root.join(POINTER_NAME);
    let bytes = match fs::read(&path) {
        Ok(bytes) => bytes,
        Err(err) if err.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(err) => return Err(Error::unreadable(&path, &err)),
    };
    let corrupted = || {
        invalid(
            "the ledger pointer has an invalid schema",
            format!(
                "fix or remove the corrupted pointer at {} (it must have exactly version, \
                 generation and selected_at)",
                path.display()
            ),
        )
    };
    let Value::Object(members) = serde_json::from_slice(&bytes).map_err(|_| corrupted())? else {
        return Err(corrupted());
    };
    let keys: BTreeSet<&str> = members.keys().map(String::as_str).collect();
    if keys != BTreeSet::from(["version", "generation", "selected_at"]) {
        return Err(corrupted());
    }
    let version = members["version"].as_i64().ok_or_else(corrupted)?;
    let generation = members["generation"].as_str().ok_or_else(corrupted)?;
    let selected_at = members["selected_at"].as_str().ok_or_else(corrupted)?;
    // The generation is joined under `generations/`: a separator would leave the state root.
    if !generation.starts_with("generation-") || generation.contains(['/', '\\']) {
        return Err(corrupted());
    }
    Ok(Some(Pointer {
        version,
        generation: generation.to_owned(),
        selected_at: selected_at.to_owned(),
    }))
}

/// Every regular file below `dir`, sorted, with `/` paths relative to `dir`. A symbolic link or a
/// special file cannot be imported faithfully, so it refuses.
fn walk(dir: &Path) -> Result<(Vec<LegacyFile>, BTreeSet<String>)> {
    let mut files = Vec::new();
    let mut directories = BTreeSet::new();
    let mut pending: Vec<(PathBuf, String)> = vec![(dir.to_path_buf(), String::new())];
    while let Some((current, prefix)) = pending.pop() {
        let entries = fs::read_dir(&current).map_err(|err| Error::unreadable(&current, &err))?;
        for entry in entries {
            let entry = entry.map_err(|err| Error::unreadable(&current, &err))?;
            let name = entry.file_name().to_string_lossy().into_owned();
            let relative = if prefix.is_empty() {
                name.clone()
            } else {
                format!("{prefix}/{name}")
            };
            let path = entry.path();
            let kind = entry
                .file_type()
                .map_err(|err| Error::unreadable(&path, &err))?;
            if kind.is_dir() {
                directories.insert(relative.clone());
                pending.push((path, relative));
            } else if kind.is_file() {
                let bytes = fs::read(&path).map_err(|err| Error::unreadable(&path, &err))?;
                files.push(LegacyFile {
                    path: relative,
                    bytes,
                });
            } else {
                return Err(invalid(
                    format!("{} is neither a file nor a directory", path.display()),
                    "remove the link or special file from the state directory",
                ));
            }
        }
    }
    files.sort_by(|a, b| a.path.cmp(&b.path));
    Ok((files, directories))
}

impl LegacyState {
    /// Read the selected generation, the other generations and `lock-audit/` of `root`.
    pub fn read(root: &Path, pointer: Pointer) -> Result<Self> {
        let generations = root.join(GENERATIONS);
        let selected = generations.join(&pointer.generation);
        if !selected.is_dir() {
            return Err(invalid(
                "the selected ledger generation is missing",
                format!("restore the generation directory {}", selected.display()),
            ));
        }
        let (files, directories) = walk(&selected)?;
        let mut other_generations = Vec::new();
        let listing =
            fs::read_dir(&generations).map_err(|err| Error::unreadable(&generations, &err))?;
        let mut names = Vec::new();
        for entry in listing {
            let entry = entry.map_err(|err| Error::unreadable(&generations, &err))?;
            names.push(entry.file_name().to_string_lossy().into_owned());
        }
        names.sort();
        for name in names.into_iter().filter(|name| *name != pointer.generation) {
            let path = generations.join(&name);
            if !path.is_dir() {
                return Err(invalid(
                    format!("{} is not a generation directory", path.display()),
                    "remove the stray file from the generations directory",
                ));
            }
            let (found, _) = walk(&path)?;
            other_generations.extend(found.into_iter().map(|file| GenerationFile {
                generation: name.clone(),
                file,
            }));
        }
        let lock_audit_dir = root.join(LOCK_AUDIT);
        let lock_audit = if lock_audit_dir.is_dir() {
            walk(&lock_audit_dir)?
                .0
                .into_iter()
                .map(|file| LegacyFile {
                    path: format!("{LOCK_AUDIT}/{}", file.path),
                    bytes: file.bytes,
                })
                .collect()
        } else {
            Vec::new()
        };
        Ok(Self {
            pointer,
            files,
            directories,
            other_generations,
            lock_audit,
        })
    }

    /// The file of the selected generation at `path`.
    pub fn file(&self, path: &str) -> Option<&LegacyFile> {
        self.files
            .binary_search_by(|file| file.path.as_str().cmp(path))
            .ok()
            .map(|index| &self.files[index])
    }

    /// Every `audit/<name>.json` of every generation and every `lock-audit/<name>.json`, with
    /// paths relative to the state root, sorted by path.
    pub fn audit_files(&self) -> Vec<(String, &[u8])> {
        let direct_audit = |path: &str| {
            path.strip_prefix("audit/")
                .is_some_and(|name| !name.contains('/') && is_json(name))
        };
        let mut found: Vec<(String, &[u8])> = Vec::new();
        for file in self.files.iter().filter(|file| direct_audit(&file.path)) {
            found.push((
                format!("{GENERATIONS}/{}/{}", self.pointer.generation, file.path),
                &file.bytes,
            ));
        }
        for other in self
            .other_generations
            .iter()
            .filter(|other| direct_audit(&other.file.path))
        {
            found.push((
                format!("{GENERATIONS}/{}/{}", other.generation, other.file.path),
                &other.file.bytes,
            ));
        }
        for file in &self.lock_audit {
            found.push((file.path.clone(), &file.bytes));
        }
        found.sort_by(|a, b| a.0.cmp(&b.0));
        found
    }
}

/// The JSON object a record file holds, or `None` when it does not hold one.
pub fn parse_object(bytes: &[u8]) -> Option<Map<String, Value>> {
    match serde_json::from_slice(bytes) {
        Ok(Value::Object(members)) => Some(members),
        _ => None,
    }
}

/// The `record_sha256` check of an audit record: the hash of its canonical form without the field.
pub fn audit_record_is_valid(bytes: &[u8]) -> bool {
    let Some(mut record) = parse_object(bytes) else {
        return false;
    };
    let Some(checksum) = record.remove("record_sha256") else {
        return false;
    };
    checksum.as_str() == Some(object_sha256(&record).as_str())
}

fn is_json(path: &str) -> bool {
    Path::new(path)
        .extension()
        .is_some_and(|extension| extension == "json")
}

fn first_component(path: &str) -> &str {
    path.split('/').next().unwrap_or("")
}

/// Whether `path` names a file directly inside `directory` (`directory/<name>`).
pub(crate) fn is_direct(path: &str, directory: &str) -> bool {
    path.strip_prefix(directory)
        .and_then(|rest| rest.strip_prefix('/'))
        .is_some_and(|name| !name.contains('/'))
}

fn stem(path: &str) -> &str {
    let name = path.rsplit('/').next().unwrap_or(path);
    name.strip_suffix(".json").unwrap_or(name)
}

fn state_is_known(record: &Map<String, Value>, known: &[&str]) -> bool {
    record
        .get("state")
        .and_then(Value::as_str)
        .is_some_and(|state| known.contains(&state))
}

impl LegacyState {
    /// Repeat the checks of Python `ledger validate` for a version-3 generation: its directories
    /// and files, the checksums of its audit records and of `lock-audit/`, the immutable plan of
    /// every batch, the graph of batches, dispatches, statuses and reports, and the batch contract
    /// of `policy`. A failure refuses: the state is not changed and the operator fixes it first.
    pub fn validate(&self) -> Result<()> {
        for directory in RECORD_DIRECTORIES {
            if !self.directories.contains(directory) {
                return Err(invalid(
                    format!("the ledger generation is missing {directory}"),
                    format!(
                        "restore the {directory} directory of generation {}",
                        self.pointer.generation
                    ),
                ));
            }
        }
        let mut records: BTreeMap<&str, Map<String, Value>> = BTreeMap::new();
        for file in &self.files {
            let directory = first_component(&file.path);
            if !RECORD_DIRECTORIES.contains(&directory) {
                continue;
            }
            if !is_json(&file.path) {
                if matches!(directory, "qa-artifacts" | "reports") {
                    continue;
                }
                return Err(invalid(
                    format!("the ledger record has an unsupported file: {}", file.path),
                    format!("remove or rename {} to end in .json", file.path),
                ));
            }
            let record = parse_object(&file.bytes).ok_or_else(|| {
                invalid(
                    format!("the ledger record is not a JSON object: {}", file.path),
                    format!("rewrite {} as a JSON object", file.path),
                )
            })?;
            let parent = file.path.rsplit('/').nth(1).unwrap_or("");
            let known: &[&str] = match parent {
                "batches" => &BATCH_STATES,
                "dispatches" | "dispatch-status" => &DISPATCH_STATES,
                _ => &[],
            };
            if !known.is_empty() && !state_is_known(&record, known) {
                return Err(invalid(
                    format!("invalid state value in {}", file.path),
                    "use an exact English state from the vocabulary of the ledger",
                ));
            }
            if parent == "batches" {
                for entry in record
                    .get("dispatches")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                {
                    if let Some(state) = entry.get("state")
                        && state
                            .as_str()
                            .is_none_or(|text| !DISPATCH_STATES.contains(&text))
                    {
                        return Err(invalid(
                            format!("invalid dispatch state in {}", file.path),
                            "use an exact English state from the vocabulary of the ledger",
                        ));
                    }
                }
            }
            records.insert(&file.path, record);
        }
        for file in self
            .files
            .iter()
            .filter(|file| is_direct(&file.path, "audit") && is_json(&file.path))
        {
            if !audit_record_is_valid(&file.bytes) {
                return Err(invalid(
                    format!(
                        "the ledger audit record failed its integrity check: {}",
                        file.path
                    ),
                    "restore the audit record from a backup or the snapshot; it was modified after its record_sha256 was written",
                ));
            }
        }
        for file in &self.lock_audit {
            if !is_json(&file.path) || !is_direct(&file.path, LOCK_AUDIT) {
                return Err(invalid(
                    format!("the lock audit holds an unsupported entry: {}", file.path),
                    "remove or rename it; lock-audit holds only <audit-id>.json records",
                ));
            }
            if !audit_record_is_valid(&file.bytes) {
                return Err(invalid(
                    format!(
                        "the lock audit record failed its integrity check: {}",
                        file.path
                    ),
                    "restore the lock audit record from a backup or the snapshot",
                ));
            }
        }
        self.validate_plans(&records)?;
        self.validate_graph(&records)?;
        policy::validate_batches(&records)
    }

    fn validate_plans(&self, records: &BTreeMap<&str, Map<String, Value>>) -> Result<()> {
        for (path, batch) in records
            .iter()
            .filter(|(path, _)| is_direct(path, "batches"))
        {
            let plan_path = format!("plans/{}.json", stem(path));
            let Some(plan) = records.get(plan_path.as_str()) else {
                return Err(invalid(
                    format!("the batch has no immutable plan: {path}"),
                    format!("restore the missing plan record {plan_path}"),
                ));
            };
            if plan
                .iter()
                .any(|(key, value)| batch.get(key) != Some(value))
            {
                return Err(invalid(
                    format!("the batch does not match its immutable plan: {path}"),
                    format!("the batch record and its plan {plan_path} have diverged"),
                ));
            }
        }
        Ok(())
    }

    fn validate_graph(&self, records: &BTreeMap<&str, Map<String, Value>>) -> Result<()> {
        let direct = |directory: &str| -> BTreeSet<String> {
            records
                .keys()
                .filter(|path| is_direct(path, directory))
                .map(|path| stem(path).to_owned())
                .collect()
        };
        let dispatches = direct("dispatches");
        let statuses = direct("dispatch-status");
        let mut referenced = BTreeSet::new();
        for (path, batch) in records
            .iter()
            .filter(|(path, _)| is_direct(path, "batches"))
        {
            let batch_id = stem(path);
            let entries = match batch.get("dispatches") {
                None => &Vec::new(),
                Some(Value::Array(entries)) => entries,
                Some(_) => {
                    return Err(invalid(
                        format!("the batch dispatches are invalid: {path}"),
                        format!("fix {path} so its dispatches field is a list"),
                    ));
                }
            };
            for entry in entries {
                let Some(dispatch_id) = entry.get("dispatch_id").and_then(Value::as_str) else {
                    return Err(invalid(
                        format!("the batch has an invalid dispatch entry: {path}"),
                        format!("fix {path} so each dispatches entry has a string dispatch_id"),
                    ));
                };
                referenced.insert(dispatch_id.to_owned());
                let dispatch_path = format!("dispatches/{dispatch_id}.json");
                let status_path = format!("dispatch-status/{dispatch_id}.json");
                let (Some(dispatch), Some(status)) = (
                    records.get(dispatch_path.as_str()),
                    records.get(status_path.as_str()),
                ) else {
                    return Err(invalid(
                        format!("the batch dispatch evidence is incomplete: {dispatch_id}"),
                        format!("restore {dispatch_path} and {status_path}"),
                    ));
                };
                if dispatch.get("dispatch_id").and_then(Value::as_str) != Some(dispatch_id)
                    || dispatch.get("batch_id").and_then(Value::as_str) != Some(batch_id)
                {
                    return Err(invalid(
                        format!("the dispatch does not belong to its batch: {dispatch_id}"),
                        format!("fix the dispatch_id and batch_id fields of {dispatch_path}"),
                    ));
                }
                if status.get("dispatch_id").and_then(Value::as_str) != Some(dispatch_id) {
                    return Err(invalid(
                        format!("the dispatch status is invalid: {dispatch_id}"),
                        format!("fix the dispatch_id field of {status_path}"),
                    ));
                }
                let brief = object_sha256(dispatch);
                if entry.get("brief_sha256").and_then(Value::as_str) != Some(brief.as_str()) {
                    return Err(invalid(
                        format!(
                            "the dispatch failed its immutable brief integrity check: {dispatch_id}"
                        ),
                        format!("{dispatch_path} was modified after its brief_sha256 was recorded"),
                    ));
                }
                if entry.get("state").and_then(Value::as_str) == Some("reported") {
                    self.validate_report(records, path, dispatch_id, entry)?;
                }
            }
        }
        if dispatches != referenced || statuses != referenced {
            return Err(invalid(
                "the ledger state contains orphaned dispatch evidence",
                "run 'coordinator.py ledger clean' on installation 1.x to remove dispatch and status records no batch references",
            ));
        }
        Ok(())
    }

    fn validate_report(
        &self,
        records: &BTreeMap<&str, Map<String, Value>>,
        batch_path: &str,
        dispatch_id: &str,
        entry: &Value,
    ) -> Result<()> {
        let report_name = entry
            .get("report")
            .and_then(Value::as_str)
            .filter(|name| valid_path(name));
        let Some(report_name) = report_name else {
            return Err(invalid(
                format!("the reported dispatch has an invalid report path: {dispatch_id}"),
                format!("fix the report path of dispatch {dispatch_id} in {batch_path}"),
            ));
        };
        let Some(report) = records.get(report_name) else {
            return Err(invalid(
                format!("the completion report is missing or unreadable: {report_name}"),
                format!("restore {report_name}"),
            ));
        };
        let checksum = object_sha256(report);
        if entry.get("report_sha256").and_then(Value::as_str) != Some(checksum.as_str()) {
            return Err(invalid(
                format!("the completion report failed its integrity check: {dispatch_id}"),
                format!("{report_name} was modified after its report_sha256 was recorded"),
            ));
        }
        Ok(())
    }
}
