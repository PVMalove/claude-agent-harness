//! The QA runs of the supervisor (ADR 0022, section 3; ADR 0023, section 5).
//!
//! `qa.run` runs the command groups of one attempt of a QA dispatch in four steps. Step 1 writes
//! nothing: the dispatch is `working` at the attempt of the payload, its brief is a QA brief that
//! pins `qa_execution` and the images, the operation id is `sha256([dispatch_id, brief_sha256,
//! attempt])`, the QA lane is held by this dispatch with the fencing token of the request, no other
//! QA run is unfinished, every command is one the brief approved, the clean-room paths lie in the
//! pinned plan's shared storage, every environment name is set and the images are the pinned ones.
//! Step 2 is the intent (the operation and a `qa` execution of the attempt in `launching`); step 3
//! creates the sidecar and the internal network of the run once, rolled back newest first on a
//! failure, which makes the execution `lost`; step 4 is the receipt (`running`) and the start of a
//! thread that runs the commands, one foreground container each, and records the exit with the
//! evidence of every command, which releases the lease of the row, then removes the run's objects.
//!
//! The mounts are narrowed to the real clean-room paths: the run root read-write, the Git common
//! directory read-only, and the plan's caches with their access; the issue worktree, the whole
//! shared storage and the artifacts are never mounted, and no later config is read. `qa.stop` asks
//! the thread to stop the running command with its grace; it writes no operation. A QA run no
//! thread of this runtime holds, after a restart, is stopped and removed by its labels and `lost`
//! with the records of the commands that ran: it is never resumed.

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::Duration;

use contracts::{ErrorBody, ErrorCode, ExecutionState, FailureDomain, Request};
use engine::argv::{self, QaRun, Spec};
use engine::ops::{self, Observation};
use engine::plan::Labels;
use engine::policy;
use qa::plan::RunPlan;
use qa::runner::{self, Limits, Runner};
use rusqlite::Connection;
use serde_json::{Map, Value, json};
use state::canonical::{canonical_json, object_sha256, sha256_hex};
use state::executions::{self, Change, Execution, KIND_QA, NewExecution, is_terminal};
use state::time::{iso_from_micros, iso_now, now_micros};

use crate::container::{Checked, host_user, protected_inputs};
use crate::launch::{
    acquire, guard, injected, log_directory, owner, replay, sql, store_busy, stored_operation,
};
use crate::{Supervisor, hook_policy};

/// How often the runner looks at a command and at the stop of the run.
const RUNNER_POLL: Duration = Duration::from_millis(100);
/// How long the runner waits for the CLI of a command after the stop or the kill of its container.
const END_WAIT: Duration = Duration::from_secs(30);
/// What the deadline of a run adds to each command besides its timeout and grace.
const COMMAND_MARGIN_SECONDS: i64 = 60;

fn malformed(detail: &str) -> ErrorBody {
    ErrorBody::new(
        ErrorCode::MalformedRequest,
        FailureDomain::Tooling,
        false,
        format!("the request is not valid: {detail}"),
        "send {\"dispatch_id\", \"attempt\", \"run_root\", \"workdir\", \"tmpdir\", \"groups\"} as the payload of qa.run",
    )
}

fn denied(detail: impl Into<String>) -> ErrorBody {
    ErrorBody::new(
        ErrorCode::PolicyDenied,
        FailureDomain::Policy,
        false,
        detail,
        "run only the approved commands of a working QA dispatch whose brief pins qa_execution, at the attempt of its status and in its clean room; nothing was written",
    )
}

/// The deterministic operation id of an attempt of a QA run.
pub fn qa_operation_id(dispatch_id: &str, brief_sha256: &str, attempt: i64) -> String {
    sha256_hex(canonical_json(&json!([dispatch_id, brief_sha256, attempt])).as_bytes())
}

/// The payload of `qa.run`.
struct QaRequest {
    dispatch_id: String,
    attempt: i64,
    run_root: PathBuf,
    workdir: PathBuf,
    tmpdir: PathBuf,
    groups: Value,
    plan: RunPlan,
}

const PAYLOAD_KEYS: [&str; 6] = [
    "dispatch_id",
    "attempt",
    "run_root",
    "workdir",
    "tmpdir",
    "groups",
];

