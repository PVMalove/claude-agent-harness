//! A scripted engine for tests: it answers by argv prefix and records every argv.

use std::sync::{Mutex, PoisonError};
use std::time::Duration;

use crate::{Answer, Engine, Kind};

/// An engine whose answers a test sets by argv prefix; the longest matching prefix answers.
///
/// A command that no prefix matches does not answer, like a missing CLI. A command whose
/// `argv[0]` is not the engine's binary is recorded and refused the same way.
#[derive(Debug)]
pub struct Scripted {
    kind: Kind,
    rules: Mutex<Vec<(Vec<String>, Answer)>>,
    calls: Mutex<Vec<Vec<String>>>,
}

impl Scripted {
    /// A scripted engine of `kind` with no answers yet.
    pub fn new(kind: Kind) -> Self {
        Self {
            kind,
            rules: Mutex::new(Vec::new()),
            calls: Mutex::new(Vec::new()),
        }
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
}
