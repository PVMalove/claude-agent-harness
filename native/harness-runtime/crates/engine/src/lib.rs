//! The container engines of `harness-runtime` (ADR 0022, section 4; ADR 0025).
//!
//! Every engine command of the runtime goes through the [`Engine`] trait: [`Docker`] and
//! [`Podman`] run the CLI, [`Scripted`] answers by argv prefix in tests. The argv builders are a
//! one-to-one port of the Python executor of ADR 0025; the only delta is the labels
//! `harness.operation` and `harness.execution` after `harness.plan_digest`. Nothing here builds or
//! pulls an image, and nothing lists or removes a volume. The one attached run is the foreground
//! `run --rm --init` of a QA command (ADR 0022, section 3): [`Engine::spawn`] writes its standard
//! output and error into two files and answers a handle the caller waits on.

pub mod argv;
pub mod facts;
pub mod ops;
pub mod plan;
pub mod policy;
mod scripted;

use std::fs::File;
use std::io::{self, Read};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

pub use scripted::{Run, Scripted};

/// One local engine query or container creation; a stuck daemon is the worst expected case.
pub const ENGINE_TIMEOUT: Duration = Duration::from_secs(30);
const POLL: Duration = Duration::from_millis(20);

/// A container engine the plan may name: exactly `access.container_engine` of `systemFields.v1`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Kind {
    Docker,
    Podman,
}

impl Kind {
    /// Every engine, in the order of the registry.
    pub const ALL: [Self; 2] = [Self::Docker, Self::Podman];

    /// The engine name, which is also its CLI binary.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Docker => "docker",
            Self::Podman => "podman",
        }
    }

    /// The engine a name stands for.
    pub fn parse(name: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|kind| kind.as_str() == name)
    }

    /// The network a sidecar joins to reach the outside: the engine's default one.
    pub const fn default_network(self) -> &'static str {
        match self {
            Self::Docker => "bridge",
            Self::Podman => "podman",
        }
    }
}

/// What one engine command answered.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Answer {
    /// Whether the CLI started and ended in time: a CLI that did not never answered.
    pub answered: bool,
    /// The exit code of an answered command.
    pub code: Option<i32>,
    pub stdout: String,
    /// The standard error, or why the CLI did not answer.
    pub stderr: String,
}

impl Answer {
    /// A successful command with this standard output.
    pub fn success(stdout: impl Into<String>) -> Self {
        Self {
            answered: true,
            code: Some(0),
            stdout: stdout.into(),
            stderr: String::new(),
        }
    }

    /// A command that exited 1 with this standard error.
    pub fn failure(stderr: impl Into<String>) -> Self {
        Self {
            answered: true,
            code: Some(1),
            stdout: String::new(),
            stderr: stderr.into(),
        }
    }

    /// A CLI that could not start or did not answer in time.
    pub fn silent(detail: impl Into<String>) -> Self {
        Self {
            answered: false,
            code: None,
            stdout: String::new(),
            stderr: detail.into(),
        }
    }

    /// Whether the command succeeded.
    pub fn ok(&self) -> bool {
        self.answered && self.code == Some(0)
    }

    /// The trimmed output of a success, otherwise the failure detail.
    pub fn output(&self) -> String {
        if self.ok() {
            return self.stdout.trim().to_owned();
        }
        if !self.answered {
            return self.stderr.trim().to_owned();
        }
        let detail = if self.stderr.is_empty() {
            &self.stdout
        } else {
            &self.stderr
        }
        .trim();
        match (detail.is_empty(), self.code) {
            (false, _) => detail.to_owned(),
            (true, Some(code)) => format!("exit code {code}"),
            (true, None) => "killed by a signal".to_owned(),
        }
    }
}

/// How an attached engine CLI ended.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Exit {
    /// The exit code of the CLI; `None` when a signal ended it.
    pub code: Option<i32>,
}

/// An engine CLI that runs attached: the foreground `run --rm` of one QA command.
pub trait Attached: Send {
    /// How the CLI ended, or `None` while it runs.
    ///
    /// # Errors
    /// The CLI cannot be waited for.
    fn try_wait(&mut self) -> io::Result<Option<Exit>>;

    /// Kill the CLI itself and reap it; the container is stopped through the engine instead.
    fn kill(&mut self);
}

/// The only process boundary of a container engine.
pub trait Engine: Send + Sync {
    /// The engine this is.
    fn kind(&self) -> Kind;

    /// Run one command, `argv[0]` being the engine binary, and wait at most `timeout`.
    fn run(&self, argv: &[String], timeout: Duration) -> Answer;