fn qa_request(request: &Request) -> Result<QaRequest, ErrorBody> {
    let Some(Value::Object(payload)) = request.payload.as_ref() else {
        return Err(malformed("the payload is an object"));
    };
    if payload.len() != PAYLOAD_KEYS.len()
        || PAYLOAD_KEYS.iter().any(|key| !payload.contains_key(*key))
    {
        return Err(malformed(
            "the payload names exactly the six qa.run members",
        ));
    }
    let text = |key: &str| {
        payload
            .get(key)
            .and_then(Value::as_str)
            .filter(|text| !text.is_empty())
            .ok_or_else(|| malformed(&format!("{key} is a non-empty text")))
    };
    let path = |key: &str| {
        text(key).map(PathBuf::from).and_then(|path| {
            if path.is_absolute() {
                Ok(path)
            } else {
                Err(malformed(&format!("{key} is an absolute path")))
            }
        })
    };
    let attempt = payload
        .get("attempt")
        .and_then(Value::as_i64)
        .filter(|attempt| *attempt >= 1)
        .ok_or_else(|| malformed("attempt is a positive integer"))?;
    let groups = payload.get("groups").cloned().unwrap_or(Value::Null);
    let plan = RunPlan::parse(&groups).map_err(|detail| malformed(&detail))?;
    Ok(QaRequest {
        dispatch_id: text("dispatch_id")?.to_owned(),
        attempt,
        run_root: path("run_root")?,
        workdir: path("workdir")?,
        tmpdir: path("tmpdir")?,
        groups,
        plan,
    })
}

/// The limits the approved QA brief pins in `qa_execution`.
#[derive(Clone, Copy)]
struct QaLimits {
    command_timeout_seconds: i64,
    stop_grace_seconds: i64,
    poll_interval_seconds: i64,
}

fn qa_limits(brief: &Map<String, Value>) -> Option<QaLimits> {
    let execution = brief.get("qa_execution")?.as_object()?;
    if execution.get("supervisor")?.as_str()? != "harness-runtime" {
        return None;
    }
    let number = |key: &str| execution.get(key).and_then(Value::as_i64);
    let limits = QaLimits {
        command_timeout_seconds: number("command_timeout_seconds")?,
        stop_grace_seconds: number("stop_grace_seconds")?,
        poll_interval_seconds: number("poll_interval_seconds")?,
    };
    (limits.command_timeout_seconds > 0
        && limits.stop_grace_seconds >= 0
        && limits.poll_interval_seconds > 0)
        .then_some(limits)
}

fn seconds(value: i64) -> Duration {
    Duration::from_secs(u64::try_from(value).unwrap_or(0))
}

/// Every command the approved brief names: its verification commands and the preparation,
/// environment probes and project file checks of its infrastructure retry policy.
fn approved_commands(brief: &Map<String, Value>) -> Vec<Value> {
    let mut approved: Vec<Value> = brief
        .get("verification_commands")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let record = Value::Object(brief.clone());
    for retry in [
        record.pointer("/orchestration_policy/infrastructure_retry"),
        record.pointer("/configuration/config/infrastructure_retry"),
    ]
    .into_iter()
    .flatten()
    {
        for key in [
            "qa_preparation",
            "qa_environment_probes",
            "qa_project_file_checks",
        ] {
            if let Some(items) = retry.get(key).and_then(Value::as_array) {
                approved.extend(items.iter().cloned());
            }
        }
    }
    approved
}

/// The container plan and pinned images of a QA brief whose `runtime_access` the built-in
/// `docker` extension runs.
fn qa_container_of(brief: &Map<String, Value>) -> Option<(Value, Value)> {
    let plan = brief.get("runtime_access")?;
    (plan.get("runtime_extension")?.as_str()? == "docker" && plan.get("container").is_some()).then(
        || {
            (
                plan.clone(),
                brief.get("runtime_images").cloned().unwrap_or(Value::Null),
            )
        },
    )
}

fn canonical(path: &Path, what: &str) -> Result<PathBuf, ErrorBody> {
    path.canonicalize().map_err(|err| {
        denied(format!(
            "the {what} {} cannot be resolved: {err}",
            path.display()
        ))
    })
}

/// The clean room of a run, narrowed to the pinned plan, and its mounts.
struct Room {
    run_root: PathBuf,
    workdir: PathBuf,
    tmpdir: PathBuf,
    mounts: Vec<(String, bool)>,
}

