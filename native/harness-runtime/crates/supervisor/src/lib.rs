//! The process supervisor of `harness-runtime` (ADR 0022, sections 2 and 3; ADR 0021,
//! sections 4 and 6).
//!
//! On Linux the runtime launches the execution of a dispatch from its stored brief and status in
//! three steps: the intent (an `operations` row and an `executions` row in `launching`, one
//! transaction), the spawn in an own process group with the labels `HARNESS_OPERATION_ID` and
//! `HARNESS_DISPATCH_ID`, and the receipt (the identity of the process, `running`, the operation
//! `committed`, one transaction). A repeat of the operation answers the stored receipt. A runtime
//! that died between the steps is followed by one that finds the process by its labels and adopts
//! it, or, when no labelled process lives, records the execution `lost` and never launches again.
//!
//! Reconcile runs before the runtime reports ready, before every execution operation and
//! periodically. One thread waits for each child of this runtime; the exit status it reaps is the
//! exit evidence. The supervisor never writes a `DispatchState`, and nothing it does kills an
//! execution when the runtime ends. Other platforms build a stub that serves no execution
//! operation; `hello` does not list them there.
//!
//! A brief whose `runtime_access` names the built-in `docker` extension runs as a `container`
//! execution (ADR 0022, section 4): the runtime checks the plan's mounts against the engine
//! sockets and its own paths, the environment names and the pinned images before the intent,
//! creates the worker's objects through the engine of the plan and records the container id.
//! Every later engine command of the row uses the engine the row names.
//!
//! A QA dispatch whose approved brief pins `qa_execution` runs its commands through `qa.run`
//! (ADR 0022, section 3): one `qa` execution per attempt, the run-wide sidecar and network, one
//! foreground `run --rm --init` container per command on a thread of the runtime, and the exit
//! evidence of every command; `qa.stop` stops it. A QA run no thread of this runtime holds after
//! a restart is stopped and `lost`, never resumed.

#[cfg(target_os = "linux")]
mod container;
#[cfg(target_os = "linux")]
mod launch;
#[cfg(target_os = "linux")]
mod linux;
#[cfg(target_os = "linux")]
mod objects;
#[cfg(target_os = "linux")]
pub mod process;
#[cfg(target_os = "linux")]
mod qa_run;
#[cfg(target_os = "linux")]
mod stop;

// The platform layer behind the seam of `launch` and `stop`: the process of an execution, its
// identity, the search for it after a restart and the signals of a stop.
#[cfg(target_os = "linux")]
use linux as os;

use std::fmt;
use std::path::PathBuf;
use std::sync::Arc;

/// The operations the supervisor serves, as `hello` lists them.
pub const FEATURES: [&str; 7] = [
    "execution.launch",
    "execution.observe",
    "container.observe",
    "container.objects",
    "container.remove",
    "qa.run",
    "qa.stop",
];

/// The engine of a kind: the CLI by default; tests script it.
pub type Engines = Arc<dyn Fn(engine::Kind) -> Arc<dyn engine::Engine> + Send + Sync>;

/// The supervisor of one runtime: the state directory it launches from, the repository and
/// runtime paths a container must never see, its engines, and what it holds in memory about the
/// executions it serves.
pub struct Supervisor {
    state_dir: Option<PathBuf>,
    /// The runtime's own canonical Git common directory: the repository label of its objects.
    #[cfg_attr(not(target_os = "linux"), allow(dead_code))]
    repository: Option<PathBuf>,
    /// The runtime's own directory, socket, token and lock.
    #[cfg_attr(not(target_os = "linux"), allow(dead_code))]
    runtime_paths: Vec<PathBuf>,
    #[cfg_attr(not(target_os = "linux"), allow(dead_code))]
    engines: Engines,
    #[cfg(target_os = "linux")]
    inner: launch::Inner,
}

impl fmt::Debug for Supervisor {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("Supervisor")
            .field("state_dir", &self.state_dir)
            .field("repository", &self.repository)
            .field("runtime_paths", &self.runtime_paths)
            .finish_non_exhaustive()
    }
}

