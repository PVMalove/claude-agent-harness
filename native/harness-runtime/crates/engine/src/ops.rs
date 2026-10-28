//! The engine sequences of the runtime over `&dyn Engine`: observe, launch and its rollback,
//! inspect, objects, remove, stop, kill and logs. None of them lists or removes a volume.

use std::fs;
use std::io;
use std::path::Path;
use std::time::Duration;

use serde_json::{Map, Value};

use crate::argv::{self, Spec};
use crate::facts::{self, Mode};
use crate::plan::{LABEL_DISPATCH, LABEL_EXECUTION, LABEL_REPOSITORY, Plan};
use crate::{ENGINE_TIMEOUT, Engine, Kind};

fn strings(parts: &[&str]) -> Vec<String> {
    parts.iter().map(|part| (*part).to_owned()).collect()
}

fn ask(engine: &dyn Engine, argv: &[String]) -> (bool, String, bool) {
    let answer = engine.run(argv, ENGINE_TIMEOUT);
    (answer.ok(), answer.output(), answer.answered)
}

/// Why `observe` refuses a plan.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Refusal {
    /// The engine did not answer `info`.
    Unavailable,
    /// The engine runs in a mode the executor does not support, or is another engine.
    Unsupported,
    /// A plan image is not present in the engine.
    MissingImage,
}

/// One reason `observe` found, with its remedy.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Blocker {
    pub refusal: Refusal,
    pub reason: String,
    pub remedy: String,
}

/// What the engine says about a plan, read-only.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Observation {
    pub engine: Kind,
    pub mode: Mode,
    pub selinux: bool,
    pub environment_id: String,
    pub worker_image_id: Option<String>,
    pub proxy_image_id: Option<String>,
    pub blockers: Vec<Blocker>,
}

fn image(engine: &dyn Engine, tag: &str, blockers: &mut Vec<Blocker>) -> Option<String> {
    let name = engine.kind().as_str();
    let (ok, output, _) = ask(
        engine,
        &strings(&[name, "image", "inspect", "--format", "{{.Id}}", tag]),
    );
    let id = if ok { facts::image_id(&output) } else { None };
    if id.is_none() {
        blockers.push(Blocker {
            refusal: Refusal::MissingImage,
            reason: format!(
                "image {tag} is not present in the {name} engine; the harness never builds or \
                 pulls images"
            ),
            remedy: format!(
                "build or pull {tag} yourself, then repeat dispatch preflight and propose"
            ),
        });
    }
    id
}

/// The engine mode, SELinux flag, identity and image ids the plan names; it only reads state.
pub fn observe(engine: &dyn Engine, plan: &Plan) -> Observation {
    let name = engine.kind().as_str();
    let mut blockers = Vec::new();
    let mut observation = Observation {
        engine: engine.kind(),
        mode: Mode::Unavailable,
        selinux: false,
        environment_id: String::new(),
        worker_image_id: None,
        proxy_image_id: None,
        blockers: Vec::new(),
    };
    let (ok, output, _) = ask(engine, &strings(&[name, "info", "--format", "{{json .}}"]));
    if !ok {
        blockers.push(Blocker {
            refusal: Refusal::Unavailable,
            reason: format!("the {name} engine is unavailable: {output}"),
            remedy: format!(
                "start the {name} engine and let harness-runtime reach the {name} CLI, then \
                 repeat dispatch preflight"
            ),
        });
        observation.blockers = blockers;
        return observation;
    }
    let info = serde_json::from_str::<Value>(&output).unwrap_or(Value::Null);
    let found = match engine.kind() {
        Kind::Docker => facts::docker_facts(&info),
        Kind::Podman => facts::podman_facts(&info),
    };
    match found {
        None => {
            observation.mode = Mode::Unknown;
            blockers.push(Blocker {
                refusal: Refusal::Unsupported,
                reason: format!("the {name} engine mode cannot be determined from {name} info"),
                remedy: format!(
                    "use a matching engine: the {name} CLI must answer as {name}; a \
                     docker-compatible shim or another engine is not supported"
                ),
            });
        }
        Some(found) => {
            observation.mode = found.mode;
            observation.selinux = found.selinux;
            observation.environment_id = found.environment_id;
            let unsupported = match found.mode {
                Mode::UsernsRemap => Some((
                    "Docker user namespace remapping is not supported by the container executor",
                    "run harness-runtime against a Docker daemon without userns-remap, or \
                     against rootless Docker or Podman",
                )),
                Mode::Remote => Some((
                    "remote Podman is not supported by the container executor",
                    "use a local Podman: the plan's mounts are paths of the runtime's own host",
                )),
                _ => None,
            };
            if let Some((reason, remedy)) = unsupported {
                blockers.push(Blocker {
                    refusal: Refusal::Unsupported,
                    reason: reason.to_owned(),
                    remedy: remedy.to_owned(),
                });
            }
        }
    }
    observation.worker_image_id = image(engine, &plan.worker_image, &mut blockers);
    observation.proxy_image_id = plan
        .proxy_image
        .as_deref()
        .and_then(|tag| image(engine, tag, &mut blockers));
    observation.blockers = blockers;
    observation
}

