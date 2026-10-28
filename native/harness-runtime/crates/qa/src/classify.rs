//! The exit code table of a containerised QA command (ADR 0022, section 3).

use contracts::FailureDomain;

/// How one QA command ended: its real exit code, and whether the runtime stopped it at its
/// deadline or on an explicit `qa.stop`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Ending {
    /// The exit code of the engine CLI, which is the container's; `None` when the CLI did not
    /// start or a signal ended the CLI itself.
    pub code: Option<i32>,
    pub deadline: bool,
    pub stopped: bool,
}

/// The failure domain of an ending, or `None` when the code is the command's own result.
///
/// In order: a command stopped at its deadline that did not pass is a `timeout`; no exit code is
/// `infrastructure`; 125 (the engine failed) is `infrastructure`; 126 and 127 (the command
/// cannot run or is not found) are `tooling`; 129 to 255 (a signal) are `cancelled` after an
/// explicit stop and `unknown` otherwise. Any other code is the command's own result.
pub const fn classify(ending: Ending) -> Option<FailureDomain> {
    if ending.deadline && !matches!(ending.code, Some(0)) {
        return Some(FailureDomain::Timeout);
    }
    match ending.code {
        None | Some(125) => Some(FailureDomain::Infrastructure),
        Some(126 | 127) => Some(FailureDomain::Tooling),
        Some(129..=255) if ending.stopped => Some(FailureDomain::Cancelled),
        Some(129..=255) => Some(FailureDomain::Unknown),
        Some(_) => None,
    }
}
