//! The container executions of the supervisor (ADR 0022, section 4; ADR 0025).
//!
//! Step 1 of a launch writes nothing: the plan's Git common directory is the runtime's own, no
//! root exposes an engine socket or a harness runtime (`engine::policy`), every environment name
//! is set in the runtime's environment, the engine answers in a supported mode and the images
//! are the pinned ones. The intent records the planned objects; a failed staged launch removes
//! them newest first and the execution is `lost`, never relaunched. Reconcile inspects the
//! container with the engine of the row: an exit saves the logs and records the exit with its
//! evidence, and only then removes the objects of the execution; a worker past its deadline is
//! stopped with its grace, then killed. The result of the removal is `teardown.json` next to the
//! logs; reconcile repeats the removal of an exited execution until that record is complete, so
//! a runtime that dies between the exit and the removal loses neither. An answer whose labels
//! name another execution or repository changes nothing.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Arc, TryLockError};
use std::thread;
use std::time::Duration;

use contracts::{ErrorBody, ErrorCode, ExecutionState, FailureDomain, Request};
use engine::argv::Spec;
use engine::facts;
use engine::ops::{self, Inspection, ObjectKind, Refusal, WorkerState};
use engine::plan::{HostUser, LABEL_EXECUTION, LABEL_REPOSITORY, Labels, Plan};
use engine::policy::{self, Protected};
use engine::{Engine, Kind};
use rusqlite::Connection;
use serde_json::{Value, json};
use state::executions::{self, Change, Execution, KIND_CONTAINER, allowed};
use state::time::iso_now;

use crate::Supervisor;
use crate::launch::{guard, sql};

/// How long a stop waits for the engine to report a killed worker gone: 30 seconds.
const KILL_PROOF_POLLS: u32 = 300;
const KILL_PROOF_POLL: Duration = Duration::from_millis(100);

/// An approval binds the pinned digests, so a rebuilt image needs a new propose.
pub(crate) const CHANGED_IMAGE_REMEDY: &str = "restore the image with the pinned digest, or rebuild or pull the image, cancel this dispatch (dispatch cancel) and repeat dispatch propose: an approval is never reused for another image";

fn error(code: ErrorCode, domain: FailureDomain, message: String, remedy: &str) -> ErrorBody {
    ErrorBody::new(code, domain, false, message, remedy)
}

fn policy_denied(message: String, remedy: &str) -> ErrorBody {
    error(
        ErrorCode::PolicyDenied,
        FailureDomain::Policy,
        message,
        remedy,
    )
}

/// The host user of a rootful engine.
pub(crate) fn host_user() -> HostUser {
    HostUser {
        uid: nix::unistd::getuid().as_raw(),
        gid: nix::unistd::getgid().as_raw(),
    }
}

fn env_text(name: &str) -> Option<String> {
    std::env::var(name).ok().filter(|value| !value.is_empty())
}

/// What the runtime's environment says about the engine sockets.
pub(crate) fn protected_inputs(uid: u32, runtime: Vec<PathBuf>) -> Protected {
    Protected {
        uid,
        xdg_runtime_dir: env_text("XDG_RUNTIME_DIR")
            .map(PathBuf::from)
            .filter(|path| path.is_absolute()),
        docker_host: env_text("DOCKER_HOST"),
        container_host: env_text("CONTAINER_HOST"),
        runtime,
    }
}

/// The checks of a container plan the runtime makes before it asks any engine.
pub(crate) struct Checked {
    pub plan: Plan,
    pub repository: String,
    pub missing_env: Vec<String>,
}

fn image_blocker(observation: &ops::Observation) -> Option<&ops::Blocker> {
    observation
        .blockers
        .iter()
        .find(|blocker| blocker.refusal == Refusal::MissingImage)
}

impl Supervisor {
    /// The engine of a kind.
    pub(crate) fn engine(&self, kind: Kind) -> Arc<dyn Engine> {
        (self.engines)(kind)
    }