/// The kind of an engine object.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum ObjectKind {
    Container,
    Network,
}

impl ObjectKind {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Container => "container",
            Self::Network => "network",
        }
    }

    pub fn parse(text: &str) -> Option<Self> {
        [Self::Container, Self::Network]
            .into_iter()
            .find(|kind| kind.as_str() == text)
    }
}

/// The objects a launch creates, by name, in creation order: network, sidecar, worker.
pub fn planned_objects(plan: &Plan, dispatch_id: &str) -> Vec<(ObjectKind, String)> {
    let mut objects = Vec::new();
    if !plan.hosts.is_empty() {
        objects.push((ObjectKind::Network, argv::network_name(dispatch_id)));
        objects.push((ObjectKind::Container, argv::proxy_name(dispatch_id)));
    }
    objects.push((ObjectKind::Container, argv::container_name(dispatch_id)));
    objects
}

/// The ids of a started launch.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Launched {
    pub worker_id: String,
    pub proxy_id: Option<String>,
}

/// A failed launch: the stage, the engine's answer, the objects attempted and what their
/// removal, newest first, did.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct LaunchFailure {
    pub stage: String,
    pub detail: String,
    pub objects: Vec<(ObjectKind, String)>,
    pub removals: Vec<String>,
}

/// Remove `objects` by name, newest first; best effort, each result is evidence only.
pub fn rollback(engine: &dyn Engine, objects: &[(ObjectKind, String)]) -> Vec<String> {
    let name = engine.kind().as_str();
    objects
        .iter()
        .rev()
        .map(|(kind, object)| {
            let argv = match kind {
                ObjectKind::Container => strings(&[name, "rm", "--force", object]),
                ObjectKind::Network => strings(&[name, "network", "rm", object]),
            };
            let (ok, detail, _) = ask(engine, &argv);
            if ok {
                format!("{} {object} was removed", kind.as_str())
            } else {
                format!("removing {} {object} failed: {detail}", kind.as_str())
            }
        })
        .collect()
}

struct Staged<'a> {
    engine: &'a dyn Engine,
    objects: Vec<(ObjectKind, String)>,
}

impl Staged<'_> {
    fn run(
        &mut self,
        stage: &str,
        argv: &[String],
        creates: Option<(ObjectKind, String)>,
        returns_id: bool,
    ) -> Result<String, LaunchFailure> {
        // An object is recorded before its command runs: a failed command may still leave it.
        self.objects.extend(creates);
        let (ok, output, _) = ask(self.engine, argv);
        let detail = if !ok {
            Some(format!("{stage} failed: {output}"))
        } else if returns_id && !facts::is_object_id(&output) {
            Some(format!("{stage} returned no container id: {output:?}"))
        } else {
            None
        };
        match detail {
            None => Ok(output),
            Some(detail) => Err(LaunchFailure {
                stage: stage.to_owned(),
                detail,
                removals: rollback(self.engine, &self.objects),
                objects: self.objects.clone(),
            }),
        }
    }
}

