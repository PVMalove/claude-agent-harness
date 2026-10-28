//! The sequential runner of a QA run: one foreground container per command, its streams in two
//! files, the stop of a command at its deadline or on an explicit stop, and a record per command.

use std::fs::{self, File};
use std::io::{self, Read};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant};

use contracts::FailureDomain;
use engine::argv::{self, QaRun};
use engine::{Attached, Engine, Exit, ops};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::classify::{Ending, classify};
use crate::plan::{Command, Group, RunPlan, RunWhen};

/// The limits of every command of a run.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Limits {
    /// A command still running after this is stopped: its timeout.
    pub command_timeout: Duration,
    /// The grace `<engine> stop --time` gives a container before the engine kills it.
    pub stop_grace: Duration,
    /// How often the runner looks at the CLI and the stop flag.
    pub poll: Duration,
    /// How long the runner waits for the CLI to end after a stop or a kill of its container.
    pub end_wait: Duration,
}

/// Who stopped a command or the run.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StoppedBy {
    /// An explicit `qa.stop`.
    Command,
    /// The deadline of the command.
    Deadline,
}

impl StoppedBy {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Command => "command",
            Self::Deadline => "deadline",
        }
    }
}

/// One stream file of a command.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Stream {
    pub path: PathBuf,
    pub sha256: String,
    pub size: u64,
}

impl Stream {
    fn of(path: &Path) -> io::Result<Self> {
        let mut file = File::open(path)?;
        let mut hasher = Sha256::new();
        let mut buffer = [0_u8; 64 * 1024];
        let mut size = 0_u64;
        loop {
            let read = file.read(&mut buffer)?;
            if read == 0 {
                break;
            }
            hasher.update(&buffer[..read]);
            size += u64::try_from(read).unwrap_or(u64::MAX);
        }
        Ok(Self {
            path: path.to_path_buf(),
            sha256: hasher
                .finalize()
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect(),
            size,
        })
    }

    fn to_json(&self) -> Value {
        json!({"path": self.path, "sha256": self.sha256, "size": self.size})
    }
}

/// What one command of the run did, or that it never ran.
#[derive(Clone, Debug, PartialEq)]
pub struct Record {
    pub index: usize,
    pub group: String,
    pub stage_id: String,
    pub command: Value,
    /// `ran` or `not_run`.
    pub result: &'static str,
    /// The real exit code; `None` for a CLI that did not start or a command never run.
    pub exit_code: Option<i32>,
    /// `None` when the code is the command's own result.
    pub domain: Option<FailureDomain>,
    pub stopped_by: Option<StoppedBy>,
    pub stdout: Option<Stream>,
    pub stderr: Option<Stream>,
    pub duration_ms: u64,
    /// What the stop and the kill of its container answered.
    pub stop: Option<Value>,
    /// Why the CLI did not start, could not be waited for, or a stream could not be read.
    pub detail: Option<String>,
    /// Where the command may write its JUnit report (`HARNESS_STAGE_JUNIT_XML`).
    pub junit: Option<PathBuf>,
}

impl Record {
    /// The record of a command the run never reached.
    pub fn not_run(index: usize, group: &Group, command: &Command) -> Self {
        Self {
            index,
            group: group.group.clone(),
            stage_id: command.stage_id.clone(),
            command: command.as_json(),
            result: "not_run",
            exit_code: None,
            domain: None,
            stopped_by: None,
            stdout: None,
            stderr: None,
            duration_ms: 0,
            stop: None,
            detail: None,
            junit: None,
        }
    }

    /// Whether the command ran and passed on its own.
    pub fn passed(&self) -> bool {
        self.result == "ran" && self.domain.is_none() && self.exit_code == Some(0)
    }

    pub fn to_json(&self) -> Value {
        json!({
            "index": self.index,
            "group": self.group,
            "stage_id": self.stage_id,
            "command": self.command,
            "result": self.result,
            "exit_code": self.exit_code,
            "domain": self.domain.map(FailureDomain::as_str),
            "stopped_by": self.stopped_by.map(StoppedBy::as_str),
            "stdout": self.stdout.as_ref().map(Stream::to_json),
            "stderr": self.stderr.as_ref().map(Stream::to_json),
            "duration_ms": self.duration_ms,
            "stop": self.stop,
            "detail": self.detail,
            "junit": self.junit,
        })
    }
}