/// A QA run that passed step 1.
struct AdmittedQa {
    digest: String,
    limits: QaLimits,
    checked: Checked,
    observation: Observation,
    room: Room,
}

/// What `qa.run` finds in the store: a stored answer, or a new run.
enum FoundQa {
    Stored(Result<Value, ErrorBody>),
    New(Box<AdmittedQa>),
}

/// Everything the thread of a run owns.
struct RunJob {
    execution: Execution,
    plan: RunPlan,
    labels: Labels,
    environment: Vec<String>,
    settings: Vec<(String, String)>,
    mounts: Vec<(String, bool)>,
    hosts: bool,
    image_id: String,
    workdir: String,
    tmpdir: PathBuf,
    commands: PathBuf,
    observation: Observation,
    limits: QaLimits,
    stop: Arc<AtomicBool>,
}

impl Supervisor {
    /// The directory of the stream files and records of the commands of a QA run.
    fn commands_dir(&self, id: &str) -> Result<PathBuf, ErrorBody> {
        Ok(self.state()?.join("executions").join(id).join("commands"))
    }

    /// The clean-room paths of a run, narrowed to the pinned plan: the run root strictly inside
    /// the plan's shared storage, never holding the state directory or a protected path, and the
    /// checkout and temporary directory inside the run root.
    fn narrow(&self, qa: &QaRequest, checked: &Checked) -> Result<Room, ErrorBody> {
        let shared = checked
            .plan
            .requirements
            .iter()
            .find(|item| item.resource == "shared_storage" && item.write)
            .ok_or_else(|| {
                denied("the pinned plan grants no writable shared storage for the clean room")
            })?;
        let shared = canonical(Path::new(&shared.path), "shared storage")?;
        let run_root = canonical(&qa.run_root, "run root")?;
        if run_root == shared || !run_root.starts_with(&shared) {
            return Err(denied(format!(
                "the run root {} is not inside the plan's shared storage {}",
                run_root.display(),
                shared.display()
            )));
        }
        let state = self
            .state()?
            .canonicalize()
            .unwrap_or_else(|_| PathBuf::new());
        if state.starts_with(&run_root) {
            return Err(denied(
                "the run root holds the state directory of the runtime",
            ));
        }
        let protected = policy::protected_paths(&protected_inputs(
            host_user().uid,
            self.runtime_paths.clone(),
        ));
        let root_text = run_root.to_string_lossy().into_owned();
        if let Err(denials) = policy::deny_mounts(std::slice::from_ref(&root_text), &protected) {
            let reasons: Vec<&str> = denials
                .iter()
                .map(|denial| denial.reason.as_str())
                .collect();
            return Err(denied(reasons.join("; ")));
        }
        let workdir = canonical(&qa.workdir, "clean checkout")?;
        let tmpdir = canonical(&qa.tmpdir, "temporary directory")?;
        for (what, path) in [
            ("clean checkout", &workdir),
            ("temporary directory", &tmpdir),
        ] {
            if !path.starts_with(&run_root) {
                return Err(denied(format!(
                    "the {what} {} is not inside the run root {}",
                    path.display(),
                    run_root.display()
                )));
            }
        }
        let mut mounts = vec![(root_text, true)];
        if let Some(common) = checked.plan.root("git_common") {
            // Git operations stay with the coordinator: a command only reads the Git metadata.
            mounts.push((common.to_owned(), false));
        }
        for item in checked
            .plan
            .requirements
            .iter()
            .filter(|item| item.resource == "cache")
        {
            match mounts.iter_mut().find(|(path, _)| *path == item.path) {
                Some((_, write)) => *write |= item.write,
                None => mounts.push((item.path.clone(), item.write)),
            }
        }
        Ok(Room {
            run_root,
            workdir,
            tmpdir,
            mounts,
        })
    }

