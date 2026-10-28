#![cfg(unix)]

//! The mount policy of ADR 0022, section 4: a plan root is decided by its real path against
//! every protected path, whether that path exists or not.

use std::fs;
use std::os::unix::fs::symlink;
use std::path::{Path, PathBuf};

use engine::policy::{self, Protected};

struct Scratch(PathBuf);

impl Scratch {
    fn new(name: &str) -> Self {
        let dir = std::env::temp_dir().join(format!("engine-policy-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).expect("scratch");
        Self(dir.canonicalize().expect("canonical scratch"))
    }

    fn dir(&self, name: &str) -> PathBuf {
        let path = self.0.join(name);
        fs::create_dir_all(&path).expect("dir");
        path
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn text(path: &Path) -> String {
    path.to_str().expect("utf-8").to_owned()
}

fn denied(roots: &[String], protected: &[PathBuf]) -> Vec<String> {
    match policy::deny_mounts(roots, protected) {
        Ok(()) => Vec::new(),
        Err(denials) => denials.into_iter().map(|denial| denial.root).collect(),
    }
}

#[test]
fn a_root_equal_to_under_or_above_a_protected_path_is_denied() {
    let scratch = Scratch::new("relations");
    let run = scratch.dir("run");
    let socket = run.join("docker.sock");
    fs::write(&socket, "").expect("socket stand-in");
    let protected = vec![socket.clone(), scratch.0.join("runtime/harness")];

    assert_eq!(denied(&[text(&socket)], &protected), [text(&socket)]);
    assert_eq!(
        denied(&[text(&run)], &protected),
        [text(&run)],
        "the root contains the socket"
    );
    // The runtime directory does not exist yet: its ancestor is still denied.
    let runtime = scratch.dir("runtime");
    assert_eq!(denied(&[text(&runtime)], &protected), [text(&runtime)]);
    let under = scratch.dir("runtime/harness/cache");
    assert_eq!(denied(&[text(&under)], &protected), [text(&under)]);
    let unrelated = scratch.dir("work");
    assert_eq!(
        denied(&[text(&unrelated)], &protected),
        Vec::<String>::new()
    );
}

#[test]
fn a_symlink_to_a_protected_path_is_denied_by_its_real_path() {
    let scratch = Scratch::new("symlink");
    let runtime = scratch.dir("runtime");
    let link = scratch.0.join("innocent");
    symlink(&runtime, &link).expect("symlink");

    assert_eq!(
        denied(&[text(&link)], &[runtime.join("x.sock")]),
        [text(&link)]
    );
}

#[test]
fn a_protected_path_behind_a_symlinked_parent_is_compared_by_its_real_prefix() {
    // Like /var/run -> /run: the protected /var/run/docker.sock protects /run itself.
    let scratch = Scratch::new("prefix");
    let real = scratch.dir("run");
    symlink(&real, scratch.0.join("var-run")).expect("symlink");

    let protected = [scratch.0.join("var-run/docker.sock")];

    assert_eq!(denied(&[text(&real)], &protected), [text(&real)]);
}

#[test]
fn an_unresolvable_or_unsafe_root_and_the_container_home_are_denied() {
    let scratch = Scratch::new("unsafe");
    let missing = text(&scratch.0.join("missing"));
    let comma = scratch.dir("a,b");

    assert_eq!(denied(std::slice::from_ref(&missing), &[]), [missing]);
    assert_eq!(denied(&[text(&comma)], &[]), [text(&comma)]);
    for root in ["/harness-home", "/harness-home/x"] {
        assert_eq!(denied(&[root.to_owned()], &[]), [root]);
    }
}

#[test]
fn the_protected_list_names_every_engine_socket_and_runtime_directory() {
    let paths = policy::protected_paths(&Protected {
        uid: 1000,
        xdg_runtime_dir: Some("/run/user/1000".into()),
        docker_host: Some("unix:///srv/docker/docker.sock".to_owned()),
        container_host: Some("tcp://example:2375".to_owned()),
        runtime: vec![PathBuf::from("/srv/runtime/repo.sock")],
    });

    for expected in [
        "/var/run/docker.sock",
        "/run/docker.sock",
        "/run/podman/podman.sock",
        "/run/user/1000/podman",
        "/run/user/1000/docker.sock",
        "/run/user/1000/harness",
        "/tmp/podman-run-1000",
        "/srv/docker/docker.sock",
        "/srv/runtime/repo.sock",
    ] {
        assert!(
            paths.contains(&PathBuf::from(expected)),
            "{expected} in {paths:?}"
        );
    }
    assert!(
        !paths
            .iter()
            .any(|path| path.to_string_lossy().contains("example"))
    );
}