    /// Start one command attached, `argv[0]` being the engine binary, with its standard output
    /// and error in the two files; the caller waits on the handle.
    ///
    /// # Errors
    /// Why the CLI could not start.
    fn spawn(
        &self,
        argv: &[String],
        stdout: File,
        stderr: File,
    ) -> Result<Box<dyn Attached>, String> {
        let _ = (argv, stdout, stderr);
        Err(format!(
            "the {} engine runs no attached command",
            self.kind().as_str()
        ))
    }
}

/// The Docker CLI.
#[derive(Clone, Copy, Debug, Default)]
pub struct Docker;

/// The Podman CLI.
#[derive(Clone, Copy, Debug, Default)]
pub struct Podman;

impl Engine for Docker {
    fn kind(&self) -> Kind {
        Kind::Docker
    }

    fn run(&self, argv: &[String], timeout: Duration) -> Answer {
        run_cli(Kind::Docker, argv, timeout)
    }

    fn spawn(
        &self,
        argv: &[String],
        stdout: File,
        stderr: File,
    ) -> Result<Box<dyn Attached>, String> {
        spawn_cli(Kind::Docker, argv, stdout, stderr)
    }
}

impl Engine for Podman {
    fn kind(&self) -> Kind {
        Kind::Podman
    }

    fn run(&self, argv: &[String], timeout: Duration) -> Answer {
        run_cli(Kind::Podman, argv, timeout)
    }

    fn spawn(
        &self,
        argv: &[String],
        stdout: File,
        stderr: File,
    ) -> Result<Box<dyn Attached>, String> {
        spawn_cli(Kind::Podman, argv, stdout, stderr)
    }
}

/// The CLI engine of `kind`.
pub fn cli(kind: Kind) -> std::sync::Arc<dyn Engine> {
    match kind {
        Kind::Docker => std::sync::Arc::new(Docker),
        Kind::Podman => std::sync::Arc::new(Podman),
    }
}

fn drain(mut stream: impl Read + Send + 'static) -> thread::JoinHandle<String> {
    thread::spawn(move || {
        let mut bytes = Vec::new();
        let _ = stream.read_to_end(&mut bytes);
        String::from_utf8_lossy(&bytes).into_owned()
    })
}

/// The binary and arguments of an engine command, when `argv[0]` is the binary of `kind`.
fn own_binary(kind: Kind, argv: &[String]) -> Result<(&String, &[String]), String> {
    let Some((binary, args)) = argv.split_first() else {
        return Err("an engine command needs its binary".to_owned());
    };
    if binary != kind.as_str() {
        return Err(format!(
            "the {} engine runs only its own binary, not {binary}",
            kind.as_str()
        ));
    }
    Ok((binary, args))
}

/// An attached engine CLI: the leader of its own process group.
struct CliRun {
    child: Child,
}

impl Attached for CliRun {
    fn try_wait(&mut self) -> io::Result<Option<Exit>> {
        Ok(self.child.try_wait()?.map(|status| Exit {
            code: status.code(),
        }))
    }

    fn kill(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn spawn_cli(
    kind: Kind,
    argv: &[String],
    stdout: File,
    stderr: File,
) -> Result<Box<dyn Attached>, String> {
    let (binary, args) = own_binary(kind, argv)?;
    let mut command = Command::new(binary);
    command
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr));
    // Its own group: a signal to the runtime's group never reaches the CLI of a QA command.
    #[cfg(unix)]
    std::os::unix::process::CommandExt::process_group(&mut command, 0);
    let child = command.spawn().map_err(|err| format!("{binary}: {err}"))?;
    Ok(Box::new(CliRun { child }))
}

fn run_cli(kind: Kind, argv: &[String], timeout: Duration) -> Answer {
    let (binary, args) = match own_binary(kind, argv) {
        Ok(found) => found,
        Err(detail) => return Answer::silent(detail),
    };
    let mut child = match Command::new(binary)
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
    {
        Ok(child) => child,
        Err(err) => return Answer::silent(format!("{binary}: {err}")),
    };
    // The readers run while the CLI does, so a full pipe never stalls it.
    let (Some(stdout), Some(stderr)) = (child.stdout.take(), child.stderr.take()) else {
        let _ = child.kill();
        let _ = child.wait();
        return Answer::silent(format!("{binary}: no output pipes"));
    };
    let (stdout, stderr) = (drain(stdout), drain(stderr));
    let started = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                return Answer {
                    answered: true,
                    code: status.code(),
                    stdout: stdout.join().unwrap_or_default(),
                    stderr: stderr.join().unwrap_or_default(),
                };
            }
            Ok(None) if started.elapsed() < timeout => thread::sleep(POLL),
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                return Answer::silent(format!(
                    "{binary} did not answer in {} seconds",
                    timeout.as_secs()
                ));
            }
            Err(err) => return Answer::silent(format!("{binary}: {err}")),
        }
    }
}
