//! The identity of a process and the verdict on its liveness (ADR 0020, section 7).
//!
//! An identity is what the runtime observes about a pid: the host, the boot, the PID namespace
//! that numbers the pid (the namespace of the runtime), the pid and the start time of the process
//! that holds it. Only an identity proves an owner alive or dead; a pid alone proves neither.
//!
//! The verdict:
//! - `owner-alive`: the pid is held by a process with the recorded start time;
//! - `owner-dead`: the same host, boot and PID namespace, and no process holds the pid;
//! - `owner-pid-reused`: a process with another start time holds the pid; it never proves death;
//! - `owner-in-other-environment`: another boot or PID namespace;
//! - `owner-on-another-host`: another host;
//! - `owner-unknown`: the identity or the observation is missing.
//!
//! Linux reads `/proc/<pid>/stat` (field 22, the start time in clock ticks since boot),
//! `/proc/self/ns/pid` and the boot id. Windows asks `OpenProcess`, `GetExitCodeProcess` and
//! `GetProcessTimes` (the creation time). Another Unix observes no start time, so its identity
//! is unknown. A debug build lets a test replace the observed PID namespace and boot id with
//! `HARNESS_RUNTIME_TEST_PID_NAMESPACE` and `HARNESS_RUNTIME_TEST_BOOT_ID`.

use serde_json::{Map, Value, json};

use crate::platform;

/// The identity of the process that holds a pid, as the runtime observed it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Identity {
    pub host: String,
    pub boot_id: Option<String>,
    pub pid_namespace: Option<String>,
    pub pid: i64,
    pub start_time: String,
}

/// Where the runtime observes from: its host, boot and PID namespace.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Observer {
    pub host: String,
    pub boot_id: Option<String>,
    pub pid_namespace: Option<String>,
}

/// What the runtime sees at a pid now.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Probe {
    /// No process holds the pid, or only one that has exited (a zombie).
    Absent,
    /// A process holds the pid; its start time when the platform reveals it.
    Running(Option<String>),
    /// The platform cannot tell.
    Unknown,
}

/// The verdict on an owner's liveness.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Verdict {
    Alive,
    Dead,
    PidReused,
    OtherEnvironment,
    OnAnotherHost,
    Unknown,
}

impl Verdict {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Alive => "owner-alive",
            Self::Dead => "owner-dead",
            Self::PidReused => "owner-pid-reused",
            Self::OtherEnvironment => "owner-in-other-environment",
            Self::OnAnotherHost => "owner-on-another-host",
            Self::Unknown => "owner-unknown",
        }
    }
}

impl Identity {
    /// The identity as a JSON object, with `null` for what the platform does not reveal.
    pub fn to_json(&self) -> Value {
        json!({
            "host": self.host,
            "boot_id": self.boot_id,
            "pid_namespace": self.pid_namespace,
            "pid": self.pid,
            "start_time": self.start_time,
        })
    }

    /// The identity of a JSON object `to_json` wrote, or `None` when it is incomplete.
    pub fn from_json(value: &Value) -> Option<Self> {
        let object: &Map<String, Value> = value.as_object()?;
        let text = |key: &str| object.get(key).and_then(Value::as_str).map(str::to_owned);
        Some(Self {
            host: text("host")?,
            boot_id: text("boot_id"),
            pid_namespace: text("pid_namespace"),
            pid: object.get("pid").and_then(Value::as_i64)?,
            start_time: text("start_time")?,
        })
    }
}

/// The observer of this runtime. A debug build takes the PID namespace and the boot id from
/// `HARNESS_RUNTIME_TEST_PID_NAMESPACE` and `HARNESS_RUNTIME_TEST_BOOT_ID` when a test sets them.
pub fn observer() -> Observer {
    let environment = platform::environment();
    #[allow(unused_mut)]
    let mut observer = Observer {
        host: platform::hostname(),
        boot_id: environment.boot_id,
        pid_namespace: environment.pid_namespace,
    };
    #[cfg(debug_assertions)]
    {
        if let Ok(namespace) = std::env::var("HARNESS_RUNTIME_TEST_PID_NAMESPACE") {
            observer.pid_namespace = Some(namespace);
        }
        if let Ok(boot) = std::env::var("HARNESS_RUNTIME_TEST_BOOT_ID") {
            observer.boot_id = Some(boot);
        }
    }
    observer
}