    /// The engine an `executions` row names.
    pub(crate) fn engine_of(&self, execution: &Execution) -> Option<Arc<dyn Engine>> {
        Kind::parse(execution.engine.as_deref()?).map(|kind| self.engine(kind))
    }

    /// Parse a plan and check its repository, its mounts and its environment names; no engine
    /// is asked.
    pub(crate) fn check_plan(&self, plan: &Value) -> Result<Checked, ErrorBody> {
        let parsed = Plan::parse(plan).map_err(|detail| {
            error(
                ErrorCode::MalformedRequest,
                FailureDomain::Tooling,
                format!("the container plan is not valid: {detail}"),
                "propose and approve a dispatch with a resolved docker runtime_access plan",
            )
        })?;
        let repository = self
            .repository
            .as_deref()
            .and_then(|path| path.to_str())
            .ok_or_else(|| {
                policy_denied(
                    "this runtime knows no Git common directory of its repository".to_owned(),
                    "start the runtime with `harness-runtime serve --repo <repo>` of the repository",
                )
            })?
            .to_owned();
        let planned = parsed
            .root("git_common")
            .map(|root| Path::new(root).canonicalize().ok());
        if planned.flatten().as_deref() != Some(Path::new(&repository)) {
            return Err(policy_denied(
                format!(
                    "the plan's git_common {:?} is not the Git common directory {repository} of this runtime",
                    parsed.root("git_common")
                ),
                "send the dispatch to the runtime of its own repository",
            ));
        }
        let protected = policy::protected_paths(&protected_inputs(
            host_user().uid,
            self.runtime_paths.clone(),
        ));
        let roots: Vec<String> = parsed.roots().into_iter().map(str::to_owned).collect();
        if let Err(denials) = policy::deny_mounts(&roots, &protected) {
            let reasons: Vec<&str> = denials
                .iter()
                .map(|denial| denial.reason.as_str())
                .collect();
            // Each denial names its own remedy; the same remedy is said once.
            let mut remedies: Vec<&str> = Vec::new();
            for denial in &denials {
                if !remedies.contains(&denial.remedy.as_str()) {
                    remedies.push(&denial.remedy);
                }
            }
            return Err(policy_denied(
                reasons.join("; "),
                &format!("{}; nothing was written", remedies.join("; ")),
            )
            .with_fact(
                "denied_roots",
                denials
                    .iter()
                    .map(|denial| denial.root.clone())
                    .collect::<Vec<_>>(),
            ));
        }
        // Only presence is read: a value never enters an answer or the evidence.
        let missing_env = parsed
            .environment
            .iter()
            .filter(|name| std::env::var_os(name).is_none())
            .cloned()
            .collect();
        Ok(Checked {
            plan: parsed,
            repository,
            missing_env,
        })
    }

    /// `container.observe`: the engine facts and image ids of a plan, read-only, with every
    /// blocker; a root the mount policy denies is refused before any engine command.
    ///
    /// # Errors
    /// `malformed_request`; `policy_denied` for the repository or a mount.
    pub fn container_observe(self: &Arc<Self>, request: &Request) -> Result<Value, ErrorBody> {
        let plan = request
            .payload
            .as_ref()
            .and_then(|payload| payload.get("plan"))
            .ok_or_else(|| {
                error(
                    ErrorCode::MalformedRequest,
                    FailureDomain::Tooling,
                    "the request is not valid: the payload is {\"plan\": <plan>}".to_owned(),
                    "send the resolved runtime_access plan as the payload",
                )
            })?;
        let checked = self.check_plan(plan)?;
        let engine = self.engine(checked.plan.engine);
        let observation = ops::observe(engine.as_ref(), &checked.plan);
        let mut blockers: Vec<Value> = Vec::new();
        if !checked.missing_env.is_empty() {
            let names = checked.missing_env.join(", ");
            blockers.push(json!({
                "reason": format!("the runtime environment has no variable named: {names}"),
                "remedy": format!("set each of {names} in the environment of harness-runtime serve and restart it, then repeat dispatch preflight"),
            }));
        }
        blockers.extend(
            observation
                .blockers
                .iter()
                .map(|blocker| json!({"reason": blocker.reason, "remedy": blocker.remedy})),
        );
        Ok(json!({
            "engine": observation.engine.as_str(),
            "engine_mode": observation.mode.as_str(),
            "selinux": observation.selinux,
            "environment_id": observation.environment_id,
            "worker_image_id": observation.worker_image_id,
            "proxy_image_id": observation.proxy_image_id,
            "blockers": blockers,
        }))
    }

