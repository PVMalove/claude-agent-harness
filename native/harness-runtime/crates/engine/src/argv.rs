//! The exact engine argv of ADR 0025: a one-to-one port of `identity_argv`, `create_argv`,
//! `proxy_argv`, `network_argv` and `mounts`, plus the operation and execution labels.

use std::path::Path;

use crate::Kind;
use crate::facts::Mode;
use crate::plan::{
    HostUser, LABEL_DISPATCH, LABEL_EXECUTION, LABEL_OPERATION, LABEL_PLAN_DIGEST,
    LABEL_REPOSITORY, Labels, Plan, Requirement,
};

/// The worker's HOME is a tmpfs, so no host home directory is mounted and any image works.
pub const CONTAINER_HOME: &str = "/harness-home";
/// The worker environment the executor sets itself (ADR 0025).
pub const FIXED_ENV: [(&str, &str); 5] = [
    ("HOME", CONTAINER_HOME),
    // Docker creates the parents of nested mount points as root; Git must still trust them.
    ("GIT_CONFIG_COUNT", "1"),
    ("GIT_CONFIG_KEY_0", "safe.directory"),
    ("GIT_CONFIG_VALUE_0", "*"),
    ("HARNESS_EXECUTOR", "docker"),
];
/// The egress sidecar listens here; its allowlist reaches it in this variable.
pub const PROXY_PORT: u16 = 3128;
pub const ALLOWED_HOSTS_ENV: &str = "HARNESS_EGRESS_ALLOWED_HOSTS";
pub const PROXY_URL_ENV_NAMES: [&str; 4] =
    ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"];
pub const PROXY_BYPASS_ENV_NAMES: [&str; 2] = ["NO_PROXY", "no_proxy"];
/// Loopback stays inside the worker's own container, so it never needs the proxy.
pub const PROXY_BYPASS: &str = "localhost,127.0.0.1,::1";

/// What a launch needs besides the plan: the labels, the adapter command, the pinned image ids,
/// the host user and the engine mode that `observe` found.
#[derive(Clone, Copy, Debug)]
pub struct Spec<'a> {
    pub plan: &'a Plan,
    pub labels: &'a Labels,
    pub command: &'a [String],
    pub worker_image_id: &'a str,
    pub proxy_image_id: Option<&'a str>,
    pub user: HostUser,
    pub mode: Mode,
    pub selinux: bool,
}

/// The name of a dispatch's worker container.
pub fn container_name(dispatch_id: &str) -> String {
    format!("harness-{dispatch_id}")
}

/// The name of a dispatch's egress-proxy sidecar, also its DNS name on the internal network.
pub fn proxy_name(dispatch_id: &str) -> String {
    format!("{}-proxy", container_name(dispatch_id))
}

/// The name of a dispatch's internal network.
pub fn network_name(dispatch_id: &str) -> String {
    format!("{}-net", container_name(dispatch_id))
}

fn owned(parts: &[&str]) -> Vec<String> {
    parts.iter().map(|part| (*part).to_owned()).collect()
}

/// The `--label` options of every object an execution creates.
pub fn labels_argv(labels: &Labels) -> Vec<String> {
    [
        (LABEL_DISPATCH, &labels.dispatch),
        (LABEL_REPOSITORY, &labels.repository),
        (LABEL_PLAN_DIGEST, &labels.plan_digest),
        (LABEL_OPERATION, &labels.operation),
        (LABEL_EXECUTION, &labels.execution),
    ]
    .into_iter()
    .flat_map(|(key, value)| ["--label".to_owned(), format!("{key}={value}")])
    .collect()
}

/// One bind mount per plan root, read-write when any requirement writes it; parents come before
/// the roots nested in them, so a nested mount is never hidden.
pub fn mounts(requirements: &[Requirement]) -> Vec<(String, bool)> {
    let mut access: Vec<(String, bool)> = Vec::new();
    for item in requirements {
        match access.iter_mut().find(|(path, _)| *path == item.path) {
            Some((_, write)) => *write |= item.write,
            None => access.push((item.path.clone(), item.write)),
        }
    }
    access.sort_by(|(a, _), (b, _)| {
        (Path::new(a).components().count(), a).cmp(&(Path::new(b).components().count(), b))
    });
    access
}

