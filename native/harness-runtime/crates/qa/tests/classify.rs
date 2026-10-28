//! The exit code table of a containerised QA command (ADR 0022, section 3; ADR 0023, section 5):
//! the code is always the real one, and only the domain says who is to blame.

use contracts::FailureDomain;
use qa::classify::{Ending, classify};

fn ended(code: Option<i32>) -> Ending {
    Ending {
        code,
        deadline: false,
        stopped: false,
    }
}

#[test]
fn a_command_of_its_own_result_has_no_domain() {
    for code in [0, 1, 2, 5, 124, 128] {
        assert_eq!(classify(ended(Some(code))), None, "{code}");
    }
}

#[test]
fn the_engine_and_the_shell_codes_name_their_domain() {
    assert_eq!(
        classify(ended(Some(125))),
        Some(FailureDomain::Infrastructure)
    );
    assert_eq!(classify(ended(Some(126))), Some(FailureDomain::Tooling));
    assert_eq!(classify(ended(Some(127))), Some(FailureDomain::Tooling));
    assert_eq!(classify(ended(None)), Some(FailureDomain::Infrastructure));
}

#[test]
fn a_signal_code_is_cancelled_only_after_an_explicit_stop() {
    for code in [129, 137, 143, 255] {
        assert_eq!(classify(ended(Some(code))), Some(FailureDomain::Unknown));
        let stopped = Ending {
            stopped: true,
            ..ended(Some(code))
        };
        assert_eq!(classify(stopped), Some(FailureDomain::Cancelled), "{code}");
    }
}

#[test]
fn a_command_stopped_at_its_deadline_is_a_timeout_unless_it_passed() {
    for code in [Some(1), Some(137), Some(143), None] {
        let late = Ending {
            deadline: true,
            ..ended(code)
        };
        assert_eq!(classify(late), Some(FailureDomain::Timeout), "{code:?}");
    }
    let passed = Ending {
        deadline: true,
        ..ended(Some(0))
    };
    assert_eq!(classify(passed), None);
}