    /// Step 1 of a container launch, read-only: the checks, then the engine and the images.
    pub(crate) fn admit_container(
        &self,
        plan: &Value,
        images: &Value,
    ) -> Result<(Checked, ops::Observation), ErrorBody> {
        let checked = self.check_plan(plan)?;
        if !checked.missing_env.is_empty() {
            return Err(policy_denied(
                format!(
                    "the runtime environment has no variable named: {}",
                    checked.missing_env.join(", ")
                ),
                "set each name in the environment of harness-runtime serve and restart it; nothing was written",
            ));
        }
        let engine = self.engine(checked.plan.engine);
        let observation = ops::observe(engine.as_ref(), &checked.plan);
        let reasons = |refusal: Refusal| -> Vec<&str> {
            observation
                .blockers
                .iter()
                .filter(|blocker| blocker.refusal == refusal)
                .map(|blocker| blocker.reason.as_str())
                .collect()
        };
        let unavailable = reasons(Refusal::Unavailable);
        if !unavailable.is_empty() {
            return Err(error(
                ErrorCode::EngineUnavailable,
                FailureDomain::Infrastructure,
                unavailable.join("; "),
                "start the engine of the plan and repeat the launch; nothing was written",
            ));
        }
        let unsupported = reasons(Refusal::Unsupported);
        if !unsupported.is_empty() {
            return Err(policy_denied(
                unsupported.join("; "),
                "run harness-runtime against a local engine in a supported mode; nothing was written",
            ));
        }
        let pinned = |key: &str| images.get(key).and_then(Value::as_str).map(str::to_owned);
        let current = [
            ("worker", observation.worker_image_id.clone()),
            ("proxy", observation.proxy_image_id.clone()),
        ];
        let changed: Vec<String> = current
            .iter()
            .filter(|(key, id)| pinned(key) != *id)
            .map(|(key, id)| {
                format!(
                    "the {key} image is {id:?}, the approval pinned {:?}",
                    pinned(key)
                )
            })
            .collect();
        if !changed.is_empty() || image_blocker(&observation).is_some() {
            return Err(error(
                ErrorCode::ApprovalMismatch,
                FailureDomain::Policy,
                changed.join("; "),
                CHANGED_IMAGE_REMEDY,
            ));
        }
        Ok((checked, observation))
    }

    /// The launch record of a container intent: what the receipt and reconcile need.
    pub(crate) fn container_launch(
        id: &str,
        dispatch_id: &str,
        spec: &Value,
        checked: &Checked,
        observation: &ops::Observation,
        logs: (&Path, &Path),
    ) -> Value {
        let objects: Vec<Value> = ops::planned_objects(&checked.plan, dispatch_id)
            .into_iter()
            .map(|(kind, name)| json!([kind.as_str(), name]))
            .collect();
        json!({
            "argv": spec["argv"],
            "cwd": spec["cwd"],
            "labels": {
                "harness.dispatch": dispatch_id,
                "harness.repository": checked.repository,
                "harness.plan_digest": checked.plan.plan_digest,
                "harness.operation": id,
                "harness.execution": id,
            },
            "engine": checked.plan.engine.as_str(),
            "engine_mode": observation.mode.as_str(),
            "selinux": observation.selinux,
            "environment_id": observation.environment_id,
            "images": {"worker": observation.worker_image_id, "proxy": observation.proxy_image_id},
            "objects": objects,
            "logs": {"stdout": logs.0, "stderr": logs.1},
        })
    }

