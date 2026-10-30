//! The Linux processes of the supervisor: the spawn in an own process group with the labels, the
//! identity read from `/proc/<pid>/stat`, the scan of `/proc/*/environ` that finds a labelled
//! group leader again after a restart, and the passes over `/proc/*/stat` that tell whether a
//! process group has a member that is no zombie (ADR 0022, sections 2 and 3).
//!
//! A child is spawned with `process_group(0)`: it leads a new process group, whose id is its pid,
//! in the session of the runtime. `std::process::Child` never kills on drop, so a runtime that
//! ends leaves its executions running. The identity is the one of `state::identity`: host, boot,
//! PID namespace, pid and start time (field 22); a pid alone is never evidence.

use std::fs::{self, OpenOptions};
use std::io;
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Child, Command, Stdio};

use nix::errno::Errno;
use state::identity::{Identity, Observer};

pub use crate::launch::{LABEL_DISPATCH, LABEL_OPERATION, Spec};

/// The fields of `/proc/<pid>/stat` the supervisor reads: the state (field 3), the process
/// group (field 5) and the start time (field 22). The command name in parentheses may hold
/// spaces and `)`, so the fields are counted after the last `)`.
pub fn parse_stat(line: &str) -> Option<(char, i64, String)> {
    let rest = &line[line.rfind(')')? + 1..];
    let fields: Vec<&str> = rest.split_whitespace().collect();
    let state = fields.first()?.chars().next()?;
    let pgid = fields.get(2)?.parse::<i64>().ok()?;
    let start_time = *fields.get(19)?;
    start_time.parse::<u64>().ok()?;
    Some((state, pgid, start_time.to_owned()))
}

/// The identity and the process group of the process that holds `pid` now, a zombie included,
/// as `observer` sees it; `None` when `/proc` has no readable entry for it.
pub fn identity_of(pid: i64, observer: &Observer) -> Option<(Identity, i64)> {
    let line = fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let (_, pgid, start_time) = parse_stat(&line)?;
    Some((
        Identity {
            host: observer.host.clone(),
            boot_id: observer.boot_id.clone(),
            pid_namespace: observer.pid_namespace.clone(),
            pid,
            start_time,
        },
        pgid,
    ))
}

fn log_file(path: &Path) -> io::Result<fs::File> {
    OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .open(path)
}

/// Start `spec` as the leader of a new process group, with the null device as stdin, its output
/// appended to the log files, and the environment of the runtime plus the two labels.
///
/// # Errors
/// The error of the OS when it refuses the spawn or a log file: nothing started then.
pub fn spawn(spec: &Spec<'_>) -> io::Result<Child> {
    let (program, arguments) = spec
        .argv
        .split_first()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "the argv is empty"))?;
    Command::new(program)
        .args(arguments)
        .current_dir(spec.cwd)
        .process_group(0)
        .stdin(Stdio::null())
        .stdout(log_file(spec.stdout)?)
        .stderr(log_file(spec.stderr)?)
        .env(LABEL_OPERATION, spec.operation_id)
        .env(LABEL_DISPATCH, spec.dispatch_id)
        .spawn()
}

/// Whether the NUL-separated environment `environ` carries `name=value`.
fn labelled(environ: &[u8], name: &str, value: &str) -> bool {
    let wanted = format!("{name}={value}");
    environ
        .split(|byte| *byte == 0)
        .any(|entry| entry == wanted.as_bytes())
}