/// What a run did: every command of its plan, in order, and who stopped it.
#[derive(Clone, Debug, PartialEq)]
pub struct Outcome {
    pub commands: Vec<Record>,
    /// `command` after an explicit stop; `deadline` when a command reached its deadline.
    pub stopped_by: Option<StoppedBy>,
}

impl Outcome {
    pub fn to_json(&self) -> Value {
        json!({
            "commands": self.commands.iter().map(Record::to_json).collect::<Vec<_>>(),
            "stopped_by": self.stopped_by.map(StoppedBy::as_str),
        })
    }
}

/// The record file of the command at `index`.
pub fn record_path(directory: &Path, index: usize) -> PathBuf {
    directory.join(format!("{index:03}.json"))
}

/// The commands of a run a runtime that died left behind: the record of every command that ran,
/// and `not_run` for every other one.
pub fn recover(plan: &RunPlan, directory: &Path) -> Vec<Value> {
    plan.commands()
        .enumerate()
        .map(|(index, (group, command))| {
            fs::read_to_string(record_path(directory, index))
                .ok()
                .and_then(|text| serde_json::from_str::<Value>(&text).ok())
                .unwrap_or_else(|| Record::not_run(index, group, command).to_json())
        })
        .collect()
}

/// The variable that names where a command may write its JUnit report (ADR 0023, section 4).
pub const JUNIT_ENV: &str = "HARNESS_STAGE_JUNIT_XML";

/// A runner of one QA run: its engine, the argv settings every command shares, the directory of
/// its stream files and records, its limits, and the directory of the JUnit report of each
/// command, if the run names one.
pub struct Runner<'a> {
    pub engine: &'a dyn Engine,
    pub run: &'a QaRun<'a>,
    pub directory: &'a Path,
    pub limits: Limits,
    pub junit_dir: Option<&'a Path>,
}

fn write_record(path: &Path, record: &Value) -> io::Result<()> {
    let partial = path.with_extension("json.partial");
    fs::write(&partial, record.to_string())?;
    fs::rename(&partial, path)
}

fn millis(elapsed: Duration) -> u64 {
    u64::try_from(elapsed.as_millis()).unwrap_or(u64::MAX)
}

