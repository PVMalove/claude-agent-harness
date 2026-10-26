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

#[cfg(debug_assertions)]
use crate::error::Error;
use crate::error::Result;

pub const STEPS: [&str; 9] = [
    "preconditions",
    "locked",
    "snapshot",
    "import",
    "parity",
    "renamed",
    "authority",
    "pointer-replace",
    "pointer",
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