impl Default for Supervisor {
    fn default() -> Self {
        Self::new(None)
    }
}

impl Supervisor {
    /// The supervisor of the store of `state_dir`; `None` serves nothing.
    pub fn new(state_dir: Option<PathBuf>) -> Self {
        Self {
            state_dir,
            repository: None,
            runtime_paths: Vec::new(),
            engines: Arc::new(engine::cli),
            #[cfg(target_os = "linux")]
            inner: launch::Inner::default(),
        }
    }

    /// The same supervisor for the repository of the canonical Git common directory
    /// `repository`, whose runtime keeps `runtime_paths`.
    #[must_use]
    pub fn with_runtime(
        mut self,
        repository: Option<PathBuf>,
        runtime_paths: Vec<PathBuf>,
    ) -> Self {
        self.repository = repository;
        self.runtime_paths = runtime_paths;
        self
    }

    /// The same supervisor with other engines.
    #[must_use]
    pub fn with_engines(mut self, engines: Engines) -> Self {
        self.engines = engines;
        self
    }

    /// Whether this platform supervises processes.
    pub const fn supported() -> bool {
        cfg!(target_os = "linux")
    }
}

#[cfg(not(target_os = "linux"))]
use std::sync::atomic::AtomicBool;

#[cfg(not(target_os = "linux"))]
use contracts::{ErrorBody, Request};
#[cfg(not(target_os = "linux"))]
use serde_json::Value;

#[cfg(not(target_os = "linux"))]
impl Supervisor {
    fn unsupported(request: &Request) -> ErrorBody {
        ErrorBody::new(
            contracts::ErrorCode::UnknownOperation,
            contracts::FailureDomain::Tooling,
            false,
            format!(
                "this platform has no process supervisor for {:?}",
                request.operation
            ),
            "use an operation the hello features list names",
        )
    }

    /// `execution.launch`: not served on this platform.
    ///
    /// # Errors
    /// Always `unknown_operation`.
    pub fn launch(self: &Arc<Self>, request: &Request) -> Result<Value, ErrorBody> {
        let _ = &self.state_dir;
        Err(Self::unsupported(request))
    }

    /// `container.observe`: not served on this platform.
    ///
    /// # Errors
    /// Always `unknown_operation`.
    pub fn container_observe(self: &Arc<Self>, request: &Request) -> Result<Value, ErrorBody> {
        Err(Self::unsupported(request))
    }

    /// `container.objects`: not served on this platform.
    ///
    /// # Errors
    /// Always `unknown_operation`.
    pub fn container_objects(self: &Arc<Self>, request: &Request) -> Result<Value, ErrorBody> {
        Err(Self::unsupported(request))
    }

    /// `container.remove`: not served on this platform.
    ///
    /// # Errors
    /// Always `unknown_operation`.
    pub fn container_remove(self: &Arc<Self>, request: &Request) -> Result<Value, ErrorBody> {
        Err(Self::unsupported(request))
    }

    /// `qa.run`: not served on this platform.
    ///
    /// # Errors
    /// Always `unknown_operation`.
    pub fn qa_run(self: &Arc<Self>, request: &Request) -> Result<Value, ErrorBody> {
        Err(Self::unsupported(request))
    }

    /// `qa.stop`: not served on this platform.
    ///
    /// # Errors
    /// Always `unknown_operation`.
    pub fn qa_stop(self: &Arc<Self>, request: &Request) -> Result<Value, ErrorBody> {
        Err(Self::unsupported(request))
    }

    /// `execution.observe`: not served on this platform.
    ///
    /// # Errors
    /// Always `unknown_operation`.
    pub fn observe(self: &Arc<Self>, request: &Request) -> Result<Value, ErrorBody> {
        Err(Self::unsupported(request))
    }

    /// Reconcile: nothing to do on this platform.
    pub fn reconcile_all(self: &Arc<Self>) {}

    /// The periodic reconcile: nothing to do on this platform.
    pub fn start(self: &Arc<Self>, _stop: Arc<AtomicBool>) {}
}