    /// Step 1 up to its write: the answer of an operation stored before, or the checks of a run.
    fn admit_qa(
        self: &Arc<Self>,
        connection: &mut Connection,
        request: &Request,
        qa: &QaRequest,
    ) -> Result<FoundQa, ErrorBody> {
        let (brief, status) = executions::dispatch_records(connection, &qa.dispatch_id)?
            .ok_or_else(|| {
                ErrorBody::new(
                    ErrorCode::NotFound,
                    FailureDomain::Tooling,
                    false,
                    format!("the store has no dispatch {}", qa.dispatch_id),
                    "name a dispatch the store holds",
                )
            })?;
        let digest = state::write::request_digest(request);
        if let Some(stored) = stored_operation(connection, &request.operation_id)? {
            if let Some(answer) = replay(stored, &digest, &request.operation_id) {
                return Ok(FoundQa::Stored(answer));
            }
            if let Some(execution) = executions::load(connection, &request.operation_id)? {
                self.settle_qa(connection, &execution)?;
            }
            let answer = stored_operation(connection, &request.operation_id)?
                .and_then(|stored| replay(stored, &digest, &request.operation_id));
            return Ok(FoundQa::Stored(answer.unwrap_or_else(|| {
                Err(store_busy("the QA run is not settled yet"))
            })));
        }
        if status.get("state").and_then(Value::as_str) != Some("working") {
            return Err(denied(format!(
                "the dispatch {} is not working",
                qa.dispatch_id
            )));
        }
        let attempt = status.get("attempt").and_then(Value::as_i64).unwrap_or(1);
        if attempt != qa.attempt {
            return Err(denied(format!(
                "the dispatch status names attempt {attempt}, not {}",
                qa.attempt
            )));
        }
        if brief.get("role").and_then(Value::as_str) != Some("qa") {
            return Err(denied("only a QA dispatch runs QA commands"));
        }
        let limits = qa_limits(&brief).ok_or_else(|| {
            denied("the approved brief pins no qa_execution of the supervisor harness-runtime")
        })?;
        let expected = qa_operation_id(&qa.dispatch_id, &object_sha256(&brief), qa.attempt);
        if request.operation_id != expected {
            return Err(denied(
                "the operation id is not sha256([dispatch_id, brief_sha256, attempt]) of the stored brief",
            )
            .with_fact("expected_operation_id", expected));
        }
        hook_policy::verify(&brief)?;
        let lane = sql(state::lease::load(connection))?;
        let held = lane.as_ref().and_then(|lane| {
            let held = lane.held.as_ref()?;
            (held.owner_kind == "dispatch"
                && held.owner_id == qa.dispatch_id
                && request.fencing_token.as_deref()
                    == Some(lane.fencing_token.to_string().as_str()))
            .then_some(())
        });
        if held.is_none() {
            return Err(ErrorBody::new(
                ErrorCode::FencingMismatch,
                FailureDomain::Policy,
                false,
                format!(
                    "the QA lane is not held by {} with the fencing token of the request",
                    qa.dispatch_id
                ),
                "acquire the QA lane for the dispatch and send its fencing token; nothing was written",
            ));
        }
        if let Some(other) = executions::unfinished(connection)?
            .into_iter()
            .find(|execution| execution.kind == KIND_QA)
        {
            return Err(ErrorBody::new(
                ErrorCode::LeaseHeld,
                FailureDomain::Infrastructure,
                true,
                format!(
                    "the QA run {} of {} is not finished",
                    other.id, other.dispatch_id
                ),
                "wait for the QA run to end or stop it with qa.stop; nothing was written",
            ));
        }
        let approved = approved_commands(&brief);
        for (_, command) in qa.plan.commands() {
            if !approved.contains(&command.as_json()) {
                return Err(denied(format!(
                    "the command of the stage {} is not one the brief approved",
                    command.stage_id
                )));
            }
        }
        let (plan, images) = qa_container_of(&brief).ok_or_else(|| {
            denied("the approved QA brief names no container plan of the built-in docker extension")
        })?;
        let (checked, observation) = self.admit_container(&plan, &images)?;
        let room = self.narrow(qa, &checked)?;
        Ok(FoundQa::New(Box::new(AdmittedQa {
            digest,
            limits,
            checked,
            observation,
            room,
        })))
    }

