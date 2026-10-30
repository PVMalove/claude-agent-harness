//! The pure logic of the Windows supervisor (ADR 0022, sections 2, 3 and 5): the name of the job
//! object of an execution, which members of the job belong to the execution, and what an
//! observation of its leader and its job means. It makes no system call, so it builds and is
//! tested on every platform.
//!
//! The hidden console of an execution has a console host, `conhost.exe` of the system directory,
//! that Windows starts for it inside the job. It serves the console and ends after its last
//! client; it is a member of the job, but never a member of the execution (`execution_members`).
//!
//! The table of an observation; a member is a live member of the execution, the console host is
//! none:
//! - the leader runs with its recorded pid and creation time and is in the job: `running`;
//! - the leader runs outside its job, or the job has no member or no name: `unknown`, because the
//!   two observations contradict each other;
//! - the leader is gone (no process, another process at its pid, or ended) and a member of the job
//!   runs: `running`, until the members end or the deadline stops them;
//! - the leader ended while this runtime held a handle to it, and the job has no member: `exited`
//!   with the exit code of that handle;
//! - the leader is gone without a held handle and the job has no member, or the job has no name:
//!   `lost`;
//! - an observation that failed: `unknown`, and the lease stays held.

use contracts::ExecutionState;

/// The prefix of the name of every job object of an execution. `Global\` makes the name visible
/// from every session, so a runtime that starts in another session finds it.
pub const PREFIX: &str = r"Global\harness-runtime-execution-";

/// The name of the job object of the execution `operation_id`: the same for every runtime.
pub fn name(operation_id: &str) -> String {
    format!("{PREFIX}{operation_id}")
}

/// The file name of the console host of Windows.
const CONSOLE_HOST: &str = "conhost.exe";

/// A live member of the job object of an execution.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Member {
    pub pid: u32,
    /// Whether it is the console host of the hidden console of the execution.
    pub console_host: bool,
}

/// Whether the executable image `image` (a full Win32 path) is the console host: `conhost.exe` in
/// the system directory `system_dir`, without regard to case. Another directory or another name,
/// such as `OpenConsole.exe`, is not.
pub fn is_console_host(image: &str, system_dir: &str) -> bool {
    let host = format!("{}\\{CONSOLE_HOST}", system_dir.trim_end_matches('\\'));
    image.to_lowercase() == host.to_lowercase()
}

/// The number of the live members of the execution among `members`: the console host is none.
pub fn execution_members(members: &[Member]) -> u32 {
    let count = members.iter().filter(|member| !member.console_host).count();
    u32::try_from(count).unwrap_or(u32::MAX)
}

/// What the runtime sees of the recorded leader of an execution.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LeaderSeen {
    /// The process with the recorded pid and creation time runs; `in_job` says whether it is a
    /// member of the job of the execution.
    Alive { in_job: bool },
    /// The recorded process has ended with `code`; `held` when this runtime held a handle to it
    /// since it ran, which makes the code its exit evidence.
    Exited { code: u32, held: bool },
    /// No process has the recorded pid.
    Absent,
    /// A process with another creation time has the recorded pid: it is never the execution.
    PidReused,
    /// The OS gave no answer.
    Unknown(String),
}

impl LeaderSeen {
    /// The verdict on the leader, as an observation records it.
    pub const fn as_str(&self) -> &'static str {
        match self {
            Self::Alive { .. } => "leader-alive",
            Self::Exited { .. } => "leader-exited",
            Self::Absent => "leader-absent",
            Self::PidReused => "leader-pid-reused",
            Self::Unknown(_) => "leader-unknown",
        }
    }
}

/// What the runtime sees of the job object of an execution, opened by its name.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum JobSeen {
    /// No object has the name: no handle to the job is open anywhere.
    NameAbsent,
    /// The job exists and holds this many live members of the execution (`execution_members`).
    Members(u32),
    /// The OS gave no answer.
    Unknown(String),
}

impl JobSeen {
    /// The state of the job, as an observation records it.
    pub const fn as_str(&self) -> &'static str {
        match self {
            Self::NameAbsent => "name-absent",
            Self::Members(0) => "empty",
            Self::Members(_) => "active",
            Self::Unknown(_) => "unknown",
        }
    }
}

/// What an observation means for the execution.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Outcome {
    Running,
    /// The execution ended with this exit code of its leader.
    Exited(u32),
    /// The execution is gone without exit evidence.
    Lost,
    /// Nothing can be proven; the reason.
    Unknown(String),
}

impl Outcome {
    /// The state of the execution the outcome means.
    pub const fn state(&self) -> ExecutionState {
        match self {
            Self::Running => ExecutionState::Running,
            Self::Exited(_) => ExecutionState::Exited,
            Self::Lost => ExecutionState::Lost,
            Self::Unknown(_) => ExecutionState::Unknown,
        }
    }
}

