//! Test hooks at the step boundaries of `migrate`.
//!
//! In a debug build, `HARNESS_RUNTIME_TEST_PAUSE_AT=<step>` makes the process print
//! `PAUSED:<step>` to stderr and wait for one line on stdin, so a test can kill it, or change the
//! legacy state, exactly there. `HARNESS_RUNTIME_TEST_FAIL_AT=<step>` makes the step fail with an
//! I/O error, exit code 3. A release build compiles both away: the environment variables have no
//! effect there.
//!
//! The steps, in order, are `preconditions` (step 1 is done), `locked` (the legacy lock is held),
//! `snapshot`, `import` (the rows are written and the transaction is still open), `parity`,
//! `renamed` (the store is `runtime.db`), `authority` (`AUTHORITY.json` is written),
//! `pointer-replace` (just before the pointer is replaced) and `pointer` (the pointer is
//! replaced: the commit point is behind). A failure cannot be injected at `pointer`, because
//! the cutover is done by then.
//!
//! `rollback --to-legacy` has its own steps: `rollback-locked` (the legacy lock is held),
//! `rollback-checked` (the store passed its preconditions), `rollback-archive` (the archive is
//! installed), `rollback-generation` (the generation is written and checked), `rollback-audit`
//! (the store has the `runtime-rollback` record and is read-only), `rollback-authority`
//! (`AUTHORITY.json` names the file ledger), `rollback-pointer-replace` (just before the pointer
//! is replaced) and `rollback-pointer` (the commit point is behind).

#[cfg(debug_assertions)]
use crate::error::Error;
use crate::error::Result;

pub const STEPS: [&str; 17] = [
    "preconditions",
    "locked",
    "snapshot",
    "import",
    "parity",
    "renamed",
    "authority",
    "pointer-replace",
    "pointer",
    "rollback-locked",
    "rollback-checked",
    "rollback-archive",
    "rollback-generation",
    "rollback-audit",
    "rollback-authority",
    "rollback-pointer-replace",
    "rollback-pointer",
];

#[cfg(debug_assertions)]
fn selected(variable: &str, step: &str) -> bool {
    std::env::var(variable).is_ok_and(|wanted| wanted == step)
}

/// Pause, if a test asked for it at `step`.
#[cfg(debug_assertions)]
pub fn pause(step: &str) {
    if selected("HARNESS_RUNTIME_TEST_PAUSE_AT", step) {
        eprintln!("PAUSED:{step}");
        let mut line = String::new();
        let _ = std::io::stdin().read_line(&mut line);
    }
}

#[cfg(not(debug_assertions))]
pub fn pause(_step: &str) {}

/// Pause, then fail, if a test asked for either at `step`.
#[cfg(debug_assertions)]
pub fn reach(step: &str) -> Result<()> {
    pause(step);
    if selected("HARNESS_RUNTIME_TEST_FAIL_AT", step) {
        return Err(Error::Interrupted(format!(
            "injected failure at the step {step}"
        )));
    }
    Ok(())
}

#[cfg(not(debug_assertions))]
pub fn reach(_step: &str) -> Result<()> {
    Ok(())
}

/// Whether a test asked for the pause or the failure at `step`, and this is the first time the
/// process reaches it. A serving runtime handles many requests, and a test wants the hook at one.
#[cfg(debug_assertions)]
fn first_asked(step: &str, variables: &[&str]) -> bool {
    use std::sync::{Mutex, PoisonError};

    static FIRED: Mutex<Vec<String>> = Mutex::new(Vec::new());
    if !variables.iter().any(|variable| selected(variable, step)) {
        return false;
    }
    let mut fired = FIRED.lock().unwrap_or_else(PoisonError::into_inner);
    if fired.iter().any(|done| done == step) {
        return false;
    }
    fired.push(step.to_owned());
    true
}

/// Like `reach`, for a step of the serving runtime (`state.write:before-begin`,
/// `state.write:in-transaction`, `state.read:in-snapshot`, after the first `SELECT` of a snapshot
/// that a read opens, and the steps of a launch of the supervisor: `execution.launch:before-begin`,
/// `execution.launch:in-transaction`, `execution.launch:intent`, `execution.launch:launched` and
/// `execution.launch:receipt`): the pause and the failure happen once per process.
#[cfg(debug_assertions)]
pub fn reach_once(step: &str) -> Result<()> {
    if !first_asked(
        step,
        &[
            "HARNESS_RUNTIME_TEST_PAUSE_AT",
            "HARNESS_RUNTIME_TEST_FAIL_AT",
        ],
    ) {
        return Ok(());
    }
    reach(step)
}

#[cfg(not(debug_assertions))]
pub fn reach_once(_step: &str) -> Result<()> {
    Ok(())
}

/// Whether a test asked, once, for the failure at `step` that has no error to return: the
/// response of an operation is dropped after it succeeded (`state.write:respond`,
/// `execution.launch:respond`).
#[cfg(debug_assertions)]
pub fn fail_once(step: &str) -> bool {
    first_asked(step, &["HARNESS_RUNTIME_TEST_FAIL_AT"])
}

#[cfg(not(debug_assertions))]
pub fn fail_once(_step: &str) -> bool {
    false
}

#[cfg(all(test, debug_assertions))]
mod tests {
    use super::*;

    #[test]
    fn a_step_passes_when_no_test_asked_for_a_failure_there() {
        assert!(reach("snapshot").is_ok());
    }

    #[test]
    fn every_step_the_hooks_name_is_listed_once() {
        let mut steps = STEPS.to_vec();
        steps.sort_unstable();
        steps.dedup();
        assert_eq!(steps.len(), STEPS.len());
    }
}