/// Create and start the dispatch's objects detached; any failure removes what it created.
///
/// The sidecar comes first and only when the plan has hosts: its internal network, the sidecar
/// on it, the default network, its start. The worker is last.
///
/// # Errors
/// The first failed stage, after the rollback.
pub fn launch(engine: &dyn Engine, spec: &Spec<'_>) -> Result<Launched, LaunchFailure> {
    let name = engine.kind().as_str();
    let dispatch = &spec.labels.dispatch;
    let mut staged = Staged {
        engine,
        objects: Vec::new(),
    };
    let mut proxy_id = None;
    if !spec.plan.hosts.is_empty() {
        staged.run(
            &format!("{name} network create"),
            &argv::network_argv(spec),
            Some((ObjectKind::Network, argv::network_name(dispatch))),
            false,
        )?;
        let id = staged.run(
            &format!("{name} create (egress proxy)"),
            &argv::proxy_argv(spec),
            Some((ObjectKind::Container, argv::proxy_name(dispatch))),
            true,
        )?;
        staged.run(
            &format!("{name} network connect"),
            &strings(&[
                name,
                "network",
                "connect",
                engine.kind().default_network(),
                &id,
            ]),
            None,
            false,
        )?;
        staged.run(
            &format!("{name} start (egress proxy)"),
            &strings(&[name, "start", &id]),
            None,
            false,
        )?;
        proxy_id = Some(id);
    }
    let worker_id = staged.run(
        &format!("{name} create"),
        &argv::create_argv(spec),
        Some((ObjectKind::Container, argv::container_name(dispatch))),
        true,
    )?;
    staged.run(
        &format!("{name} start"),
        &strings(&[name, "start", &worker_id]),
        None,
        false,
    )?;
    Ok(Launched {
        worker_id,
        proxy_id,
    })
}

fn word_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_'
}

fn bounded_before(text: &[u8], at: usize) -> bool {
    at == 0 || !word_byte(text[at - 1])
}

fn bounded_after(text: &[u8], at: usize) -> bool {
    at >= text.len() || !word_byte(text[at])
}

/// The engine's own wording for an object it does not know: `no such container|network|object`,
/// or `container|network <name> not found`. A daemon it cannot reach says `no such file`.
fn names_missing_object(detail: &str) -> bool {
    let lower = detail.to_ascii_lowercase();
    let text = lower.as_bytes();
    let starts = |needle: &str| {
        lower
            .match_indices(needle)
            .map(|(at, _)| at)
            .filter(|at| bounded_before(text, *at))
            .collect::<Vec<_>>()
    };
    let no_such = starts("no such ").into_iter().any(|at| {
        let rest = &lower[at + "no such ".len()..];
        ["container", "network", "object"]
            .iter()
            .any(|noun| rest.starts_with(noun) && bounded_after(rest.as_bytes(), noun.len()))
    });
    let not_found = ["container ", "network "].iter().any(|noun| {
        starts(noun).into_iter().any(|at| {
            let rest = &lower[at + noun.len()..];
            let end = rest.find(char::is_whitespace).unwrap_or(rest.len());
            let after = &rest[end..];
            end > 0
                && after.starts_with(" not found")
                && bounded_after(after.as_bytes(), " not found".len())
        })
    });
    no_such || not_found
}

/// Whether the engine itself said the object does not exist; a CLI that did not answer never did.
pub fn gone(detail: &str, answered: bool) -> bool {
    answered && names_missing_object(detail)
}

/// The state of a worker container.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WorkerState {
    Running,
    Exited,
    /// The engine has no such container.
    Missing,
    /// An engine failure, an unreadable answer or a state that proves nothing.
    Unknown,
}

/// What the engine says about one worker container: the only proof that it stopped.
#[derive(Clone, Debug, PartialEq)]
pub struct Inspection {
    pub state: WorkerState,
    pub exit_code: Option<i64>,
    pub finished_at: Option<String>,
    pub oom_killed: Option<bool>,
    pub labels: Option<Map<String, Value>>,
}

impl Inspection {
    fn of(state: WorkerState) -> Self {
        Self {
            state,
            exit_code: None,
            finished_at: None,
            oom_killed: None,
            labels: None,
        }
    }

    /// The value of one label, if the answer carried labels.
    pub fn label(&self, key: &str) -> Option<&str> {
        self.labels.as_ref()?.get(key)?.as_str()
    }
}