    /// Step 3: create and start the objects; a failure is rolled back by the engine sequence.
    pub(crate) fn start_container(
        &self,
        execution: &Execution,
        checked: &Checked,
        observation: &ops::Observation,
    ) -> Result<ops::Launched, ErrorBody> {
        let engine = self.engine(checked.plan.engine);
        let labels = Labels {
            dispatch: execution.dispatch_id.clone(),
            repository: checked.repository.clone(),
            plan_digest: checked.plan.plan_digest.clone(),
            operation: execution.id.clone(),
            execution: execution.id.clone(),
        };
        let command: Vec<String> = execution.launch["argv"]
            .as_array()
            .map(|items| {
                items
                    .iter()
                    .filter_map(|i| i.as_str().map(str::to_owned))
                    .collect()
            })
            .unwrap_or_default();
        let worker_image_id = observation.worker_image_id.clone().unwrap_or_default();
        let spec = Spec {
            plan: &checked.plan,
            labels: &labels,
            command: &command,
            worker_image_id: &worker_image_id,
            proxy_image_id: observation.proxy_image_id.as_deref(),
            user: host_user(),
            mode: observation.mode,
            selinux: observation.selinux,
        };
        ops::launch(engine.as_ref(), &spec).map_err(|failure| {
            let detail = format!("{}; {}", failure.detail, failure.removals.join("; "));
            error(
                ErrorCode::EngineUnavailable,
                FailureDomain::Infrastructure,
                detail,
                "fix the engine error, for example a missing plan root or an adapter outside the plan's mounts; the execution is lost and a new launch needs a new dispatch",
            )
            .with_fact("stage", failure.stage)
            .with_fact(
                "objects",
                failure
                    .objects
                    .iter()
                    .map(|(kind, name)| format!("{} {name}", kind.as_str()))
                    .collect::<Vec<_>>(),
            )
            .with_fact("removals", failure.removals)
            .with_fact("execution_state", ExecutionState::Lost.as_str())
        })
    }

    /// Step 4, or the adoption of a labelled running worker: the container id, `running`, the
    /// receipt and the committed operation, in one transaction.
    pub(crate) fn container_receipt(
        &self,
        connection: &mut Connection,
        execution: &Execution,
        launched: &ops::Launched,
        adopted: bool,
    ) -> Result<Value, ErrorBody> {
        let at = iso_now();
        let tx = executions::begin(connection)?;
        let current = executions::load(&tx, &execution.id)?
            .filter(|current| current.container_id.is_none() && !executions::is_terminal(current.state))
            .ok_or_else(|| {
                error(
                    ErrorCode::StoreBusy,
                    FailureDomain::Infrastructure,
                    "the execution changed before its receipt".to_owned(),
                    "repeat the command; a launch is never repeated, a repeat answers what it stored",
                )
            })?;
        let receipt = json!({
            "execution_id": current.id,
            "dispatch_id": current.dispatch_id,
            "state": ExecutionState::Running.as_str(),
            "kind": KIND_CONTAINER,
            "engine": current.engine,
            "container_id": launched.worker_id,
            "proxy_id": launched.proxy_id,
            "environment_id": current.launch.get("environment_id").cloned().unwrap_or(Value::Null),
            "launched_at": at,
            "execution_deadline": current.execution_deadline,
            "logs": current.launch.get("logs").cloned().unwrap_or(Value::Null),
            "adopted": adopted,
        });
        executions::transition(
            &tx,
            &current,
            ExecutionState::Running,
            Change {
                container_id: Some(launched.worker_id.clone()),
                receipt: Some(receipt.clone()),
                lease_owner: adopted.then(crate::launch::owner),
                facts: json!({"adopted": adopted, "container_id": launched.worker_id}),
                ..Change::default()
            },
            &at,
        )?;
        sql(tx.execute(
            "UPDATE operations SET state = 'committed', result_json = ?2, completed_at = ?3
             WHERE operation_id = ?1 AND state = 'intent'",
            rusqlite::params![current.id, receipt.to_string(), at],
        ))?;
        sql(tx.commit())?;
        Ok(receipt)
    }