impl Runner<'_> {
    /// Run the plan in order and call `on_command` after each command that ran; `stop` is the
    /// explicit stop of the run. A command is never repeated.
    pub fn run(
        &self,
        plan: &RunPlan,
        stop: &AtomicBool,
        mut on_command: impl FnMut(&Record),
    ) -> Outcome {
        let mut commands = Vec::new();
        let mut failed = false;
        let mut halted = false;
        let mut deadline = false;
        let mut index = 0;
        for group in &plan.groups {
            let runs = match group.run_when {
                RunWhen::Always => true,
                RunWhen::AfterFailure => failed,
                RunWhen::AfterSuccess => !failed,
            };
            let mut group_stopped = false;
            for command in &group.commands {
                halted |= stop.load(Ordering::SeqCst);
                if halted || !runs || group_stopped {
                    commands.push(Record::not_run(index, group, command));
                    index += 1;
                    continue;
                }
                let record = self.execute(index, group, command, stop);
                on_command(&record);
                if !record.passed() {
                    failed = true;
                    group_stopped = group.stop_on_failure;
                }
                halted |= record.stopped_by == Some(StoppedBy::Command);
                deadline |= record.stopped_by == Some(StoppedBy::Deadline);
                commands.push(record);
                index += 1;
            }
        }
        let stopped_by = if halted {
            Some(StoppedBy::Command)
        } else {
            deadline.then_some(StoppedBy::Deadline)
        };
        Outcome {
            commands,
            stopped_by,
        }
    }

    /// The CLI's end within `end_wait`, or `None`.
    fn wait_end(&self, attached: &mut dyn Attached) -> Option<Exit> {
        let started = Instant::now();
        loop {
            match attached.try_wait() {
                Ok(Some(exit)) => return Some(exit),
                Err(_) => return None,
                Ok(None) if started.elapsed() >= self.limits.end_wait => return None,
                Ok(None) => thread::sleep(self.limits.poll),
            }
        }
    }

    /// Stop the container with its grace, then kill it, then kill the CLI itself; never leave the
    /// CLI unreaped.
    fn stop_container(&self, name: &str, attached: &mut dyn Attached) -> (Value, Exit) {
        let (stopped, detail) = ops::stop(self.engine, name, self.limits.stop_grace);
        let mut evidence = json!({"stop": {"ok": stopped, "detail": detail}});
        if let Some(exit) = self.wait_end(attached) {
            return (evidence, exit);
        }
        let (killed, detail) = ops::kill(self.engine, name);
        evidence["kill"] = json!({"ok": killed, "detail": detail});
        if let Some(exit) = self.wait_end(attached) {
            return (evidence, exit);
        }
        attached.kill();
        evidence["cli_killed"] = json!(true);
        let exit = attached
            .try_wait()
            .ok()
            .flatten()
            .unwrap_or(Exit { code: None });
        (evidence, exit)
    }

    fn execute(&self, index: usize, group: &Group, command: &Command, stop: &AtomicBool) -> Record {
        let mut record = Record {
            result: "ran",
            ..Record::not_run(index, group, command)
        };
        let name = format!("{}-{}", self.run.base, command.stage_id);
        let out = self
            .directory
            .join(format!("{}.stdout.log", command.stage_id));
        let err = self
            .directory
            .join(format!("{}.stderr.log", command.stage_id));
        let started = Instant::now();
        let files = fs::create_dir_all(self.directory)
            .and_then(|()| File::create(&out))
            .and_then(|stdout| File::create(&err).map(|stderr| (stdout, stderr)));
        let spawned = files
            .map_err(|err| err.to_string())
            .and_then(|(stdout, stderr)| {
                let argv = argv::qa_run_argv(self.run, &command.stage_id, &command.command);
                self.engine.spawn(&argv, stdout, stderr)
            });
        let mut ending = Ending {
            code: None,
            deadline: false,
            stopped: false,
        };
        match spawned {
            Err(detail) => record.detail = Some(detail),
            Ok(mut attached) => loop {
                match attached.try_wait() {
                    Ok(Some(exit)) => {
                        ending.code = exit.code;
                        break;
                    }
                    Ok(None) => {}
                    Err(err) => {
                        record.detail = Some(format!("the engine CLI cannot be waited for: {err}"));
                        attached.kill();
                        break;
                    }
                }
                let by = if stop.load(Ordering::SeqCst) {
                    Some(StoppedBy::Command)
                } else {
                    (started.elapsed() >= self.limits.command_timeout)
                        .then_some(StoppedBy::Deadline)
                };
                if let Some(by) = by {
                    let (evidence, exit) = self.stop_container(&name, attached.as_mut());
                    record.stop = Some(evidence);
                    record.stopped_by = Some(by);
                    ending.code = exit.code;
                    ending.deadline = by == StoppedBy::Deadline;
                    ending.stopped = by == StoppedBy::Command;
                    break;
                }
                thread::sleep(self.limits.poll);
            },
        }
        record.duration_ms = millis(started.elapsed());
        record.exit_code = ending.code;
        record.domain = classify(ending);
        for (path, slot) in [(&out, &mut record.stdout), (&err, &mut record.stderr)] {
            match Stream::of(path) {
                Ok(stream) => *slot = Some(stream),
                Err(error) if record.detail.is_none() => {
                    record.detail = Some(format!("{}: {error}", path.display()));
                }
                Err(_) => {}
            }
        }
        if let Err(error) = write_record(&record_path(self.directory, index), &record.to_json()) {
            let detail = format!("the record of the command was not written: {error}");
            record.detail = Some(
                record
                    .detail
                    .map_or(detail.clone(), |old| format!("{old}; {detail}")),
            );
        }
        record
    }
}