/// Inspect one container by id or name.
pub fn inspect(engine: &dyn Engine, id: &str) -> Inspection {
    let name = engine.kind().as_str();
    let (ok, output, answered) = ask(
        engine,
        &strings(&[name, "container", "inspect", "--format", "{{json .}}", id]),
    );
    if !ok {
        return Inspection::of(if gone(&output, answered) {
            WorkerState::Missing
        } else {
            WorkerState::Unknown
        });
    }
    let Ok(info) = serde_json::from_str::<Value>(&output) else {
        return Inspection::of(WorkerState::Unknown);
    };
    let state = info.get("State");
    let status = state.and_then(|s| s.get("Status")).and_then(Value::as_str);
    let labels = match info.get("Config").and_then(|c| c.get("Labels")) {
        Some(Value::Object(labels)) => Some(labels.clone()),
        Some(Value::Null) => Some(Map::new()),
        _ => None,
    };
    let (Some(status), Some(labels)) = (status, labels) else {
        return Inspection::of(WorkerState::Unknown);
    };
    let mut result = Inspection::of(WorkerState::Unknown);
    result.labels = Some(labels);
    match status {
        "running" | "paused" | "restarting" => result.state = WorkerState::Running,
        "exited" | "dead" => {
            let field = |key: &str| state.and_then(|s| s.get(key));
            result.state = WorkerState::Exited;
            result.exit_code = field("ExitCode").and_then(Value::as_i64);
            result.finished_at = field("FinishedAt")
                .and_then(Value::as_str)
                .map(str::to_owned);
            result.oom_killed = field("OOMKilled").and_then(Value::as_bool);
        }
        _ => {}
    }
    result
}

/// One container or network the harness created for a repository.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Object {
    pub engine: Kind,
    pub kind: ObjectKind,
    pub id: String,
    pub name: String,
    pub dispatch: String,
    /// `worker`, `proxy` or `network`.
    pub role: String,
    /// The engine status of a container; `present` for a network.
    pub state: String,
    /// The `harness.execution` label, absent on objects of the Python executor.
    pub execution: Option<String>,
}

fn listed_ids(engine: &dyn Engine, command: &[&str], filters: &[String]) -> Option<Vec<String>> {
    let mut argv = strings(command);
    argv.extend(strings(&["--quiet", "--no-trunc"]));
    for filter in filters {
        argv.extend(["--filter".to_owned(), filter.clone()]);
    }
    let (ok, output, _) = ask(engine, &argv);
    ok.then(|| output.split_whitespace().map(str::to_owned).collect())
}

fn labelled(
    engine: &dyn Engine,
    kind: ObjectKind,
    ids: &[String],
    repository: &str,
    dispatch_id: Option<&str>,
) -> Option<Vec<Object>> {
    if ids.is_empty() {
        return Some(Vec::new());
    }
    let mut argv = strings(&[engine.kind().as_str(), kind.as_str(), "inspect"]);
    argv.extend(ids.iter().cloned());
    let (ok, output, _) = ask(engine, &argv);
    let inspected = ok
        .then(|| serde_json::from_str::<Value>(&output).ok())
        .flatten()?;
    let container = kind == ObjectKind::Container;
    let mut found = Vec::new();
    for item in inspected.as_array()? {
        let labels = if container {
            item.get("Config").and_then(|c| c.get("Labels"))
        } else {
            item.get("Labels")
        };
        let Some(labels) = labels.and_then(Value::as_object) else {
            continue;
        };
        let Some(dispatch) = labels.get(LABEL_DISPATCH).and_then(Value::as_str) else {
            continue;
        };
        let name = item
            .get("Name")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .trim_start_matches('/');
        let role = if name == argv::container_name(dispatch) {
            "worker"
        } else if name == argv::proxy_name(dispatch) {
            "proxy"
        } else if name == argv::network_name(dispatch) {
            "network"
        } else {
            continue;
        };
        // The engine filter only narrows the search: every object is checked against its labels.
        if labels.get(LABEL_REPOSITORY).and_then(Value::as_str) != Some(repository)
            || dispatch_id.is_some_and(|wanted| wanted != dispatch)
            || (role == "network") == container
        {
            continue;
        }
        let state = if container {
            item.get("State")
                .and_then(|s| s.get("Status"))
                .and_then(Value::as_str)
                .unwrap_or("unknown")
                .to_owned()
        } else {
            "present".to_owned()
        };
        found.push(Object {
            engine: engine.kind(),
            kind,
            id: item
                .get("Id")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_owned(),
            name: name.to_owned(),
            dispatch: dispatch.to_owned(),
            role: role.to_owned(),
            state,
            execution: labels
                .get(LABEL_EXECUTION)
                .and_then(Value::as_str)
                .map(str::to_owned),
        });
    }
    Some(found)
}