    /// The objects of this repository that carry the execution label of `execution`; `None`
    /// when the engine does not answer.
    pub(crate) fn own_objects(
        &self,
        engine: &dyn Engine,
        execution: &Execution,
    ) -> Option<Vec<ops::Object>> {
        let repository = self.repository.as_deref()?.to_str()?;
        let found = ops::objects(engine, repository, Some(&execution.dispatch_id))?;
        Some(
            found
                .into_iter()
                .filter(|object| object.execution.as_deref() == Some(execution.id.as_str()))
                .collect(),
        )
    }

    /// Save the worker's logs while its container is still there.
    fn save_logs(&self, engine: &dyn Engine, execution: &Execution, worker: &str) -> Value {
        let paths = self.log_paths(&execution.id);
        let saved = paths
            .first()
            .and_then(|stdout| stdout.parent())
            .map_or_else(
                || Err("the execution has no log directory".to_owned()),
                |dir| {
                    crate::launch::log_directory(dir)
                        .map_err(|err| err.to_string())
                        .and_then(|()| {
                            ops::logs(engine, worker, &paths[0], &paths[1])
                                .map_err(|err| err.to_string())
                        })
                },
            );
        let logs = crate::stop::logs(&paths);
        match saved {
            Ok(()) => logs,
            Err(reason) => json!({"files": logs, "error": reason}),
        }
    }

    /// Remove the objects of the execution. The removal is `complete` when the engine listed
    /// them and every one is removed or already absent; a repeat is safe.
    fn remove_objects(&self, engine: &dyn Engine, execution: &Execution) -> Value {
        self.own_objects(engine, execution).map_or_else(
            || {
                json!({
                    "complete": false,
                    "error": "the engine did not list the objects of the execution",
                })
            },
            |objects| {
                let removal = ops::remove(engine, &objects);
                json!({
                    "removed": removal.removed,
                    "missing": removal.missing,
                    "failed": removal.failed.iter()
                        .map(|(object, reason)| json!({"object": object, "reason": reason}))
                        .collect::<Vec<_>>(),
                    "complete": removal.failed.is_empty(),
                })
            },
        )
    }

    /// Where the result of the removal after the exit of `id` is kept: next to its logs.
    fn teardown_record(&self, id: &str) -> Option<PathBuf> {
        self.log_paths(id)
            .first()
            .and_then(|stdout| stdout.parent())
            .map(|dir| dir.join("teardown.json"))
    }

    fn teardown_complete(&self, id: &str) -> bool {
        self.teardown_record(id)
            .and_then(|path| fs::read_to_string(path).ok())
            .and_then(|text| serde_json::from_str::<Value>(&text).ok())
            .is_some_and(|record| record["complete"] == true)
    }

    /// Remove the objects of an exited execution, whose exit is stored, and record the result.
    pub(crate) fn remove_after_exit(&self, engine: &dyn Engine, execution: &Execution) {
        let mut teardown = self.remove_objects(engine, execution);
        teardown["at"] = json!(iso_now());
        let Some(path) = self.teardown_record(&execution.id) else {
            return;
        };
        let partial = path.with_extension("json.partial");
        let written = path
            .parent()
            .map_or(Ok(()), crate::launch::log_directory)
            .and_then(|()| fs::write(&partial, teardown.to_string()))
            .and_then(|()| fs::rename(&partial, &path));
        if let Err(err) = written {
            eprintln!(
                "harness-runtime: the removal after the exit of {} was not recorded: {err}",
                execution.id
            );
        }
    }

