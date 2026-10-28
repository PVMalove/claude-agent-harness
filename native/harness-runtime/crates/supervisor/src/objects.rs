//! `container.objects` and `container.remove` (ADR 0022, section 5): the containers and networks
//! the harness created for this repository, and their removal. The repository label is the
//! runtime's own Git common directory, never a request field. Every object is listed and
//! inspected again before a removal; an object of another repository, of an execution that is
//! not terminal, or a running worker (and the sidecar and network of its dispatch) is refused.
//! No volume is listed or removed.

use std::collections::BTreeSet;
use std::sync::Arc;

use contracts::{ErrorBody, ErrorCode, FailureDomain, Request};
use engine::Kind;
use engine::ops::{self, Object};
use rusqlite::Connection;
use serde_json::{Value, json};
use state::executions::{self, is_terminal};

use crate::Supervisor;
use crate::launch::sql;

fn malformed(detail: &str) -> ErrorBody {
    ErrorBody::new(
        ErrorCode::MalformedRequest,
        FailureDomain::Tooling,
        false,
        format!("the request is not valid: {detail}"),
        "send {\"engines\": [\"docker\"|\"podman\", ...]} or {\"objects\": [{\"engine\", \"id\"}, ...]}",
    )
}

fn unavailable(kind: Kind) -> ErrorBody {
    ErrorBody::new(
        ErrorCode::EngineUnavailable,
        FailureDomain::Infrastructure,
        true,
        format!("the {} engine did not list its objects", kind.as_str()),
        "start the engine and repeat; an engine that does not answer never means that nothing is there",
    )
}

fn kind_of(value: &Value) -> Result<Kind, ErrorBody> {
    value
        .as_str()
        .and_then(Kind::parse)
        .ok_or_else(|| malformed("an engine is docker or podman"))
}

fn running(object: &Object) -> bool {
    matches!(object.state.as_str(), "running" | "paused" | "restarting")
}

impl Supervisor {
    fn repository_label(&self) -> Result<String, ErrorBody> {
        self.repository
            .as_deref()
            .and_then(|path| path.to_str())
            .map(str::to_owned)
            .ok_or_else(|| {
                ErrorBody::new(
                    ErrorCode::NotFound,
                    FailureDomain::Policy,
                    false,
                    "this runtime knows no Git common directory of its repository",
                    "start the runtime with `harness-runtime serve --repo <repo>` of the repository",
                )
            })
    }

    /// The state of the execution an object names, if the store holds it.
    fn execution_state(
        connection: &Connection,
        object: &Object,
    ) -> Result<Option<String>, ErrorBody> {
        let Some(id) = object.execution.as_deref() else {
            return Ok(None);
        };
        Ok(executions::load(connection, id)?.map(|execution| execution.state.as_str().to_owned()))
    }

    /// This repository's objects in one engine.
    fn listed(&self, kind: Kind) -> Result<Vec<Object>, ErrorBody> {
        let repository = self.repository_label()?;
        ops::objects(self.engine(kind).as_ref(), &repository, None).ok_or_else(|| unavailable(kind))
    }

    /// `container.objects`: the objects of this repository in the engines of the request and of
    /// every container execution the store records.
    ///
    /// # Errors
    /// `malformed_request`; `engine_unavailable` when an engine does not list its objects.
    pub fn container_objects(self: &Arc<Self>, request: &Request) -> Result<Value, ErrorBody> {
        let asked = request
            .payload
            .as_ref()
            .and_then(|payload| payload.get("engines"))
            .and_then(Value::as_array)
            .ok_or_else(|| malformed("the payload is {\"engines\": [...]}"))?;
        let mut kinds: BTreeSet<Kind> = asked.iter().map(kind_of).collect::<Result<_, _>>()?;
        let connection = executions::connect(self.state()?)?;
        let mut statement =
            sql(connection
                .prepare("SELECT DISTINCT engine FROM executions WHERE engine IS NOT NULL"))?;
        let recorded: Vec<String> = sql(sql(statement.query_map([], |row| row.get(0)))?.collect())?;
        kinds.extend(recorded.iter().filter_map(|name| Kind::parse(name)));
        let mut objects = Vec::new();
        for kind in kinds {
            for object in self.listed(kind)? {
                objects.push(json!({
                    "engine": kind.as_str(),
                    "kind": object.kind.as_str(),
                    "id": object.id,
                    "name": object.name,
                    "dispatch": object.dispatch,
                    "role": object.role,
                    "state": object.state,
                    "execution": object.execution,
                    "execution_state": Self::execution_state(&connection, &object)?,
                }));
            }
        }
        Ok(json!({"objects": objects}))
    }

