//! The command groups of a QA run, as the coordinator's policy sends them in `qa.run`.

use std::collections::HashSet;

use engine::argv::QaCommand;
use serde_json::Value;

/// When a group runs, from what the earlier groups did.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RunWhen {
    Always,
    /// Only when a command of an earlier group did not pass: the diagnostics of a failure.
    AfterFailure,
    /// Only when every command of the earlier groups passed.
    AfterSuccess,
}

impl RunWhen {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Always => "always",
            Self::AfterFailure => "after-failure",
            Self::AfterSuccess => "after-success",
        }
    }

    fn parse(text: &str) -> Option<Self> {
        [Self::Always, Self::AfterFailure, Self::AfterSuccess]
            .into_iter()
            .find(|when| when.as_str() == text)
    }
}

/// One approved command of a group.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Command {
    /// The stage id: unique in the run, and the last part of its container name.
    pub stage_id: String,
    pub command: QaCommand,
}

impl Command {
    /// The command as the request sent it: a text or a list of words.
    pub fn as_json(&self) -> Value {
        match &self.command {
            QaCommand::Shell(text) => Value::String(text.clone()),
            QaCommand::Argv(words) => Value::from(words.clone()),
        }
    }
}

/// One group of a run.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Group {
    pub group: String,
    pub run_when: RunWhen,
    pub stop_on_failure: bool,
    pub commands: Vec<Command>,
}

/// The groups of a run, in order.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RunPlan {
    pub groups: Vec<Group>,
}

/// Whether a stage id may name a container: lower-case letters, digits, `.`, `_` and `-`, at most
/// 63 characters, starting with a letter or digit; `net` and `proxy` name the run's own objects.
pub fn valid_stage_id(stage_id: &str) -> bool {
    let bytes = stage_id.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 63
        && (bytes[0].is_ascii_lowercase() || bytes[0].is_ascii_digit())
        && bytes.iter().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'.' | b'_' | b'-')
        })
        && !matches!(stage_id, "net" | "proxy")
}

fn command_of(value: &Value) -> Result<QaCommand, String> {
    match value {
        Value::String(text) if !text.trim().is_empty() => Ok(QaCommand::Shell(text.clone())),
        Value::Array(items) if !items.is_empty() => items
            .iter()
            .map(|item| {
                item.as_str()
                    .filter(|word| !word.is_empty())
                    .map(str::to_owned)
                    .ok_or_else(|| "a command list holds only non-empty strings".to_owned())
            })
            .collect::<Result<Vec<_>, _>>()
            .map(QaCommand::Argv),
        _ => Err("a command is a non-empty text or a non-empty list of words".to_owned()),
    }
}

impl RunPlan {
    /// The groups of the `groups` member of a `qa.run` payload.
    ///
    /// # Errors
    /// A malformed group, an unknown `run_when`, an unsafe or repeated stage id, an empty command,
    /// or a run without any command.
    pub fn parse(groups: &Value) -> Result<Self, String> {
        let items = groups.as_array().ok_or("groups is a list")?;
        let mut seen = HashSet::new();
        let mut parsed = Vec::new();
        for item in items {
            let group = item
                .get("group")
                .and_then(Value::as_str)
                .filter(|name| !name.is_empty())
                .ok_or("every group names itself")?;
            let when = item.get("run_when").and_then(Value::as_str).unwrap_or("");
            let run_when =
                RunWhen::parse(when).ok_or_else(|| format!("unknown run_when {when:?}"))?;
            let stop_on_failure = item
                .get("stop_on_failure")
                .and_then(Value::as_bool)
                .ok_or("every group says whether it stops on failure")?;
            let commands = item
                .get("commands")
                .and_then(Value::as_array)
                .ok_or("every group has a list of commands")?
                .iter()
                .map(|entry| {
                    let stage_id = entry
                        .get("stage_id")
                        .and_then(Value::as_str)
                        .unwrap_or_default();
                    if !valid_stage_id(stage_id) {
                        return Err(format!("the stage id {stage_id:?} cannot name a container"));
                    }
                    if !seen.insert(stage_id.to_owned()) {
                        return Err(format!("the stage id {stage_id} is repeated"));
                    }
                    Ok(Command {
                        stage_id: stage_id.to_owned(),
                        command: command_of(entry.get("command").unwrap_or(&Value::Null))?,
                    })
                })
                .collect::<Result<Vec<_>, String>>()?;
            parsed.push(Group {
                group: group.to_owned(),
                run_when,
                stop_on_failure,
                commands,
            });
        }
        if seen.is_empty() {
            return Err("a QA run has at least one command".to_owned());
        }
        Ok(Self { groups: parsed })
    }

    /// Every command of the run, in order, with its group.
    pub fn commands(&self) -> impl Iterator<Item = (&Group, &Command)> {
        self.groups
            .iter()
            .flat_map(|group| group.commands.iter().map(move |command| (group, command)))
    }
}