    /// Repeat the removal of every exited container execution or QA run without a complete
    /// record of it: a runtime that died between the exit and the removal, or an engine that
    /// refused it.
    pub(crate) fn finish_removals(self: &Arc<Self>, connection: &Connection) {
        let ids = connection
            .prepare(
                "SELECT id FROM executions WHERE kind IN (?1, ?2) AND state = 'exited' ORDER BY id",
            )
            .and_then(|mut statement| {
                let rows = statement.query_map([KIND_CONTAINER, executions::KIND_QA], |row| {
                    row.get::<_, String>(0)
                })?;
                rows.collect::<rusqlite::Result<Vec<String>>>()
            });
        let Ok(ids) = ids else {
            return;
        };
        for id in ids {
            if self.teardown_complete(&id) || self.is_watched(&id) {
                continue;
            }
            let lock = self.lock_of(&id);
            let _held = match lock.try_lock() {
                Ok(held) => held,
                Err(TryLockError::Poisoned(poisoned)) => poisoned.into_inner(),
                Err(TryLockError::WouldBlock) => continue,
            };
            let Ok(Some(execution)) = executions::load(connection, &id) else {
                continue;
            };
            if let Some(engine) = self.engine_of(&execution) {
                self.remove_after_exit(engine.as_ref(), &execution);
            }
        }
    }

    /// The exit evidence of an exited worker, with its saved logs; its objects are removed only
    /// after this evidence is stored.
    fn exit_of(
        &self,
        engine: &dyn Engine,
        execution: &Execution,
        seen: &Inspection,
        worker: &str,
        stopped_by: Option<&str>,
    ) -> Value {
        let logs = self.save_logs(engine, execution, worker);
        let mut exit = json!({
            "exit_code": seen.exit_code,
            "finished_at": seen.finished_at,
            "oom_killed": seen.oom_killed,
            "source": "engine-inspect",
            "ended_at": iso_now(),
            "logs": logs,
        });
        if let Some(by) = stopped_by {
            exit["stopped_by"] = json!(by);
        }
        exit
    }

    /// Whether an inspected answer belongs to `execution` of this repository.
    fn ours(&self, seen: &Inspection, execution: &Execution) -> bool {
        seen.label(LABEL_EXECUTION) == Some(execution.id.as_str())
            && seen.label(LABEL_REPOSITORY).map(Path::new) == self.repository.as_deref()
    }

