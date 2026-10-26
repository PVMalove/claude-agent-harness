//! A scratch copy of the legacy state fixture written by the Python 1.x ledger.

#![allow(dead_code)]

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};

use state::legacy::{self, LegacyState};

pub const FIXTURE: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/legacy_v3");

fn copy_tree(from: &Path, to: &Path) {
    fs::create_dir_all(to).unwrap();
    for entry in fs::read_dir(from).unwrap() {
        let entry = entry.unwrap();
        let target = to.join(entry.file_name());
        if entry.file_type().unwrap().is_dir() {
            copy_tree(&entry.path(), &target);
        } else {
            fs::copy(entry.path(), target).unwrap();
        }
    }
}

/// A fresh directory that holds a copy of the fixture state root as `state/`.
pub struct Scratch {
    pub base: PathBuf,
}

impl Scratch {
    pub fn new(name: &str) -> Self {
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        let n = NEXT.fetch_add(1, Ordering::Relaxed);
        let base = std::env::temp_dir().join(format!("hr-state-{name}-{}-{n}", std::process::id()));
        let _ = fs::remove_dir_all(&base);
        copy_tree(Path::new(FIXTURE), &base.join("state"));
        Self { base }
    }

    pub fn state(&self) -> PathBuf {
        self.base.join("state")
    }

    pub fn db(&self) -> PathBuf {
        self.base.join("runtime.db.importing")
    }

    pub fn generation(&self) -> PathBuf {
        let pointer = legacy::read_pointer(&self.state()).unwrap().unwrap();
        self.state().join("generations").join(pointer.generation)
    }

    pub fn read(&self) -> LegacyState {
        let pointer = legacy::read_pointer(&self.state()).unwrap().unwrap();
        LegacyState::read(&self.state(), pointer).unwrap()
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.base);
    }
}

/// A repository root whose `.harness/orchestration/state` is the fixture, with no QA lease and no
/// queued QA run, which is the state `migrate` accepts.
pub struct Repo {
    pub root: PathBuf,
}

impl Repo {
    pub fn new(name: &str) -> Self {
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        let n = NEXT.fetch_add(1, Ordering::Relaxed);
        let root = std::env::temp_dir().join(format!("hr-repo-{name}-{}-{n}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        copy_tree(
            Path::new(FIXTURE),
            &root.join(".harness/orchestration/state"),
        );
        let repo = Self { root };
        let generation = repo.generation();
        fs::remove_file(generation.join("qa-lane/lease.json")).unwrap();
        fs::remove_file(generation.join("qa-lane/queue/00000000000000000001-dispatch.json"))
            .unwrap();
        repo
    }

    pub fn state(&self) -> PathBuf {
        self.root.join(".harness/orchestration/state")
    }

    pub fn snapshots(&self) -> PathBuf {
        self.root.join(".harness/orchestration/state-snapshots")
    }

    pub fn generation(&self) -> PathBuf {
        let pointer = legacy::read_pointer(&self.state()).unwrap().unwrap();
        self.state().join("generations").join(pointer.generation)
    }

    /// Every file of the state directory and its bytes, keyed by relative path, without the
    /// files `migrate` is allowed to create next to the legacy records.
    pub fn tree(&self) -> std::collections::BTreeMap<String, Vec<u8>> {
        let mut found = std::collections::BTreeMap::new();
        let mut pending = vec![(self.state(), String::new())];
        while let Some((dir, prefix)) = pending.pop() {
            for entry in fs::read_dir(&dir).unwrap() {
                let entry = entry.unwrap();
                let name = entry.file_name().to_string_lossy().into_owned();
                let relative = if prefix.is_empty() {
                    name
                } else {
                    format!("{prefix}/{name}")
                };
                if entry.file_type().unwrap().is_dir() {
                    pending.push((entry.path(), relative));
                } else {
                    found.insert(relative, fs::read(entry.path()).unwrap());
                }
            }
        }
        found
    }
}

impl Drop for Repo {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}