    /// `qa.run`: run the command groups of an attempt of a QA dispatch once, answering at once
    /// with the receipt; the thread of the run records the exit.
    ///
    /// # Errors
    /// The typed error of the response; a sidecar the engine refused is the stored
    /// `engine_unavailable` of the operation.
    pub fn qa_run(self: &Arc<Self>, request: &Request) -> Result<Value, ErrorBody> {
        let state = self.state()?.to_path_buf();
        let qa = qa_request(request)?;
        self.reconcile_all();
        let lock = self.lock_of(&request.operation_id);
        let _held = acquire(&lock, request.request_deadline)?;
        let mut connection = executions::connect(&state)?;
        let admitted = match self.admit_qa(&mut connection, request, &qa)? {
            FoundQa::Stored(answer) => return answer,
            FoundQa::New(admitted) => admitted,
        };
        let id = request.operation_id.clone();
        let base = argv::qa_base(&qa.dispatch_id, qa.attempt);
        let labels = Labels {
            dispatch: qa.dispatch_id.clone(),
            repository: admitted.checked.repository.clone(),
            plan_digest: admitted.checked.plan.plan_digest.clone(),
            operation: id.clone(),
            execution: id.clone(),
        };
        let commands = self.commands_dir(&id)?;
        let tmp = admitted.room.tmpdir.to_string_lossy().into_owned();
        let settings: Vec<(String, String)> = ["TMPDIR", "TMP", "TEMP", "GIT_CEILING_DIRECTORIES"]
            .iter()
            .map(|name| ((*name).to_owned(), tmp.clone()))
            .chain([("HARNESS_VERIFY_COLD".to_owned(), "1".to_owned())])
            .collect();
        let observation = &admitted.observation;
        let launch = json!({
            "dispatch_id": qa.dispatch_id,
            "attempt": qa.attempt,
            "labels": {
                "harness.dispatch": labels.dispatch,
                "harness.repository": labels.repository,
                "harness.plan_digest": labels.plan_digest,
                "harness.operation": id,
                "harness.execution": id,
            },
            "engine": admitted.checked.plan.engine.as_str(),
            "engine_mode": observation.mode.as_str(),
            "selinux": observation.selinux,
            "environment_id": observation.environment_id,
            "images": {"worker": observation.worker_image_id, "proxy": observation.proxy_image_id},
            "objects_base": base,
            "run_root": admitted.room.run_root,
            "workdir": admitted.room.workdir,
            "tmpdir": admitted.room.tmpdir,
            "mounts": admitted.room.mounts.iter().map(|(path, write)| json!([path, write])).collect::<Vec<_>>(),
            "environment": admitted.checked.plan.environment,
            "settings": settings.iter().map(|(name, _)| name).collect::<Vec<_>>(),
            "groups": qa.groups,
            "commands_dir": commands,
            "limits": {
                "command_timeout_seconds": admitted.limits.command_timeout_seconds,
                "stop_grace_seconds": admitted.limits.stop_grace_seconds,
            },
        });
        hooks_reach("qa.run:admitted")?;
        let execution = self.qa_intent(&mut connection, request, &qa, &admitted, &launch)?;
        hooks_reach("qa.run:intent")?;
        let engine = self.engine(admitted.checked.plan.engine);
        let worker_image_id = observation.worker_image_id.clone().unwrap_or_default();
        let spec = Spec {
            plan: &admitted.checked.plan,
            labels: &labels,
            command: &[],
            worker_image_id: &worker_image_id,
            proxy_image_id: observation.proxy_image_id.as_deref(),
            user: host_user(),
            mode: observation.mode,
            selinux: observation.selinux,
        };
        let sidecar = match ops::sidecar(engine.as_ref(), &spec, &base) {
            Ok(sidecar) => sidecar,
            Err(failure) => {
                let error = ErrorBody::new(
                    ErrorCode::EngineUnavailable,
                    FailureDomain::Infrastructure,
                    false,
                    format!("{}; {}", failure.detail, failure.removals.join("; ")),
                    "fix the engine error and repeat qa run: a new attempt of the dispatch gets a new operation id",
                )
                .with_fact("stage", failure.stage.clone())
                .with_fact("removals", failure.removals.clone())
                .with_fact("execution_state", ExecutionState::Lost.as_str());
                let facts =
                    json!({"sidecar_failure": failure.detail, "removals": failure.removals});
                self.fail(&mut connection, &execution, &error, facts)?;
                return Err(error);
            }
        };
        let receipt = self.qa_receipt(&mut connection, &execution, &sidecar)?;
        let stop = Arc::new(AtomicBool::new(false));
        guard(&self.inner.watched).insert(id.clone());
        guard(&self.inner.qa_stops).insert(id.clone(), Arc::clone(&stop));
        let job = RunJob {
            execution: executions::load(&connection, &id)?.unwrap_or(execution),
            plan: qa.plan,
            labels,
            environment: admitted.checked.plan.environment.clone(),
            settings,
            mounts: admitted.room.mounts.clone(),
            hosts: !admitted.checked.plan.hosts.is_empty(),
            image_id: worker_image_id,
            workdir: admitted.room.workdir.to_string_lossy().into_owned(),
            tmpdir: admitted.room.tmpdir.clone(),
            commands,
            observation: admitted.observation.clone(),
            limits: admitted.limits,
            stop,
        };
        let supervisor = Arc::clone(self);
        thread::spawn(move || {
            supervisor.drive_qa(&job, &engine);
            guard(&supervisor.inner.qa_stops).remove(&job.execution.id);
            guard(&supervisor.inner.watched).remove(&job.execution.id);
        });
        Ok(receipt)
    }