    /// Reconcile one container execution whose mutex this thread holds.
    pub(crate) fn settle_container(
        self: &Arc<Self>,
        connection: &mut Connection,
        execution: &Execution,
    ) -> Result<(), ErrorBody> {
        let Some(engine) = self.engine_of(execution) else {
            return Self::to_unknown(connection, execution, "the execution names no known engine");
        };
        let Some(worker) = execution.container_id.clone() else {
            return self.settle_unfinished_launch(connection, execution, engine.as_ref());
        };
        let answer = engine.run(
            &[
                engine.kind().as_str().to_owned(),
                "info".to_owned(),
                "--format".to_owned(),
                "{{json .}}".to_owned(),
            ],
            engine::ENGINE_TIMEOUT,
        );
        let info = serde_json::from_str::<Value>(&answer.output()).unwrap_or(Value::Null);
        let environment = match engine.kind() {
            Kind::Docker => facts::docker_facts(&info),
            Kind::Podman => facts::podman_facts(&info),
        }
        .map(|found| found.environment_id);
        let recorded = execution
            .launch
            .get("environment_id")
            .and_then(Value::as_str);
        if !answer.ok() || environment.as_deref() != recorded {
            return Self::to_unknown(
                connection,
                execution,
                "the engine is another one or does not answer",
            );
        }
        let seen = ops::inspect(engine.as_ref(), &worker);
        match seen.state {
            WorkerState::Running if !self.ours(&seen, execution) => {
                Self::to_unknown(connection, execution, "the container carries other labels")
            }
            WorkerState::Running => {
                let due = execution.state == ExecutionState::Running
                    && crate::stop::past(&execution.execution_deadline);
                if due || execution.state == ExecutionState::Stopping {
                    self.stop_container(execution, engine, worker);
                    return Ok(());
                }
                Self::move_from(
                    connection,
                    execution,
                    ExecutionState::Running,
                    json!({"observed": "running"}),
                    None,
                )
            }
            WorkerState::Exited if self.ours(&seen, execution) => {
                let exit = self.exit_of(engine.as_ref(), execution, &seen, &worker, None);
                Self::move_from(
                    connection,
                    execution,
                    ExecutionState::Exited,
                    json!({"source": "engine-inspect"}),
                    Some(exit),
                )?;
                let stored = executions::load(connection, &execution.id)?;
                if stored.is_some_and(|stored| stored.state == ExecutionState::Exited) {
                    self.remove_after_exit(engine.as_ref(), execution);
                }
                Ok(())
            }
            WorkerState::Missing => {
                let teardown = self.remove_objects(engine.as_ref(), execution);
                Self::move_from(
                    connection,
                    execution,
                    ExecutionState::Lost,
                    json!({"reason": "the engine has no such container", "teardown": teardown}),
                    None,
                )
            }
            WorkerState::Exited | WorkerState::Unknown => Self::to_unknown(
                connection,
                execution,
                "the engine answer proves nothing about this execution",
            ),
        }
    }

    pub(crate) fn to_unknown(
        connection: &mut Connection,
        execution: &Execution,
        reason: &str,
    ) -> Result<(), ErrorBody> {
        Self::move_from(
            connection,
            execution,
            ExecutionState::Unknown,
            json!({"reason": reason}),
            None,
        )
    }

    /// One transition from the state the row was read in, when the matrix allows it.
    fn move_from(
        connection: &mut Connection,
        execution: &Execution,
        to: ExecutionState,
        facts: Value,
        exit: Option<Value>,
    ) -> Result<(), ErrorBody> {
        if to == execution.state || !allowed(execution.state, to) {
            return Ok(());
        }
        Self::move_to(
            connection,
            execution,
            to,
            Change {
                exit,
                observation: Some(json!({"observed_at": iso_now(), "facts": facts})),
                facts,
                ..Change::default()
            },
        )
    }

    /// An intent without a receipt: a runtime died during the launch. A running worker that
    /// carries the execution label is adopted; otherwise the labelled objects are removed newest
    /// first and the execution is lost, never relaunched.
    fn settle_unfinished_launch(
        self: &Arc<Self>,
        connection: &mut Connection,
        execution: &Execution,
        engine: &dyn Engine,
    ) -> Result<(), ErrorBody> {
        let Some(found) = self.own_objects(engine, execution) else {
            return Self::to_unknown(
                connection,
                execution,
                "the engine did not list the objects of the launch",
            );
        };
        let worker = found.iter().find(|object| object.role == "worker");
        if let Some(worker) =
            worker.filter(|w| matches!(w.state.as_str(), "running" | "paused" | "restarting"))
        {
            let launched = ops::Launched {
                worker_id: worker.id.clone(),
                proxy_id: found
                    .iter()
                    .find(|o| o.role == "proxy")
                    .map(|o| o.id.clone()),
            };
            self.container_receipt(connection, execution, &launched, true)?;
            return Ok(());
        }
        if let Some(worker) = worker {
            // The worker ran and ended before its receipt: keep what it wrote.
            let paths = self.log_paths(&execution.id);
            if let Some(dir) = paths.first().and_then(|path| path.parent())
                && crate::launch::log_directory(dir).is_ok()
            {
                let _ = ops::logs(engine, &worker.id, &paths[0], &paths[1]);
            }
        }
        // Creation order is network, sidecar, worker; the rollback goes newest first.
        let rank = |object: &&ops::Object| match object.role.as_str() {
            "network" => 0,
            "proxy" => 1,
            _ => 2,
        };
        let mut mine: Vec<&ops::Object> = found.iter().collect();
        mine.sort_by_key(rank);
        let planned: Vec<(ObjectKind, String)> = mine
            .iter()
            .map(|object| (object.kind, object.name.clone()))
            .collect();
        let removals = ops::rollback(engine, &planned);
        let reason = "no running labelled worker; never relaunched";
        let failure = error(
            ErrorCode::EngineUnavailable,
            FailureDomain::Infrastructure,
            format!("the launch of the execution {} left {reason}", execution.id),
            "the execution is lost: a new launch needs a new dispatch (ADR 0021, section 4)",
        )
        .with_fact("execution_state", ExecutionState::Lost.as_str());
        self.fail(
            connection,
            execution,
            &failure,
            json!({"reason": reason, "removals": removals}),
        )
    }