/// The options that keep host file ownership for the engine mode, then SELinux.
///
/// A rootful engine runs the host `uid:gid`. Rootless Podman keeps the host id with
/// `--userns=keep-id`. Rootless Docker maps container root to the host user, so the worker pins
/// `--user 0:0`; the sidecar keeps its own image user. SELinux gets `label=disable`.
pub fn identity_argv(
    engine: Kind,
    mode: Mode,
    selinux: bool,
    user: HostUser,
    worker: bool,
) -> Vec<String> {
    let mut argv = if mode != Mode::Rootless {
        vec!["--user".to_owned(), format!("{}:{}", user.uid, user.gid)]
    } else if engine == Kind::Podman {
        owned(&["--userns=keep-id"])
    } else if worker {
        owned(&["--user", "0:0"])
    } else {
        Vec::new()
    };
    if selinux {
        argv.extend(owned(&["--security-opt", "label=disable"]));
    }
    argv
}

/// The exact `<engine> create` of the approved worker: plan roots only, no direct network, and
/// every plan environment name as `--env NAME` without a value.
pub fn create_argv(spec: &Spec<'_>) -> Vec<String> {
    let plan = spec.plan;
    let dispatch = &spec.labels.dispatch;
    let hosts = !plan.hosts.is_empty();
    let engine = plan.engine;
    let mut argv = owned(&[engine.as_str(), "create", "--pull", "never", "--name"]);
    argv.push(container_name(dispatch));
    argv.push("--network".to_owned());
    argv.push(if hosts {
        network_name(dispatch)
    } else {
        "none".to_owned()
    });
    argv.extend(identity_argv(
        engine,
        spec.mode,
        spec.selinux,
        spec.user,
        true,
    ));
    argv.extend(owned(&[
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
    ]));
    argv.push(format!("{CONTAINER_HOME}:rw,exec,nosuid,nodev,mode=1777"));
    for (name, value) in FIXED_ENV {
        argv.extend(["--env".to_owned(), format!("{name}={value}")]);
    }
    if hosts {
        let url = format!("http://{}:{PROXY_PORT}", proxy_name(dispatch));
        for name in PROXY_URL_ENV_NAMES {
            argv.extend(["--env".to_owned(), format!("{name}={url}")]);
        }
        for name in PROXY_BYPASS_ENV_NAMES {
            argv.extend(["--env".to_owned(), format!("{name}={PROXY_BYPASS}")]);
        }
    }
    for name in &plan.environment {
        argv.extend(["--env".to_owned(), name.clone()]);
    }
    argv.extend(labels_argv(spec.labels));
    argv.push("--workdir".to_owned());
    argv.push(plan.root("checkout").unwrap_or_default().to_owned());
    for (path, write) in mounts(&plan.requirements) {
        argv.push("--mount".to_owned());
        argv.push(format!(
            "type=bind,source={path},target={path}{}",
            if write { "" } else { ",readonly" }
        ));
    }
    // The image is the pinned id, never the tag: a retag after approval cannot change it.
    let (program, args) = spec
        .command
        .split_first()
        .map_or(("", &[][..]), |(program, args)| (program.as_str(), args));
    argv.extend(["--entrypoint".to_owned(), program.to_owned()]);
    argv.push(spec.worker_image_id.to_owned());
    argv.extend(args.iter().cloned());
    argv
}

/// The `<engine> network create` of the dispatch's internal network: no route out of it.
pub fn network_argv(spec: &Spec<'_>) -> Vec<String> {
    let mut argv = owned(&[spec.plan.engine.as_str(), "network", "create", "--internal"]);
    argv.extend(labels_argv(spec.labels));
    argv.push(network_name(&spec.labels.dispatch));
    argv
}

/// The `<engine> create` of the egress sidecar from its pinned id, on the internal network.
pub fn proxy_argv(spec: &Spec<'_>) -> Vec<String> {
    let engine = spec.plan.engine;
    let dispatch = &spec.labels.dispatch;
    let mut argv = owned(&[engine.as_str(), "create", "--pull", "never", "--name"]);
    argv.push(proxy_name(dispatch));
    argv.push("--network".to_owned());
    argv.push(network_name(dispatch));
    argv.extend(identity_argv(
        engine,
        spec.mode,
        spec.selinux,
        spec.user,
        false,
    ));
    argv.extend(owned(&[
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--env",
    ]));
    argv.push(format!("{ALLOWED_HOSTS_ENV}={}", spec.plan.hosts.join(",")));
    argv.extend(labels_argv(spec.labels));
    argv.push(spec.proxy_image_id.unwrap_or_default().to_owned());
    argv
}