/// The pid and start time of the process group leader whose environment carries the label of
/// `operation_id`. A labelled process that does not lead its group (a child of the execution) is
/// never chosen. Of several labelled leaders the one that started first is chosen: the launch
/// precedes every process it starts. A leader that started before `not_before` (a start time of
/// this boot, in clock ticks) is never chosen.
///
/// # Errors
/// `/proc` cannot be listed: the scan proves nothing.
pub fn scan(operation_id: &str, not_before: Option<u64>) -> io::Result<Option<(i64, String)>> {
    let mut first: Option<(u64, i64, String)> = None;
    for entry in fs::read_dir("/proc")? {
        let Ok(entry) = entry else { continue };
        let Some(pid) = entry
            .file_name()
            .to_str()
            .and_then(|name| name.parse::<i64>().ok())
        else {
            continue;
        };
        // A process that ended or belongs to another user is not the labelled one.
        let Ok(environ) = fs::read(entry.path().join("environ")) else {
            continue;
        };
        if !labelled(&environ, LABEL_OPERATION, operation_id) {
            continue;
        }
        let Some((state, pgid, start_time)) = fs::read_to_string(entry.path().join("stat"))
            .ok()
            .and_then(|line| parse_stat(&line))
        else {
            continue;
        };
        let Ok(started) = start_time.parse::<u64>() else {
            continue;
        };
        if pgid != pid
            || matches!(state, 'Z' | 'X')
            || not_before.is_some_and(|bound| started < bound)
        {
            continue;
        }
        if first
            .as_ref()
            .is_none_or(|(earliest, _, _)| started < *earliest)
        {
            first = Some((started, pid, start_time));
        }
    }
    Ok(first.map(|(_, pid, start_time)| (pid, start_time)))
}

/// What two passes over `/proc` show of the members of a process group.
#[derive(Debug, PartialEq, Eq)]
pub enum Members {
    /// A process of the group has a thread that is neither a zombie nor dead.
    Live,
    /// Two passes found no live member, on the procfs of the runtime's own PID namespace without
    /// `hidepid`: only zombies are left, or nothing.
    NoLiveMember,
    /// The passes prove nothing, for this reason.
    Unproven(String),
}

/// Whether a process or a thread in `state` (field 3 of its `stat`) has ended.
fn ended(state: char) -> bool {
    matches!(state, 'Z' | 'X')
}

/// Whether the error of a read in `/proc` means that the process or the thread is gone.
fn gone(err: &io::Error) -> bool {
    err.kind() == io::ErrorKind::NotFound || err.raw_os_error() == Some(Errno::ESRCH as i32)
}

/// The state and the process group of the process or thread whose `stat` is at `path`; `None`
/// when it is gone.
fn stat_at(path: &Path) -> Result<Option<(char, i64)>, String> {
    match fs::read_to_string(path) {
        Ok(line) => parse_stat(&line)
            .map(|(state, pgid, _)| Some((state, pgid)))
            .ok_or_else(|| format!("{} cannot be parsed", path.display())),
        Err(err) if gone(&err) => Ok(None),
        Err(err) => Err(format!("{} cannot be read: {err}", path.display())),
    }
}

/// Whether a thread of the process whose thread directory is `task` has not ended. A process
/// whose main thread ended shows `Z` in its own `stat` while another thread still runs.
fn thread_alive(task: &Path) -> Result<bool, String> {
    let threads = match fs::read_dir(task) {
        Ok(threads) => threads,
        Err(err) if gone(&err) => return Ok(false),
        Err(err) => return Err(format!("{} cannot be listed: {err}", task.display())),
    };
    for thread in threads {
        let thread = match thread {
            Ok(thread) => thread,
            Err(err) if gone(&err) => return Ok(false),
            Err(err) => return Err(format!("{} cannot be listed: {err}", task.display())),
        };
        if stat_at(&thread.path().join("stat"))?.is_some_and(|(state, _)| !ended(state)) {
            return Ok(true);
        }
    }
    Ok(false)
}

/// One pass over `/proc`: whether a process of the group `pgid` (field 5 of its `stat`) has a
/// thread that has not ended. A process that is gone during the pass is skipped.
fn pass(pgid: i64) -> Result<bool, String> {
    let listing = |err: io::Error| format!("/proc cannot be listed: {err}");
    for entry in fs::read_dir("/proc").map_err(listing)? {
        let entry = entry.map_err(listing)?;
        if entry
            .file_name()
            .to_str()
            .is_none_or(|name| name.parse::<i64>().is_err())
        {
            continue;
        }
        let Some((state, group)) = stat_at(&entry.path().join("stat"))? else {
            continue;
        };
        if group == pgid && (!ended(state) || thread_alive(&entry.path().join("task"))?) {
            return Ok(true);
        }
    }
    Ok(false)
}