/// The outcome of an observation of the leader and the job of an execution.
pub fn outcome(leader: &LeaderSeen, job: &JobSeen) -> Outcome {
    match (leader, job) {
        (LeaderSeen::Unknown(reason), _) | (_, JobSeen::Unknown(reason)) => {
            Outcome::Unknown(reason.clone())
        }
        (LeaderSeen::Alive { in_job: true }, JobSeen::Members(members)) if *members > 0 => {
            Outcome::Running
        }
        (LeaderSeen::Alive { .. }, _) => Outcome::Unknown(
            "the leader runs, but not as a member of the job object of the execution".to_owned(),
        ),
        (_, JobSeen::Members(members)) if *members > 0 => Outcome::Running,
        (LeaderSeen::Exited { code, held: true }, _) => Outcome::Exited(*code),
        (_, JobSeen::Members(_) | JobSeen::NameAbsent) => Outcome::Lost,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const GONE: [LeaderSeen; 4] = [
        LeaderSeen::Absent,
        LeaderSeen::PidReused,
        LeaderSeen::Exited {
            code: 3,
            held: false,
        },
        LeaderSeen::Exited {
            code: 3,
            held: true,
        },
    ];

    #[test]
    fn the_name_is_derived_from_the_operation_id_in_the_global_namespace() {
        let id = "ab".repeat(32);

        let name = name(&id);

        assert_eq!(name, format!(r"Global\harness-runtime-execution-{id}"));
        assert_eq!(name.matches('\\').count(), 1);
        assert!(name.len() < 260);
    }

    #[test]
    fn a_leader_that_runs_in_its_job_is_running_and_outside_it_unknown() {
        assert_eq!(
            outcome(&LeaderSeen::Alive { in_job: true }, &JobSeen::Members(2)),
            Outcome::Running
        );
        for job in [
            JobSeen::Members(2),
            JobSeen::Members(0),
            JobSeen::NameAbsent,
        ] {
            assert!(matches!(
                outcome(&LeaderSeen::Alive { in_job: false }, &job),
                Outcome::Unknown(_)
            ));
        }
        assert!(matches!(
            outcome(&LeaderSeen::Alive { in_job: true }, &JobSeen::NameAbsent),
            Outcome::Unknown(_)
        ));
    }

    #[test]
    fn members_that_outlive_their_leader_keep_the_execution_running() {
        for leader in GONE {
            assert_eq!(
                outcome(&leader, &JobSeen::Members(1)),
                Outcome::Running,
                "{leader:?}"
            );
        }
    }

    #[test]
    fn an_empty_job_is_exited_only_with_the_code_of_a_held_leader() {
        assert_eq!(
            outcome(
                &LeaderSeen::Exited {
                    code: 259,
                    held: true
                },
                &JobSeen::Members(0)
            ),
            Outcome::Exited(259)
        );
        for leader in [
            LeaderSeen::Absent,
            LeaderSeen::PidReused,
            LeaderSeen::Exited {
                code: 3,
                held: false,
            },
        ] {
            assert_eq!(
                outcome(&leader, &JobSeen::Members(0)),
                Outcome::Lost,
                "{leader:?}"
            );
            assert_eq!(
                outcome(&leader, &JobSeen::NameAbsent),
                Outcome::Lost,
                "{leader:?}"
            );
        }
    }

    const SYSTEM: &str = r"C:\WINDOWS\system32";

    fn member(pid: u32, console_host: bool) -> Member {
        Member { pid, console_host }
    }

    #[test]
    fn the_console_host_is_conhost_of_the_system_directory_in_any_case() {
        assert!(is_console_host(r"C:\Windows\System32\conhost.exe", SYSTEM));
        assert!(is_console_host(r"c:\windows\system32\CONHOST.EXE", SYSTEM));
        assert!(is_console_host(
            r"C:\Windows\System32\conhost.exe",
            r"C:\Windows\System32\"
        ));
        for image in [
            r"C:\Windows\SysWOW64\conhost.exe",
            r"D:\Windows\System32\conhost.exe",
            r"C:\Windows\System32\sub\conhost.exe",
            r"C:\tools\conhost.exe",
            r"C:\Windows\System32\OpenConsole.exe",
            r"C:\Windows\System32\conhost.exe.bak",
            r"C:\Windows\System32\xconhost.exe",
            r"C:\Python312\python.exe",
            "conhost.exe",
        ] {
            assert!(!is_console_host(image, SYSTEM), "{image}");
        }
    }

    #[test]
    fn the_console_host_is_never_a_member_of_the_execution() {
        assert_eq!(execution_members(&[]), 0);
        assert_eq!(execution_members(&[member(7, true)]), 0);
        assert_eq!(
            execution_members(&[member(7, true), member(8, false), member(9, false)]),
            2
        );
    }

    #[test]
    fn a_held_leader_that_ended_beside_a_live_console_host_is_exited() {
        let only_the_host = JobSeen::Members(execution_members(&[member(7, true)]));

        assert_eq!(
            outcome(
                &LeaderSeen::Exited {
                    code: 5,
                    held: true
                },
                &only_the_host
            ),
            Outcome::Exited(5)
        );
        for leader in [
            LeaderSeen::Absent,
            LeaderSeen::PidReused,
            LeaderSeen::Exited {
                code: 5,
                held: false,
            },
        ] {
            assert_eq!(
                outcome(&leader, &only_the_host),
                Outcome::Lost,
                "{leader:?}"
            );
        }
        let with_a_descendant =
            JobSeen::Members(execution_members(&[member(7, true), member(8, false)]));
        assert_eq!(
            outcome(&LeaderSeen::Absent, &with_a_descendant),
            Outcome::Running
        );
    }

    #[test]
    fn an_observation_that_failed_is_unknown() {
        let failed = JobSeen::Unknown("access denied".to_owned());
        for leader in GONE {
            assert_eq!(
                outcome(&leader, &failed),
                Outcome::Unknown("access denied".to_owned())
            );
        }
        assert_eq!(
            outcome(
                &LeaderSeen::Unknown("no answer".to_owned()),
                &JobSeen::Members(1)
            ),
            Outcome::Unknown("no answer".to_owned())
        );
        assert_eq!(
            Outcome::Unknown(String::new()).state(),
            ExecutionState::Unknown
        );
    }
}