/// The containers and networks the harness created for `repository` (one dispatch if named).
///
/// `None` means the engine could not answer, which is never the same as "nothing is there".
pub fn objects(
    engine: &dyn Engine,
    repository: &str,
    dispatch_id: Option<&str>,
) -> Option<Vec<Object>> {
    let name = engine.kind().as_str();
    let mut filters = vec![format!("label={LABEL_REPOSITORY}={repository}")];
    if let Some(dispatch) = dispatch_id {
        filters.push(format!("label={LABEL_DISPATCH}={dispatch}"));
    }
    let containers = listed_ids(engine, &[name, "ps", "--all"], &filters)?;
    let networks = listed_ids(engine, &[name, "network", "ls"], &filters)?;
    let mut found = labelled(
        engine,
        ObjectKind::Container,
        &containers,
        repository,
        dispatch_id,
    )?;
    found.extend(labelled(
        engine,
        ObjectKind::Network,
        &networks,
        repository,
        dispatch_id,
    )?);
    Some(found)
}

/// What a removal did: objects removed, already absent, and refused with a reason.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Removal {
    pub removed: Vec<String>,
    pub missing: Vec<String>,
    pub failed: Vec<(String, String)>,
}

fn role_order(role: &str) -> u8 {
    match role {
        "worker" => 0,
        "proxy" => 1,
        _ => 2,
    }
}

/// Remove objects dispatch by dispatch: worker, then sidecar, then network.
///
/// The worker goes by a plain `rm`: the engine itself refuses a running container. The sidecar
/// is force-removed only once its dispatch's worker is gone, and a network only after the
/// containers on it. An object already absent is `missing`, so a repeated run is safe.
pub fn remove(engine: &dyn Engine, objects: &[Object]) -> Removal {
    let name = engine.kind().as_str();
    let mut result = Removal::default();
    let mut dispatches: Vec<&str> = Vec::new();
    for item in objects {
        if !dispatches.contains(&item.dispatch.as_str()) {
            dispatches.push(&item.dispatch);
        }
    }
    for dispatch in dispatches {
        let mut group: Vec<&Object> = objects.iter().filter(|o| o.dispatch == dispatch).collect();
        group.sort_by_key(|item| role_order(&item.role));
        let mut gone_so_far = true;
        for item in group {
            let label = format!("{} {}", item.kind.as_str(), item.name);
            if !gone_so_far {
                result.failed.push((
                    label,
                    "an earlier object of this dispatch is still present".to_owned(),
                ));
                continue;
            }
            let argv = match item.role.as_str() {
                "worker" => strings(&[name, "rm", &item.id]),
                "proxy" => strings(&[name, "rm", "--force", &item.id]),
                _ => strings(&[name, "network", "rm", &item.id]),
            };
            let (ok, detail, answered) = ask(engine, &argv);
            if ok {
                result.removed.push(label);
            } else if gone(&detail, answered) {
                result.missing.push(label);
            } else {
                result.failed.push((label, detail));
                gone_so_far = false;
            }
        }
    }
    result
}

/// `<engine> stop --time <grace>`: the engine signals the worker and waits up to `grace`.
pub fn stop(engine: &dyn Engine, id: &str, grace: Duration) -> (bool, String) {
    let name = engine.kind().as_str();
    let answer = engine.run(
        &strings(&[name, "stop", "--time", &grace.as_secs().to_string(), id]),
        grace + ENGINE_TIMEOUT,
    );
    (answer.ok(), answer.output())
}

/// `<engine> kill`: the worker outlived its stop.
pub fn kill(engine: &dyn Engine, id: &str) -> (bool, String) {
    let (ok, output, _) = ask(engine, &strings(&[engine.kind().as_str(), "kill", id]));
    (ok, output)
}

/// Save the worker's standard output and error to the two files.
///
/// # Errors
/// The engine refused or did not answer, or a file could not be written.
pub fn logs(engine: &dyn Engine, id: &str, stdout: &Path, stderr: &Path) -> io::Result<()> {
    let answer = engine.run(
        &strings(&[engine.kind().as_str(), "logs", id]),
        ENGINE_TIMEOUT,
    );
    if !answer.ok() {
        return Err(io::Error::other(answer.output()));
    }
    fs::write(stdout, answer.stdout.as_bytes())?;
    fs::write(stderr, answer.stderr.as_bytes())
}