/// Whether the last `proc` mount at `/proc` in the mount table `mounts` hides the processes of
/// other users (`hidepid` other than `0` or `off`); `true` when no such mount is listed.
fn hides_pids(mounts: &str) -> bool {
    let options = mounts.lines().rev().find_map(|line| {
        let fields: Vec<&str> = line.split_whitespace().collect();
        (fields.get(1) == Some(&"/proc") && fields.get(2) == Some(&"proc"))
            .then(|| fields.get(3).copied().unwrap_or_default())
    });
    options.is_none_or(|options| {
        options.split(',').any(|option| {
            option
                .strip_prefix("hidepid=")
                .is_some_and(|value| !matches!(value, "0" | "off"))
        })
    })
}

/// Why `/proc` cannot prove that a group has no live member, if it cannot: it numbers the
/// processes of another PID namespace, or it hides the processes of other users.
fn untrusted() -> Option<String> {
    match fs::read_link("/proc/self") {
        Ok(own) if own.to_str() == Some(std::process::id().to_string().as_str()) => {}
        Ok(own) => {
            return Some(format!(
                "/proc numbers the processes of another PID namespace: /proc/self is {}",
                own.display()
            ));
        }
        Err(err) => return Some(format!("/proc/self cannot be read: {err}")),
    }
    match fs::read_to_string("/proc/self/mounts") {
        Ok(mounts) if hides_pids(&mounts) => {
            Some("/proc is mounted with hidepid: the processes of other users are hidden".into())
        }
        Ok(_) => None,
        Err(err) => Some(format!("/proc/self/mounts cannot be read: {err}")),
    }
}

/// Whether a member of the process group `pgid` lives. `killpg` reaches a group whose members
/// are all zombies, so the group is empty only when two passes in a row find no member with a
/// thread that has not ended: a member that forks a child with a lower pid and exits during one
/// pass is seen by the other. Only the procfs of the runtime's own PID namespace without
/// `hidepid`, read without an error, proves that; anything else is `Unproven`.
pub fn members(pgid: i64) -> Members {
    for _ in 0..2 {
        match pass(pgid) {
            Ok(false) => {}
            Ok(true) => return Members::Live,
            Err(reason) => return Members::Unproven(reason),
        }
    }
    untrusted().map_or(Members::NoLiveMember, Members::Unproven)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hidepid_on_the_last_proc_mount_hides_the_processes_of_other_users() {
        let mount = |options: &str| format!("proc /proc proc {options} 0 0\n");

        assert!(hides_pids(&mount("rw,nosuid,hidepid=2")));
        assert!(hides_pids(&mount("rw,hidepid=invisible,gid=10")));
        assert!(!hides_pids(&mount("rw,hidepid=0")));
        assert!(!hides_pids(&mount("rw,hidepid=off")));
        assert!(!hides_pids(&mount("rw,nosuid,nodev,noexec,relatime")));
        // Of two stacked mounts at /proc, the last one is the one a path resolves to.
        let stacked = format!("{}{}", mount("rw,hidepid=2"), mount("rw,relatime"));
        assert!(!hides_pids(&stacked));
        let stacked = format!("{}{}", mount("rw,relatime"), mount("rw,hidepid=1"));
        assert!(hides_pids(&stacked));
        // A proc mount elsewhere, or a mount of another type at /proc, is not the one.
        assert!(hides_pids("proc /mnt/proc proc rw 0 0\n"));
        assert!(hides_pids("tmpfs /proc tmpfs rw 0 0\n"));
        assert!(hides_pids(""));
    }

    #[test]
    fn the_stat_fields_are_counted_after_the_last_parenthesis() {
        let line = "42 (a) b) c) S 1 42 42 0 -1 4194560 1 0 0 0 0 0 0 0 20 0 1 0 987 0 0";

        assert_eq!(parse_stat(line), Some(('S', 42, "987".to_owned())));
    }

    #[test]
    fn a_label_matches_a_whole_entry_only() {
        let environ = b"A=1\0HARNESS_OPERATION_ID=abc\0B=2\0";

        assert!(labelled(environ, LABEL_OPERATION, "abc"));
        assert!(!labelled(environ, LABEL_OPERATION, "ab"));
        assert!(!labelled(
            b"X_HARNESS_OPERATION_ID=abc\0",
            LABEL_OPERATION,
            "abc"
        ));
    }
}