/// The process state and the start time of a `/proc/<pid>/stat` line. The command name in
/// parentheses may hold spaces and `)`, so the fields are counted after the last `)`.
pub fn parse_stat(line: &str) -> Option<(char, String)> {
    let rest = &line[line.rfind(')')? + 1..];
    let mut fields = rest.split_whitespace();
    let state = fields.next()?.chars().next()?;
    // The state is field 3; the start time is field 22.
    let start_time = fields.nth(18)?;
    start_time.parse::<u64>().ok()?;
    Some((state, start_time.to_owned()))
}

/// What the runtime sees at `pid` now.
pub fn probe(pid: i64) -> Probe {
    if pid <= 0 {
        return Probe::Unknown;
    }
    #[cfg(target_os = "linux")]
    {
        match std::fs::read_to_string(format!("/proc/{pid}/stat")) {
            Ok(line) => match parse_stat(&line) {
                // A zombie has exited: it holds the pid, never a lease.
                Some(('Z' | 'X', _)) => Probe::Absent,
                Some((_, start_time)) => Probe::Running(Some(start_time)),
                None => Probe::Unknown,
            },
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => Probe::Absent,
            Err(_) => Probe::Unknown,
        }
    }
    #[cfg(windows)]
    {
        let Ok(pid) = u32::try_from(pid) else {
            return Probe::Absent;
        };
        match platform_ffi::process_creation_time(pid) {
            Ok(Some(created)) => Probe::Running(Some(created.to_string())),
            Ok(None) => Probe::Absent,
            Err(_) => Probe::Unknown,
        }
    }
    #[cfg(all(unix, not(target_os = "linux")))]
    {
        match platform::process_alive(pid) {
            Some(true) => Probe::Running(None),
            Some(false) => Probe::Absent,
            None => Probe::Unknown,
        }
    }
}

/// The identity of the process that holds `pid` now, as `observer` sees it, or `None` when the
/// platform reveals no start time for it.
pub fn observe(pid: i64, observer: &Observer) -> Option<Identity> {
    match probe(pid) {
        Probe::Running(Some(start_time)) => Some(Identity {
            host: observer.host.clone(),
            boot_id: observer.boot_id.clone(),
            pid_namespace: observer.pid_namespace.clone(),
            pid,
            start_time,
        }),
        _ => None,
    }
}

/// Whether one environment value, the recorded one and the observed one, matches: `Some(true)`
/// when both match or the platform reveals neither, `Some(false)` when they differ and `None`
/// when only one side knows it.
fn same(recorded: Option<&String>, observed: Option<&String>) -> Option<bool> {
    match (recorded, observed) {
        (Some(recorded), Some(observed)) => Some(recorded == observed),
        (None, None) => Some(true),
        _ => None,
    }
}

/// The verdict on the owner `recorded`, observed by `observer` with `probe` at its pid.
pub fn verdict(recorded: Option<&Identity>, observer: &Observer, probe: &Probe) -> Verdict {
    let Some(recorded) = recorded.filter(|recorded| recorded.pid > 0) else {
        return Verdict::Unknown;
    };
    if recorded.host != observer.host {
        return Verdict::OnAnotherHost;
    }
    let environment = [
        same(recorded.boot_id.as_ref(), observer.boot_id.as_ref()),
        same(
            recorded.pid_namespace.as_ref(),
            observer.pid_namespace.as_ref(),
        ),
    ];
    if environment.contains(&Some(false)) {
        return Verdict::OtherEnvironment;
    }
    if environment.contains(&None) {
        return Verdict::Unknown;
    }
    match probe {
        Probe::Absent => Verdict::Dead,
        Probe::Running(Some(start_time)) if *start_time == recorded.start_time => Verdict::Alive,
        Probe::Running(Some(_)) => Verdict::PidReused,
        Probe::Running(None) | Probe::Unknown => Verdict::Unknown,
    }
}

/// The verdict of this runtime on the owner `recorded`, observed now.
pub fn judge(recorded: Option<&Identity>) -> Verdict {
    let observer = observer();
    let probe = recorded.map_or(Probe::Unknown, |recorded| probe(recorded.pid));
    verdict(recorded, &observer, &probe)
}