    /// Stop a worker past its deadline on its own thread: `stop --time <grace>`, then `kill`,
    /// then the exit with `stopped_by: deadline`. A stop that cannot prove its end is `unknown`.
    fn stop_container(
        self: &Arc<Self>,
        execution: &Execution,
        engine: Arc<dyn Engine>,
        worker: String,
    ) {
        if !guard(&self.inner.watched).insert(execution.id.clone()) {
            return;
        }
        let supervisor = Arc::clone(self);
        let execution = execution.clone();
        thread::spawn(move || {
            supervisor.drive_stop(&execution, engine.as_ref(), &worker);
            guard(&supervisor.inner.watched).remove(&execution.id);
        });
    }

    fn drive_stop(&self, execution: &Execution, engine: &dyn Engine, worker: &str) {
        let id = &execution.id;
        let mut from = execution.state;
        if from == ExecutionState::Running {
            let begun = Change {
                facts: json!({"stopped_by": "deadline", "term_sent_at": iso_now()}),
                ..Change::default()
            };
            self.record(id, from, ExecutionState::Stopping, &begun);
            from = ExecutionState::Stopping;
        }
        let grace = Duration::from_secs(u64::try_from(execution.stop_grace_seconds).unwrap_or(0));
        let (stopped, stop_detail) = ops::stop(engine, worker, grace);
        let mut seen = ops::inspect(engine, worker);
        let mut killed = None;
        if seen.state == WorkerState::Running {
            let (ok, detail) = ops::kill(engine, worker);
            killed = Some(json!({"ok": ok, "detail": detail}));
            // The engine reports the end of a killed worker shortly after the kill returns.
            for _ in 0..KILL_PROOF_POLLS {
                seen = ops::inspect(engine, worker);
                if seen.state != WorkerState::Running {
                    break;
                }
                thread::sleep(KILL_PROOF_POLL);
            }
        }
        let current = Execution {
            state: from,
            ..execution.clone()
        };
        if seen.state == WorkerState::Exited && self.ours(&seen, &current) {
            let mut exit = self.exit_of(engine, &current, &seen, worker, Some("deadline"));
            exit["stop"] = json!({"ok": stopped, "detail": stop_detail, "kill": killed});
            let change = Change {
                exit: Some(exit),
                facts: json!({"source": "engine-inspect", "stopped_by": "deadline"}),
                ..Change::default()
            };
            if self.record(id, from, ExecutionState::Exited, &change) {
                self.remove_after_exit(engine, &current);
            }
        } else {
            let change = Change {
                facts: json!({"reason": "the stop of the worker cannot be proven", "stop": stop_detail}),
                ..Change::default()
            };
            self.record(id, from, ExecutionState::Unknown, &change);
        }
    }
}
