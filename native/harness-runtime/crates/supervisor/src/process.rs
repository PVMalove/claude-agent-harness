//! The Linux processes of the supervisor: the spawn in an own process group with the labels, the
//! identity read from `/proc/<pid>/stat`, and the scan of `/proc/*/environ` that finds a labelled
//! group leader again after a restart (ADR 0022, sections 2 and 3).
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

use state::identity::{Identity, Observer};

/// The label that names the operation id of a launch in the environment of its process.
pub const LABEL_OPERATION: &str = "HARNESS_OPERATION_ID";
/// The label that names the dispatch of a launch in the environment of its process.
pub const LABEL_DISPATCH: &str = "HARNESS_DISPATCH_ID";

/// What to start: the stored launch spec and the labels of the execution.
#[derive(Debug)]
pub struct Spec<'a> {
    pub argv: &'a [String],
    pub cwd: &'a Path,
    pub operation_id: &'a str,
    pub dispatch_id: &'a str,
    pub stdout: &'a Path,
    pub stderr: &'a Path,
}

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

#[cfg(test)]
mod tests {
    use super::*;

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