    /// Step 2: the operation intent and the `qa` execution of the attempt in `launching`.
    fn qa_intent(
        &self,
        connection: &mut Connection,
        request: &Request,
        qa: &QaRequest,
        admitted: &AdmittedQa,
        launch: &Value,
    ) -> Result<Execution, ErrorBody> {
        let at = iso_now();
        let commands = i64::try_from(qa.plan.commands().count()).unwrap_or(i64::MAX);
        let per_command = admitted.limits.command_timeout_seconds
            + admitted.limits.stop_grace_seconds
            + COMMAND_MARGIN_SECONDS;
        let deadline = iso_from_micros(
            now_micros().saturating_add(
                commands
                    .saturating_mul(per_command)
                    .saturating_mul(1_000_000),
            ),
        );
        let tx = executions::begin(connection)?;
        sql(tx.execute(
            "INSERT INTO operations (operation_id, operation, request_digest, state, created_at)
             VALUES (?1, ?2, ?3, 'intent', ?4)",
            rusqlite::params![request.operation_id, request.operation, admitted.digest, at],
        ))?;
        let execution = executions::insert(
            &tx,
            &NewExecution {
                id: &request.operation_id,
                dispatch_id: &qa.dispatch_id,
                attempt: qa.attempt,
                kind: KIND_QA,
                engine: Some(admitted.checked.plan.engine.as_str()),
                launch,
                execution_deadline: &deadline,
                stop_grace_seconds: admitted.limits.stop_grace_seconds,
                poll_interval_seconds: admitted.limits.poll_interval_seconds,
                lease_owner: &owner(),
            },
            &at,
        )?;
        sql(tx.commit())?;
        Ok(execution)
    }

