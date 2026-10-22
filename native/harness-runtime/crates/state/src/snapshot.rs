//! The snapshot of the state directory taken before the import (ADR 0020, section 5, step 3), and
//! the check, before the commit point, that the state did not change since.
//!
//! The snapshot holds a copy of the state directory in `state/`, `snapshot-manifest.json` with the
//! SHA-256 of every copied file, and (written by the migrator) `import-manifest.json`. Files and
//! directories are flushed to disk, so a crash cannot leave a snapshot that looks complete and is not.

use std::collections::BTreeMap;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use serde_json::{Value, json};

use crate::canonical::{canonical_json, sha256_hex};
use crate::error::{Error, Result};
use crate::platform;
use crate::time::iso_now;

pub const MANIFEST_NAME: &str = "snapshot-manifest.json";
pub const IMPORT_MANIFEST_NAME: &str = "import-manifest.json";
/// The copy of the state directory inside a snapshot.
pub const COPY_NAME: &str = "state";

/// Whether an entry of the state root belongs to neither the snapshot nor the change check: the
/// runtime directory of the IPC socket, the lock being held, and the stores.
pub fn is_excluded(name: &str) -> bool {
    matches!(
        name,
        "runtime" | ".coordinator.lock" | ".coordinator.lock.release"
    ) || name.starts_with("runtime.db")
}

/// The SHA-256 of every file below `state`, keyed by its `/` path relative to `state`.
fn hash_tree(state: &Path) -> io::Result<BTreeMap<String, (String, u64)>> {
    let mut found = BTreeMap::new();
    let mut pending = vec![(state.to_path_buf(), String::new())];
    while let Some((directory, prefix)) = pending.pop() {
        for entry in fs::read_dir(&directory)? {
            let entry = entry?;
            let name = entry.file_name().to_string_lossy().into_owned();
            if prefix.is_empty() && is_excluded(&name) {
                continue;
            }
            let relative = if prefix.is_empty() {
                name
            } else {
                format!("{prefix}/{name}")
            };
            let kind = entry.file_type()?;
            if kind.is_dir() {
                pending.push((entry.path(), relative));
            } else if kind.is_file() {
                let bytes = fs::read(entry.path())?;
                found.insert(relative, (sha256_hex(&bytes), bytes.len() as u64));
            } else {
                return Err(io::Error::other(format!(
                    "{} is neither a file nor a directory",
                    entry.path().display()
                )));
            }
        }
    }
    Ok(found)
}

/// A snapshot that was taken.
#[derive(Debug)]
pub struct Snapshot {
    pub dir: PathBuf,
    /// The SHA-256 of every file the snapshot holds, keyed by its path relative to the state root.
    pub files: BTreeMap<String, String>,
}

fn snapshot_name(at: &str) -> String {
    // `2026-10-22T07:10:19.972174+00:00` becomes `20261022T071019Z`.
    let digits: String = at
        .chars()
        .take(19)
        .filter(char::is_ascii_alphanumeric)
        .collect();
    format!("{digits}Z")
}

/// Copy `state` into a new directory below `snapshots`, with its manifest, and flush everything.
///
/// # Errors
/// `Error::Interrupted` when anything cannot be copied or flushed; the partial snapshot is removed.
pub fn create(snapshots: &Path, state: &Path) -> Result<Snapshot> {
    fs::create_dir_all(snapshots)?;
    let at = iso_now();
    let base = snapshot_name(&at);
    let mut dir = snapshots.join(&base);
    let mut attempt = 1;
    while dir.exists() {
        dir = snapshots.join(format!("{base}-{attempt}"));
        attempt += 1;
    }
    fs::create_dir(&dir)?;
    let result = copy_into(&dir, state, &at);
    match result {
        Ok(files) => {
            platform::sync_dir(snapshots)?;
            Ok(Snapshot { dir, files })
        }
        Err(err) => {
            let _ = fs::remove_dir_all(&dir);
            Err(err)
        }
    }
}

fn copy_into(dir: &Path, state: &Path, at: &str) -> Result<BTreeMap<String, String>> {
    let copy = dir.join(COPY_NAME);
    fs::create_dir(&copy)?;
    let mut files = BTreeMap::new();
    let mut manifest_entries = Vec::new();
    let mut directories = vec![copy.clone()];
    let mut pending = vec![(state.to_path_buf(), copy, String::new())];
    while let Some((source, target, prefix)) = pending.pop() {
        for entry in fs::read_dir(&source)? {
            let entry = entry?;
            let name = entry.file_name().to_string_lossy().into_owned();
            if prefix.is_empty() && is_excluded(&name) {
                continue;
            }
            let relative = if prefix.is_empty() {
                name.clone()
            } else {
                format!("{prefix}/{name}")
            };
            let kind = entry.file_type()?;
            let destination = target.join(&name);
            if kind.is_dir() {
                fs::create_dir(&destination)?;
                directories.push(destination.clone());
                pending.push((entry.path(), destination, relative));
            } else if kind.is_file() {
                let bytes = fs::read(entry.path())?;
                platform::write_durable(&destination, &bytes)?;
                let sha256 = sha256_hex(&bytes);
                manifest_entries.push(json!({
                    "path": relative,
                    "sha256": sha256,
                    "bytes": bytes.len(),
                }));
                files.insert(relative, sha256);
            } else {
                return Err(Error::Interrupted(format!(
                    "{} is neither a file nor a directory",
                    entry.path().display()
                )));
            }
        }
    }
    manifest_entries.sort_by(|a, b| a["path"].as_str().cmp(&b["path"].as_str()));
    let manifest = canonical_json(&json!({
        "created_at": at,
        "files": manifest_entries,
        "source": state.to_string_lossy(),
    }));
    platform::write_durable(&dir.join(MANIFEST_NAME), manifest.as_bytes())?;
    for directory in directories.iter().rev() {
        platform::sync_dir(directory)?;
    }
    platform::sync_dir(dir)?;
    Ok(files)
}

/// Write `import-manifest.json` into the snapshot.
pub fn write_import_manifest(snapshot: &Snapshot, text: &str) -> Result<()> {
    platform::write_durable(&snapshot.dir.join(IMPORT_MANIFEST_NAME), text.as_bytes())?;
    platform::sync_dir(&snapshot.dir)?;
    Ok(())
}

/// Compare the state directory with the snapshot manifest.
///
/// # Errors
/// `Error::Invariant` when a file was added, removed or changed since the snapshot: the legacy
/// ledger was written during the operation, so the operator must look.
pub fn verify_unchanged(state: &Path, snapshot: &Snapshot) -> Result<()> {
    let now = hash_tree(state)?;
    let mut changed = Vec::new();
    for (path, sha256) in &snapshot.files {
        match now.get(path) {
            None => changed.push(format!("removed: {path}")),
            Some((current, _)) if current != sha256 => changed.push(format!("changed: {path}")),
            Some(_) => {}
        }
    }
    for path in now
        .keys()
        .filter(|path| !snapshot.files.contains_key(*path))
    {
        changed.push(format!("added: {path}"));
    }
    if changed.is_empty() {
        return Ok(());
    }
    let total = changed.len();
    changed.truncate(10);
    Err(Error::invariant(format!(
        "the legacy state changed while the migration ran ({total} file(s)): {}",
        changed.join("; ")
    )))
}

/// The manifest of a snapshot, for the tests and for the operator.
pub fn read_manifest(dir: &Path) -> Result<Value> {
    let bytes = fs::read(dir.join(MANIFEST_NAME))?;
    serde_json::from_slice(&bytes).map_err(|err| Error::Interrupted(err.to_string()))
}
