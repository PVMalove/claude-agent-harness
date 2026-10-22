//! Where each file of the selected generation goes in the store, and the path a row projects to.
//!
//! `import` and `parity` both ask `classify`, so a file cannot be imported to one place and
//! compared at another.

use serde_json::{Map, Value};

use crate::legacy::parse_object;
use crate::project::{Spec, spec_of};

/// The place of one file of the selected generation.
#[derive(Debug)]
pub enum Target {
    /// A record of a typed table (`batch`, `dispatch`, ...); `id` is the file name without `.json`.
    Typed {
        spec: &'static Spec,
        id: String,
        record: Map<String, Value>,
    },
    /// `qa-lane/lease.json`: the singleton lease.
    QaLease(Map<String, Value>),
    /// `qa-lane/queue/<name>.json`.
    QaQueue {
        id: String,
        record: Map<String, Value>,
    },
    /// `audit/<name>.json`: imported byte for byte into `legacy_audit`.
    Audit,
    /// Any other JSON object.
    Record {
        collection: String,
        record: Map<String, Value>,
    },
    /// Everything else, byte for byte.
    Artifact,
}

fn file_stem(name: &str) -> Option<&str> {
    name.strip_suffix(".json").filter(|stem| !stem.is_empty())
}

/// The target of the file at `path` (relative to the generation) holding `bytes`.
pub fn classify(path: &str, bytes: &[u8]) -> Target {
    let parts: Vec<&str> = path.split('/').collect();
    if let [directory, name] = parts[..] {
        if directory == "audit" && file_stem(name).is_some() {
            return Target::Audit;
        }
        if let (Some(spec), Some(id)) = (spec_of(directory), file_stem(name))
            && let Some(record) = parse_object(bytes)
        {
            return Target::Typed {
                spec,
                id: id.to_owned(),
                record,
            };
        }
    }
    if let ["qa-lane", "lease.json"] = parts[..]
        && let Some(record) = parse_object(bytes)
    {
        return Target::QaLease(record);
    }
    if let ["qa-lane", "queue", name] = parts[..]
        && let (Some(id), Some(record)) = (file_stem(name), parse_object(bytes))
    {
        return Target::QaQueue {
            id: id.to_owned(),
            record,
        };
    }
    if path.ends_with(".json")
        && let Some(record) = parse_object(bytes)
    {
        let collection = if parts.len() > 1 { parts[0] } else { "" };
        return Target::Record {
            collection: collection.to_owned(),
            record,
        };
    }
    Target::Artifact
}