    /// Step 4: the receipt, `running` and the committed operation, in one transaction.
    fn qa_receipt(
        &self,
        connection: &mut Connection,
        execution: &Execution,
        sidecar: &ops::Sidecar,
    ) -> Result<Value, ErrorBody> {
        let at = iso_now();
        let tx = executions::begin(connection)?;
        let current = executions::load(&tx, &execution.id)?
            .filter(|current| current.state == ExecutionState::Launching)
            .ok_or_else(|| store_busy("the QA run changed before its receipt"))?;
        let receipt = json!({
            "execution_id": current.id,
            "dispatch_id": current.dispatch_id,
            "attempt": current.attempt,
            "state": ExecutionState::Running.as_str(),
            "kind": KIND_QA,
            "engine": current.engine,
            "proxy_id": sidecar.proxy_id,
            "objects": sidecar.objects.iter()
                .map(|(kind, name)| json!([kind.as_str(), name]))
                .collect::<Vec<_>>(),
            "commands_dir": current.launch.get("commands_dir").cloned().unwrap_or(Value::Null),
            "launched_at": at,
            "execution_deadline": current.execution_deadline,
        });
        executions::transition(
            &tx,
            &current,
            ExecutionState::Running,
            Change {
                receipt: Some(receipt.clone()),
                facts: json!({"kind": KIND_QA, "attempt": current.attempt}),
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

    /// The thread of a run: every command in order, then the exit with its evidence, then the
    /// removal of the run's objects.
    fn drive_qa(&self, job: &RunJob, engine: &Arc<dyn engine::Engine>) {
        let id = &job.execution.id;
        let base = argv::qa_base(&job.execution.dispatch_id, job.execution.attempt);
        let run = QaRun {
            engine: engine.kind(),
            base: &base,
            hosts: job.hosts,
            environment: &job.environment,
            settings: &job.settings,
            labels: &job.labels,
            workdir: &job.workdir,
            mounts: &job.mounts,
            image_id: &job.image_id,
            user: host_user(),
            mode: job.observation.mode,
            selinux: job.observation.selinux,
        };
        let outcome = match log_directory(&job.commands) {
            Ok(()) => {
                let runner = Runner {
                    engine: engine.as_ref(),
                    run: &run,
                    directory: &job.commands,
                    limits: Limits {
                        command_timeout: seconds(job.limits.command_timeout_seconds),
                        stop_grace: seconds(job.limits.stop_grace_seconds),
                        poll: RUNNER_POLL,
                        end_wait: END_WAIT,
                    },
                    junit_dir: Some(&job.tmpdir),
                };
                Ok(runner.run(&job.plan, &job.stop, |_| {}))
            }
            Err(err) => Err(err.to_string()),
        };
        let mut exit = match &outcome {
            Ok(outcome) => outcome.to_json(),
            Err(reason) => json!({
                "commands": runner::recover(&job.plan, &job.commands),
                "stopped_by": null,
                "error": format!("the directory of the commands cannot be created: {reason}"),
            }),
        };
        exit["source"] = json!("qa-runner");
        exit["ended_at"] = json!(iso_now());
        let facts = json!({"source": "qa-runner", "stopped_by": exit["stopped_by"]});
        let mut recorded = false;
        for _ in 0..5 {
            let Ok(Some(current)) = self
                .state()
                .and_then(executions::connect)
                .and_then(|connection| executions::load(&connection, id))
            else {
                break;
            };
            if is_terminal(current.state) {
                break;
            }
            let change = Change {
                exit: Some(exit.clone()),
                facts: facts.clone(),
                ..Change::default()
            };
            if self.record(id, current.state, ExecutionState::Exited, &change) {
                recorded = true;
                break;
            }
        }
        if recorded {
            self.remove_after_exit(engine.as_ref(), &job.execution);
        }
    }

    /// `qa.stop`: ask the run of the attempt of the payload to stop its running command with its
    /// grace; every later command is `not_run`. An ended run answers as it is.
    ///
    /// # Errors
    /// The typed error of the response.
    pub fn qa_stop(self: &Arc<Self>, request: &Request) -> Result<Value, ErrorBody> {
        let state = self.state()?.to_path_buf();
        let (dispatch_id, attempt) = match request.payload.as_ref() {
            Some(Value::Object(members)) if members.len() == 2 => (
                members.get("dispatch_id").and_then(Value::as_str),
                members.get("attempt").and_then(Value::as_i64),
            ),
            _ => (None, None),
        };
        let (Some(dispatch_id), Some(attempt)) = (dispatch_id, attempt) else {
            return Err(ErrorBody::new(
                ErrorCode::MalformedRequest,
                FailureDomain::Tooling,
                false,
                "the request is not valid: the payload is {\"dispatch_id\", \"attempt\"}",
                "send the dispatch id and the attempt of the QA run to stop",
            ));
        };
        let connection = executions::connect(&state)?;
        let execution = executions::of_dispatch(&connection, dispatch_id)?
            .filter(|execution| execution.kind == KIND_QA && execution.attempt == attempt)
            .ok_or_else(|| {
                ErrorBody::new(
                    ErrorCode::NotFound,
                    FailureDomain::Tooling,
                    false,
                    format!("the dispatch {dispatch_id} has no QA run of attempt {attempt}"),
                    "name the dispatch and the attempt of a QA run the runtime started",
                )
            })?;
        if !is_terminal(execution.state) {
            if execution.state == ExecutionState::Running {
                let change = Change {
                    facts: json!({"stopped_by": "command", "requested_at": iso_now()}),
                    ..Change::default()
                };
                self.record(
                    &execution.id,
                    ExecutionState::Running,
                    ExecutionState::Stopping,
                    &change,
                );
            }
            let flag = guard(&self.inner.qa_stops).get(&execution.id).cloned();
            match flag {
                Some(flag) => flag.store(true, Ordering::SeqCst),
                // No thread of this runtime runs it: reconcile stops and settles it.
                None => self.reconcile_all(),
            }
        }
        let current = executions::load(&connection, &execution.id)?.unwrap_or(execution);
        Ok(json!({"execution": current.to_json()}))
    }

    /// Reconcile a QA run no thread of this runtime holds: a runtime died during its launch or
    /// its commands. Its labelled containers are stopped with the grace, killed and removed with
    /// its sidecar and network; it is `lost` with the records of the commands that ran and
    /// `not_run` for the others, never resumed. An engine that does not answer leaves it
    /// `unknown`.
    pub(crate) fn settle_qa(
        self: &Arc<Self>,
        connection: &mut Connection,
        execution: &Execution,
    ) -> Result<(), ErrorBody> {
        if is_terminal(execution.state) || self.is_watched(&execution.id) {
            return Ok(());
        }
        let Some(engine) = self.engine_of(execution) else {
            return Self::to_unknown(connection, execution, "the QA run names no known engine");
        };
        let Some(objects) = self.own_objects(engine.as_ref(), execution) else {
            return Self::to_unknown(
                connection,
                execution,
                "the engine did not list the objects of the QA run",
            );
        };
        let grace = seconds(execution.stop_grace_seconds);
        let stops: Vec<Value> = objects
            .iter()
            .filter(|object| object.role == "worker")
            .map(|object| {
                let (stopped, stop) = ops::stop(engine.as_ref(), &object.id, grace);
                let (killed, kill) = ops::kill(engine.as_ref(), &object.id);
                json!({"object": object.name, "stop": {"ok": stopped, "detail": stop},
                       "kill": {"ok": killed, "detail": kill}})
            })
            .collect();
        let removal = ops::remove(engine.as_ref(), &objects);
        let groups = execution
            .launch
            .get("groups")
            .cloned()
            .unwrap_or(Value::Null);
        let commands = match (RunPlan::parse(&groups), self.commands_dir(&execution.id)) {
            (Ok(plan), Ok(dir)) => runner::recover(&plan, &dir),
            _ => Vec::new(),
        };
        let reason = if execution.receipt.is_none() {
            "the launch of the QA run did not finish; never resumed"
        } else {
            "no thread of this runtime runs the QA run; never resumed"
        };
        let teardown = json!({
            "removed": removal.removed,
            "missing": removal.missing,
            "failed": removal.failed.iter()
                .map(|(object, reason)| json!({"object": object, "reason": reason}))
                .collect::<Vec<_>>(),
            "complete": removal.failed.is_empty(),
        });
        let exit = json!({
            "commands": commands,
            "stopped_by": null,
            "source": "reconcile",
            "reason": reason,
            "stops": stops,
            "teardown": teardown,
            "ended_at": iso_now(),
        });
        let error = ErrorBody::new(
            ErrorCode::EngineUnavailable,
            FailureDomain::Infrastructure,
            false,
            format!("the QA run {}: {reason}", execution.id),
            "repeat qa run: a new attempt of the dispatch gets a new operation id (ADR 0021, section 4)",
        )
        .with_fact("execution_state", ExecutionState::Lost.as_str());
        let at = iso_now();
        let tx = executions::begin(connection)?;
        let Some(current) = executions::load(&tx, &execution.id)?
            .filter(|current| current.state == execution.state)
        else {
            return Ok(());
        };
        executions::transition(
            &tx,
            &current,
            ExecutionState::Lost,
            Change {
                exit: Some(exit),
                facts: json!({"reason": reason}),
                ..Change::default()
            },
            &at,
        )?;
        sql(tx.execute(
            "UPDATE operations SET state = 'failed', result_json = ?2, completed_at = ?3
             WHERE operation_id = ?1 AND state = 'intent'",
            rusqlite::params![
                current.id,
                serde_json::to_string(&error).unwrap_or_default(),
                at
            ],
        ))?;
        sql(tx.commit())
    }
}

/// A debug-build hook of a QA run (see the launch hooks in `launch`).
fn hooks_reach(point: &str) -> Result<(), ErrorBody> {
    state::hooks::reach_once(point).map_err(|err| injected(&err))
}
