//! A scripted engine for tests: it answers by argv prefix and records every argv.

use std::collections::HashMap;
use std::fs::File;
use std::io::{self, Write};
use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use crate::{Answer, Attached, Engine, Exit, Kind};

/// The exit code of a container the engine stopped: 128 + SIGTERM.
const STOPPED: i32 = 143;
/// The exit code of a container the engine killed: 128 + SIGKILL.
const KILLED: i32 = 137;

/// What an attached scripted command does: write both streams, then end with a code, or run
/// until the engine stops or kills its container by the name of its `--name`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Run {
    stdout: String,
    stderr: String,
    code: Option<i32>,
    /// Whether `stop` ends a hanging run; `kill` always does.
    stoppable: bool,
}

impl Run {
    /// A command that writes both streams and exits `code`.
    pub fn finish(stdout: impl Into<String>, stderr: impl Into<String>, code: i32) -> Self {
        Self {
            stdout: stdout.into(),
            stderr: stderr.into(),
            code: Some(code),
            stoppable: true,
        }
    }

    /// A command that writes both streams and runs until its container is stopped (143) or
    /// killed (137).
    pub fn until_stopped(stdout: impl Into<String>, stderr: impl Into<String>) -> Self {
        Self {
            stdout: stdout.into(),
            stderr: stderr.into(),
            code: None,
            stoppable: true,
        }
    }

    /// A command that ignores the stop of its container and ends only when it is killed.
    pub fn until_killed(stdout: impl Into<String>, stderr: impl Into<String>) -> Self {
        Self {
            stoppable: false,
            ..Self::until_stopped(stdout, stderr)
        }
    }
}

type Ended = Arc<Mutex<HashMap<String, i32>>>;

/// An attached scripted run.
struct Running {
    name: String,
    code: Option<i32>,
    stoppable: bool,
    ended: Ended,
    killed: bool,
}

impl Attached for Running {
    fn try_wait(&mut self) -> io::Result<Option<Exit>> {
        if self.killed {
            return Ok(Some(Exit { code: None }));
        }
        if let Some(code) = self.code {
            return Ok(Some(Exit { code: Some(code) }));
        }
        let ended = self.ended.lock().unwrap_or_else(PoisonError::into_inner);
        Ok(match ended.get(&self.name) {
            Some(&KILLED) => Some(Exit { code: Some(KILLED) }),
            Some(&code) if self.stoppable => Some(Exit { code: Some(code) }),
            _ => None,
        })
    }

    fn kill(&mut self) {
        if self.code.is_none() {
            self.killed = true;
        }
    }
}

/// An engine whose answers a test sets by argv prefix; the longest matching prefix answers.
///
/// A command that no prefix matches does not answer, like a missing CLI. A command whose
/// `argv[0]` is not the engine's binary is recorded and refused the same way.
#[derive(Debug)]
pub struct Scripted {
    kind: Kind,
    rules: Mutex<Vec<(Vec<String>, Answer)>>,
    runs: Mutex<Vec<(Vec<String>, Run)>>,
    calls: Mutex<Vec<Vec<String>>>,
    /// The containers `stop` or `kill` ended, by name or id, with their exit code.
    ended: Ended,
}

impl Scripted {
    /// A scripted engine of `kind` with no answers yet.
    pub fn new(kind: Kind) -> Self {
        Self {
            kind,
            rules: Mutex::new(Vec::new()),
            runs: Mutex::new(Vec::new()),
            calls: Mutex::new(Vec::new()),
            ended: Arc::default(),
        }
    }

    /// Run every attached command that starts with `prefix` as `run`; the longest prefix wins.
    pub fn attach(&self, prefix: &[&str], run: Run) -> &Self {
        let prefix: Vec<String> = prefix.iter().map(|part| (*part).to_owned()).collect();
        let mut runs = self.runs.lock().unwrap_or_else(PoisonError::into_inner);
        runs.retain(|(known, _)| *known != prefix);
        runs.push((prefix, run));
        self
    }

    /// Answer every command that starts with `prefix`; a rule for the same prefix is replaced.
    pub fn answer(&self, prefix: &[&str], answer: Answer) -> &Self {
        let prefix: Vec<String> = prefix.iter().map(|part| (*part).to_owned()).collect();
        let mut rules = self.rules.lock().unwrap_or_else(PoisonError::into_inner);
        rules.retain(|(known, _)| *known != prefix);
        rules.push((prefix, answer));
        self
    }

    /// Every argv run so far, in order.
    pub fn calls(&self) -> Vec<Vec<String>> {
        self.calls
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }

    /// Forget the argv run so far.
    pub fn clear(&self) {
        self.calls
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clear();
    }
}

impl Engine for Scripted {
    fn kind(&self) -> Kind {
        self.kind
    }

    fn run(&self, argv: &[String], _timeout: Duration) -> Answer {
        self.calls
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push(argv.to_vec());
        if argv.first().map(String::as_str) != Some(self.kind.as_str()) {
            return Answer::silent(format!("not a {} command: {argv:?}", self.kind.as_str()));
        }
        let code = match argv.get(1).map(String::as_str) {
            Some("stop") => Some(STOPPED),
            Some("kill") => Some(KILLED),
            _ => None,
        };
        if let (Some(code), Some(name)) = (code, argv.last()) {
            let mut ended = self.ended.lock().unwrap_or_else(PoisonError::into_inner);
            // A kill after a stop that did not end the run is what ends it.
            ended.insert(name.clone(), code);
        }
        let rules = self.rules.lock().unwrap_or_else(PoisonError::into_inner);
        rules
            .iter()
            .filter(|(prefix, _)| argv.starts_with(prefix))
            .max_by_key(|(prefix, _)| prefix.len())
            .map_or_else(
                || Answer::silent(format!("no scripted answer for {argv:?}")),
                |(_, answer)| answer.clone(),
            )
    }

    fn spawn(
        &self,
        argv: &[String],
        mut stdout: File,
        mut stderr: File,
    ) -> Result<Box<dyn Attached>, String> {
        self.calls
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push(argv.to_vec());
        if argv.first().map(String::as_str) != Some(self.kind.as_str()) {
            return Err(format!("not a {} command: {argv:?}", self.kind.as_str()));
        }
        let run = self
            .runs
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .iter()
            .filter(|(prefix, _)| argv.starts_with(prefix))
            .max_by_key(|(prefix, _)| prefix.len())
            .map(|(_, run)| run.clone())
            .ok_or_else(|| format!("no scripted run for {argv:?}"))?;
        stdout
            .write_all(run.stdout.as_bytes())
            .and_then(|()| stderr.write_all(run.stderr.as_bytes()))
            .map_err(|err| err.to_string())?;
        let name = argv
            .iter()
            .position(|part| part == "--name")
            .and_then(|at| argv.get(at + 1))
            .cloned()
            .unwrap_or_default();
        Ok(Box::new(Running {
            name,
            code: run.code,
            stoppable: run.stoppable,
            ended: Arc::clone(&self.ended),
            killed: false,
        }))
    }
}
