//! The mount policy of ADR 0022, section 4: no plan root gives a container an engine socket or
//! a harness runtime.
//!
//! A root is denied when its real path equals a protected path, lies under it or is its
//! ancestor, whether the protected path exists or not: an engine socket may appear after the
//! check. A protected path is compared by the real path of its longest existing prefix plus the
//! rest, so `/var/run/docker.sock` protects `/run/docker.sock`. A root that does not resolve, a
//! root with a character that could inject `--mount` options and a root that overlaps the
//! container home are denied too. The caller builds the protected list, so this crate does not
//! depend on where the runtime keeps its files.

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

/// One denied root and why.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Denial {
    pub root: String,
    pub reason: String,
}

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
    // Rootless Podman without XDG_RUNTIME_DIR keeps its socket here.
    paths.push(PathBuf::from(format!("/tmp/podman-run-{uid}")));
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

fn check(root: &str, protected: &[PathBuf]) -> Option<String> {
    let path = Path::new(root);
    if unsafe_text(root) || overlaps_home(path) {
        return Some(format!(
            "plan root {root:?} cannot be mounted safely: move it to a path without commas, \
             quotes or line breaks outside {CONTAINER_HOME}"
        ));
    }
    let Ok(real) = path.canonicalize() else {
        return Some(format!(
            "plan root {root:?} does not resolve to a real path"
        ));
    };
    let real_text = real.to_string_lossy();
    if unsafe_text(&real_text) || overlaps_home(&real) {
        return Some(format!(
            "plan root {root:?} resolves to {real_text:?}, which cannot be mounted safely"
        ));
    }
    protected
        .iter()
        .filter(|item| normal(item))
        .map(|item| resolve_prefix(item))
        .find(|item| related(&real, item))
        .map(|item| {
            format!(
                "plan root {root:?} would give the worker {}: an engine socket or a harness \
                 runtime is never mounted",
                item.display()
            )
        })
}

/// Deny every root that would expose a protected path, does not resolve or is unsafe to mount.
///
/// # Errors
/// Every denied root, with its reason.
pub fn deny_mounts(roots: &[String], protected: &[PathBuf]) -> Result<(), Vec<Denial>> {
    let denials: Vec<Denial> = roots
        .iter()
        .filter_map(|root| {
            check(root, protected).map(|reason| Denial {
                root: root.clone(),
                reason,
            })
        })
        .collect();
    if denials.is_empty() {
        Ok(())
    } else {
        Err(denials)
    }
}
