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

#[cfg(target_os = "linux")]
mod launch;
#[cfg(target_os = "linux")]
pub mod process;
#[cfg(target_os = "linux")]
mod stop;

use std::path::PathBuf;

/// The operations the supervisor serves, as `hello` lists them.
pub const FEATURES: [&str; 2] = ["execution.launch", "execution.observe"];

/// The supervisor of one runtime: the state directory it launches from, and what it holds in
/// memory about the executions it serves.
#[derive(Debug, Default)]
pub struct Supervisor {
    state_dir: Option<PathBuf>,
    #[cfg(target_os = "linux")]
    inner: launch::Inner,
}

impl Supervisor {
    /// The supervisor of the store of `state_dir`; `None` serves nothing.
    pub fn new(state_dir: Option<PathBuf>) -> Self {
        Self {
            state_dir,
            ..Self::default()
        }
    }

    /// Whether this platform supervises processes.
    pub const fn supported() -> bool {
        cfg!(target_os = "linux")
    }
}

#[cfg(not(target_os = "linux"))]
use std::sync::{Arc, atomic::AtomicBool};

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