    /// `container.remove`: remove the named objects of this repository, worker, sidecar and
    /// network in that order within a dispatch, after listing and inspecting them again.
    ///
    /// # Errors
    /// `malformed_request`.
    pub fn container_remove(self: &Arc<Self>, request: &Request) -> Result<Value, ErrorBody> {
        let asked = request
            .payload
            .as_ref()
            .and_then(|payload| payload.get("objects"))
            .and_then(Value::as_array)
            .ok_or_else(|| malformed("the payload is {\"objects\": [...]}"))?;
        let mut wanted: Vec<(Kind, String)> = Vec::new();
        for item in asked {
            let id = item
                .get("id")
                .and_then(Value::as_str)
                .filter(|id| !id.is_empty())
                .ok_or_else(|| malformed("every object names its id"))?;
            wanted.push((
                kind_of(item.get("engine").unwrap_or(&Value::Null))?,
                id.to_owned(),
            ));
        }
        let connection = executions::connect(self.state()?)?;
        let (mut removed, mut missing, mut failed) = (Vec::new(), Vec::new(), Vec::new());
        let kinds: BTreeSet<Kind> = wanted.iter().map(|(kind, _)| *kind).collect();
        for kind in kinds {
            let engine = self.engine(kind);
            let ids: Vec<&String> = wanted
                .iter()
                .filter(|(k, _)| *k == kind)
                .map(|(_, id)| id)
                .collect();
            let own = match self.listed(kind) {
                Ok(own) => own,
                Err(err) => {
                    failed.extend(
                        ids.iter()
                            .map(|id| json!({"object": id, "reason": err.message})),
                    );
                    continue;
                }
            };
            let mut removable = Vec::new();
            for id in ids {
                let Some(object) = own.iter().find(|object| object.id == *id) else {
                    match ops::exists(engine.as_ref(), id) {
                        Some(false) => missing.push(json!(id)),
                        Some(true) => failed.push(json!({"object": id,
                            "reason": "the object is not one this repository's harness created"})),
                        None => failed.push(json!({"object": id,
                            "reason": format!("the {} engine did not answer", kind.as_str())})),
                    }
                    continue;
                };
                let label = format!("{} {}", object.kind.as_str(), object.name);
                let live = Self::execution_state(&connection, object)?.filter(|state| {
                    executions::parse_state(state).is_some_and(|state| !is_terminal(state))
                });
                let worker_runs = own.iter().any(|other| {
                    other.dispatch == object.dispatch && other.role == "worker" && running(other)
                });
                let refusal = if let Some(state) = live {
                    Some(format!("its execution is {state}"))
                } else if worker_runs {
                    Some("the worker of its dispatch is running".to_owned())
                } else {
                    None
                };
                match refusal {
                    Some(reason) => failed.push(json!({"object": label, "reason": reason})),
                    None => removable.push(object.clone()),
                }
            }
            let removal = ops::remove(engine.as_ref(), &removable);
            removed.extend(removal.removed);
            failed.extend(
                removal
                    .failed
                    .into_iter()
                    .map(|(object, reason)| json!({"object": object, "reason": reason})),
            );
            missing.extend(removal.missing.into_iter().map(Value::String));
        }
        Ok(json!({"removed": removed, "missing": missing, "failed": failed}))
    }
}
