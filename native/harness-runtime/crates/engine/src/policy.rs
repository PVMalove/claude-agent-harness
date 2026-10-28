//! The mount policy of ADR 0022, section 4: no plan root gives a container an engine socket or
//! a harness runtime.
//!
//! A root is denied when its real path equals a protected path, lies under it or is its
//! ancestor, whether the protected path exists or not: an engine socket may appear after the
//! check. A protected path is compared by the real path of its longest existing prefix plus the
//! rest, so `/var/run/docker.sock` protects `/run/docker.sock`. A root that does not resolve, a
//! root with a character that could inject `--mount` options and a root that overlaps the
//! container home are denied too. Each denial carries its own remedy: a root that does not exist
//! names itself, a protected path names the runtime directory. The caller builds the protected
//! list, so this crate does not depend on where the runtime keeps its files.

use std::io::ErrorKind;
use std::path::{Component, Path, PathBuf};

use crate::argv::CONTAINER_HOME;

/// What the protected list is built from: the runtime's own environment and paths.
#[derive(Clone, Debug, Default)]
pub struct Protected {
    pub uid: u32,
    /// `XDG_RUNTIME_DIR` of the runtime, when it is set and absolute.
    pub xdg_runtime_dir: Option<PathBuf>,
    /// `DOCKER_HOST` and `CONTAINER_HOST` of the runtime: only a `unix://` path is protected.
    pub docker_host: Option<String>,
    pub container_host: Option<String>,
    /// The runtime's own directory, socket, token and lock.
    pub runtime: Vec<PathBuf>,
}

/// One denied root, why, and what the operator does about it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Denial {
    pub root: String,
    pub reason: String,
    pub remedy: String,
}

/// The remedy of a root that exposes an engine socket or a harness runtime.
const PROTECTED_REMEDY: &str = "keep every engine socket and the harness runtime directory out of \
     the plan roots: narrow the root or set XDG_RUNTIME_DIR outside them and restart harness-runtime";

fn unix_path(host: Option<&str>) -> Option<PathBuf> {
    let path = host?.strip_prefix("unix://")?;
    Path::new(path).is_absolute().then(|| PathBuf::from(path))
}

/// The engine sockets of ADR 0022 and every known runtime directory of the uid, then the
/// runtime's own paths.
pub fn protected_paths(inputs: &Protected) -> Vec<PathBuf> {
    let uid = inputs.uid;
    let user_run = PathBuf::from(format!("/run/user/{uid}"));
    let mut paths: Vec<PathBuf> = [
        "/var/run/docker.sock",
        "/run/docker.sock",
        "/run/podman/podman.sock",
    ]
    .into_iter()
    .map(PathBuf::from)
    .collect();
    for base in inputs.xdg_runtime_dir.iter().chain([&user_run]) {
        paths.extend([
            base.join("podman"),
            base.join("docker.sock"),
            base.join("harness"),
        ]);
    }
    // Rootless Podman without XDG_RUNTIME_DIR keeps its socket here, and a harness runtime
    // without it its directory (ADR 0021, section 2).
    paths.push(PathBuf::from(format!("/tmp/podman-run-{uid}")));
    paths.push(PathBuf::from(format!("/tmp/harness-{uid}")));
    paths.extend(unix_path(inputs.docker_host.as_deref()));
    paths.extend(unix_path(inputs.container_host.as_deref()));
    paths.extend(inputs.runtime.iter().cloned());
    paths.sort();
    paths.dedup();
    paths
}

/// The real path of the longest existing prefix of `path`, plus the rest of it.
fn resolve_prefix(path: &Path) -> PathBuf {
    let mut rest = Vec::new();
    let mut prefix = path;
    loop {
        if let Ok(real) = prefix.canonicalize() {
            return rest.iter().rev().fold(real, |acc, part| acc.join(part));
        }
        match (prefix.parent(), prefix.file_name()) {
            (Some(parent), Some(name)) => {
                rest.push(name.to_owned());
                prefix = parent;
            }
            _ => return path.to_path_buf(),
        }
    }
}

fn unsafe_text(path: &str) -> bool {
    path.contains([',', '"', '\n', '\r'])
}

fn overlaps_home(path: &Path) -> bool {
    let home = Path::new(CONTAINER_HOME);
    path.starts_with(home) || home.starts_with(path)
}

fn related(root: &Path, protected: &Path) -> bool {
    root.starts_with(protected) || protected.starts_with(root)
}

fn normal(path: &Path) -> bool {
    path.is_absolute()
        && path
            .components()
            .all(|part| matches!(part, Component::RootDir | Component::Normal(_)))
}

fn denial(root: &str, reason: String, remedy: String) -> Option<Denial> {
    Some(Denial {
        root: root.to_owned(),
        reason,
        remedy,
    })
}

fn check(root: &str, protected: &[PathBuf]) -> Option<Denial> {
    let path = Path::new(root);
    let unsafe_remedy = || {
        format!(
            "move the root to a path without commas, quotes or line breaks outside {CONTAINER_HOME}"
        )
    };
    if unsafe_text(root) || overlaps_home(path) {
        return denial(
            root,
            format!(
                "plan root {root:?} cannot be mounted safely: it holds a comma, a quote or a line \
                 break, or overlaps {CONTAINER_HOME}"
            ),
            unsafe_remedy(),
        );
    }
    let real = match path.canonicalize() {
        Ok(real) => real,
        Err(err) if err.kind() == ErrorKind::NotFound => {
            return denial(
                root,
                format!("plan root {root:?} does not exist"),
                format!(
                    "create {root}, or run `harness install` in the repository when it is a \
                     harness directory, and repeat the command"
                ),
            );
        }
        Err(err) => {
            return denial(
                root,
                format!("plan root {root:?} does not resolve to a real path: {err}"),
                format!(
                    "make {root} readable by the user of harness-runtime and repeat the command"
                ),
            );
        }
    };
    let real_text = real.to_string_lossy();
    if unsafe_text(&real_text) || overlaps_home(&real) {
        return denial(
            root,
            format!("plan root {root:?} resolves to {real_text:?}, which cannot be mounted safely"),
            unsafe_remedy(),
        );
    }
    let item = protected
        .iter()
        .filter(|item| normal(item))
        .map(|item| resolve_prefix(item))
        .find(|item| related(&real, item))?;
    denial(
        root,
        format!(
            "plan root {root:?} would give the worker {}: an engine socket or a harness runtime \
             is never mounted",
            item.display()
        ),
        PROTECTED_REMEDY.to_owned(),
    )
}

/// Deny every root that would expose a protected path, does not resolve or is unsafe to mount.
///
/// # Errors
/// Every denied root, with its reason and remedy.
pub fn deny_mounts(roots: &[String], protected: &[PathBuf]) -> Result<(), Vec<Denial>> {
    let denials: Vec<Denial> = roots
        .iter()
        .filter_map(|root| check(root, protected))
        .collect();
    if denials.is_empty() {
        Ok(())
    } else {
        Err(denials)
    }
}
