//! The worker container of a docker brief, supervised with a scripted Podman (ADR 0022,
//! section 4): launch from the stored brief, receipt, exit with logs and teardown, the deadline
//! stop, an unfinished launch rolled back, and the refusals of step 1 that write nothing. Every
//! engine command is `podman`, the engine of the plan and of the `executions` row.
#![cfg(target_os = "linux")]

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use contracts::{ErrorCode, ExecutionState, PROTOCOL_VERSION, Request};
use engine::{Answer, Engine, Kind, Scripted};
use rusqlite::Connection;
use serde_json::{Map, Value, json};
use state::canonical::{canonical_json, object_sha256, sha256_hex};
use state::executions::{self, Execution};
use supervisor::Supervisor;

const DISPATCH: &str = "dispatch-11111111-0000-4000-8000-000000000001";
const FIXTURE: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../state/tests/fixtures/legacy_v3"
);
const WORKER_TAG: &str = "harness-worker:local";
const WORKER_PIN: &str = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const WORKER_ID: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

fn copy_tree(from: &Path, to: &Path) {
    fs::create_dir_all(to).unwrap();
    for entry in fs::read_dir(from).unwrap() {
        let entry = entry.unwrap();
        let target = to.join(entry.file_name());
        if entry.file_type().unwrap().is_dir() {
            copy_tree(&entry.path(), &target);
        } else {
            fs::copy(entry.path(), target).unwrap();
        }
    }
}

fn podman_info() -> String {
    json!({"host": {"hostname": "podman-host", "serviceIsRemote": false,
                    "security": {"rootless": true, "selinuxEnabled": true}},
           "version": {"Version": "5.4.0"}})
    .to_string()
}

fn inspected(status: &str, execution: &str, repository: &Path) -> Answer {
    Answer::success(
        json!({"Id": WORKER_ID, "Name": format!("/harness-{DISPATCH}"),
               "State": {"Status": status, "ExitCode": 3, "FinishedAt": "2026-10-28T10:00:00Z",
                         "OOMKilled": false},
               "Config": {"Labels": {"harness.dispatch": DISPATCH,
                                     "harness.repository": repository,
                                     "harness.execution": execution}}})
        .to_string(),
    )
}

struct Repo {
    root: PathBuf,
    git_common: PathBuf,
    engine: Arc<Scripted>,
}

impl Repo {
    fn new(name: &str, timeout: i64) -> Self {
        let root = std::env::temp_dir().join(format!("hr-container-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let state = root.join(".harness/orchestration/state");
        copy_tree(Path::new(FIXTURE), &state);
        let generation = state.join("generations/generation-7a4a6d6e");
        fs::remove_file(generation.join("qa-lane/lease.json")).unwrap();
        fs::remove_file(generation.join("qa-lane/queue/00000000000000000001-dispatch.json"))
            .unwrap();
        state::migrate::run(&root).unwrap();
        let root = root.canonicalize().unwrap();
        for dir in [".git", "worktree", ".harness/.sandboxes/artifacts"] {
            fs::create_dir_all(root.join(dir)).unwrap();
        }
        let git_common = root.join(".git");
        let base = root.clone();
        let path = move |rel: &str| base.join(rel).to_str().unwrap().to_owned();
        let plan = json!({
            "mode": "sandbox", "network": {"hosts": []}, "runtime_extension": "docker",
            "container": {"engine": "podman", "worker_image": WORKER_TAG},
            "requirements": [
                {"resource": "checkout", "path": path("worktree"), "access": "write"},
                {"resource": "git_common", "path": path(".git"), "access": "read"},
                {"resource": "shared_storage", "path": path(".harness"), "access": "read"},
                {"resource": "artifacts", "path": path(".harness/.sandboxes/artifacts"), "access": "write"},
            ],
            "plan_digest": "d".repeat(64),
        });
        let engine = Arc::new(Scripted::new(Kind::Podman));
        let repo = Self {
            root,
            git_common,
            engine,
        };
        let connection = repo.store();
        let body = |table: &str| -> Map<String, Value> {
            let text: String = connection
                .query_row(
                    &format!("SELECT body_json FROM {table} WHERE id = ?1"),
                    [DISPATCH],
                    |row| row.get(0),
                )
                .unwrap();
            serde_json::from_str(&text).unwrap()
        };
        let mut brief = body("dispatch");
        brief.insert("resolved_transport".to_owned(), json!("external"));
        brief.insert(
            "execution".to_owned(),
            json!({"supervisor": "harness-runtime", "timeout_seconds": timeout,
                   "stop_grace_seconds": 5, "poll_interval_seconds": 1}),
        );
        brief.insert("runtime_access".to_owned(), plan);
        brief.insert(
            "runtime_images".to_owned(),
            json!({"worker": WORKER_PIN, "proxy": null}),
        );
        let mut status = body("dispatch_status");
        status.insert(
            "launch".to_owned(),
            json!({"argv": ["/work/adapter.sh", "dispatch"], "cwd": path("worktree")}),
        );
        for (table, record, state) in [
            ("dispatch", brief, None),
            ("dispatch_status", status, Some("dispatched")),
        ] {
            connection
                .execute(
                    &format!(
                        "UPDATE {table} SET body_json = ?2{} WHERE id = ?1",
                        state.map_or(String::new(), |s| format!(", state = '{s}'"))
                    ),
                    [DISPATCH, &Value::Object(record).to_string()],
                )
                .unwrap();
        }
        let bin = "podman";
        repo.engine
            .answer(&[bin, "info"], Answer::success(podman_info()))
            .answer(
                &[bin, "image", "inspect", "--format", "{{.Id}}", WORKER_TAG],
                Answer::success(WORKER_PIN.trim_start_matches("sha256:")),
            )
            .answer(&[bin, "create"], Answer::success(WORKER_ID))
            .answer(&[bin, "start"], Answer::success(WORKER_ID))
            .answer(&[bin, "rm"], Answer::success(WORKER_ID))
            .answer(
                &[bin, "logs"],
                Answer {
                    answered: true,
                    code: Some(0),
                    stdout: "worker out\n".to_owned(),
                    stderr: "worker err\n".to_owned(),
                },
            );
        repo
    }

    fn state(&self) -> PathBuf {
        self.root.join(".harness/orchestration/state")
    }

    fn store(&self) -> Connection {
        Connection::open(self.state().join("runtime.db")).unwrap()
    }

    fn supervisor(&self, runtime: Vec<PathBuf>) -> Arc<Supervisor> {
        let engine = Arc::clone(&self.engine);
        Arc::new(
            Supervisor::new(Some(self.state()))
                .with_runtime(Some(self.git_common.clone()), runtime)
                .with_engines(Arc::new(move |kind| {
                    assert_eq!(kind, Kind::Podman);
                    Arc::clone(&engine) as Arc<dyn Engine>
                })),
        )
    }

    fn operation_id(&self) -> String {
        let (brief, _) = executions::dispatch_records(&self.store(), DISPATCH)
            .unwrap()
            .unwrap();
        sha256_hex(canonical_json(&json!([DISPATCH, object_sha256(&brief), 1])).as_bytes())
    }

    fn request(&self, operation: &str, operation_id: String, payload: Value) -> Request {
        let deadline = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis()
            + 30_000;
        Request {
            protocol_version: PROTOCOL_VERSION,
            operation_id,
            operation: operation.to_owned(),
            request_deadline: u64::try_from(deadline).unwrap(),
            batch_id: None,
            dispatch_id: None,
            candidate_sha: None,
            plan_digest: None,
            expected_revision: None,
            fencing_token: None,
            payload: Some(payload),
        }
    }

    fn launch(&self, supervisor: &Arc<Supervisor>) -> Result<Value, contracts::ErrorBody> {
        supervisor.launch(&self.request(
            "execution.launch",
            self.operation_id(),
            json!({"dispatch_id": DISPATCH}),
        ))
    }

    fn execution(&self) -> Option<Execution> {
        executions::of_dispatch(&self.store(), DISPATCH).unwrap()
    }

    fn wait_for(&self, supervisor: &Arc<Supervisor>, state: ExecutionState) -> Execution {
        let until = Instant::now() + Duration::from_secs(20);
        loop {
            supervisor.reconcile_all();
            let execution = self.execution().unwrap();
            if execution.state == state || Instant::now() > until {
                assert_eq!(execution.state, state, "{execution:?}");
                return execution;
            }
            std::thread::sleep(Duration::from_millis(50));
        }
    }

    fn only_podman(&self) {
        for argv in self.engine.calls() {
            assert_eq!(argv[0], "podman", "{argv:?}");
            assert!(!argv.iter().any(|part| part == "volume"), "{argv:?}");
        }
    }
}

impl Drop for Repo {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

#[test]
fn a_docker_brief_launches_its_worker_with_podman_and_records_the_container() {
    let repo = Repo::new("launch", 3600);
    let supervisor = repo.supervisor(Vec::new());

    let receipt = repo.launch(&supervisor).unwrap();

    assert_eq!(receipt["container_id"], WORKER_ID);
    assert_eq!(receipt["engine"], "podman");
    let execution = repo.execution().unwrap();
    assert_eq!(
        (
            execution.state,
            execution.kind.as_str(),
            execution.engine.as_deref()
        ),
        (ExecutionState::Running, "container", Some("podman"))
    );
    assert_eq!(execution.container_id.as_deref(), Some(WORKER_ID));
    let create = repo
        .engine
        .calls()
        .into_iter()
        .find(|argv| argv[1] == "create")
        .unwrap();
    let joined = create.join(" ");
    for part in [
        "--userns=keep-id --security-opt label=disable",
        &format!("harness.execution={}", execution.id),
        &format!("harness.repository={}", repo.git_common.display()),
        "--entrypoint /work/adapter.sh",
    ] {
        assert!(joined.contains(part), "{part} not in {joined}");
    }
    repo.only_podman();
    // A repeat answers the stored receipt and never launches again.
    repo.engine.clear();
    assert_eq!(repo.launch(&supervisor).unwrap(), receipt);
    assert!(!repo.engine.calls().iter().any(|argv| argv[1] == "create"));
}

#[test]
fn an_exited_worker_leaves_its_logs_and_exit_and_its_objects_are_removed() {
    let repo = Repo::new("exit", 3600);
    let supervisor = repo.supervisor(Vec::new());
    repo.launch(&supervisor).unwrap();
    let id = repo.execution().unwrap().id;
    repo.engine
        .answer(
            &["podman", "container", "inspect", "--format"],
            inspected("exited", &id, &repo.git_common),
        )
        .answer(&["podman", "ps"], Answer::success(WORKER_ID))
        .answer(&["podman", "network", "ls"], Answer::success(""))
        .answer(
            &["podman", "container", "inspect", WORKER_ID],
            Answer::success(
                json!([{"Id": WORKER_ID, "Name": format!("/harness-{DISPATCH}"),
                        "State": {"Status": "exited"},
                        "Config": {"Labels": {"harness.dispatch": DISPATCH,
                                              "harness.repository": repo.git_common,
                                              "harness.execution": id}}}])
                .to_string(),
            ),
        );

    let exited = repo.wait_for(&supervisor, ExecutionState::Exited);

    let exit = exited.exit.unwrap();
    assert_eq!(exit["exit_code"], 3);
    assert_eq!(
        exit["teardown"]["removed"],
        json!([format!("container harness-{DISPATCH}")])
    );
    assert!(exited.lease_released_at.is_some());
    let logs = repo.state().join("executions").join(&id);
    assert_eq!(
        fs::read_to_string(logs.join("stdout.log")).unwrap(),
        "worker out\n"
    );
    assert_eq!(
        fs::read_to_string(logs.join("stderr.log")).unwrap(),
        "worker err\n"
    );
    assert!(repo.engine.calls().contains(&vec![
        "podman".to_owned(),
        "rm".to_owned(),
        WORKER_ID.to_owned()
    ]));
    repo.only_podman();
}

#[test]
fn a_worker_past_its_deadline_is_stopped_with_its_grace_then_killed() {
    let repo = Repo::new("deadline", 1);
    let supervisor = repo.supervisor(Vec::new());
    repo.launch(&supervisor).unwrap();
    let id = repo.execution().unwrap().id;
    repo.engine
        .answer(
            &["podman", "container", "inspect", "--format"],
            inspected("running", &id, &repo.git_common),
        )
        .answer(&["podman", "stop"], Answer::success(WORKER_ID))
        .answer(&["podman", "ps"], Answer::success(""))
        .answer(&["podman", "network", "ls"], Answer::success(""));
    let engine = Arc::clone(&repo.engine);
    let git_common = repo.git_common.clone();
    let stopped_id = id.clone();
    std::thread::sleep(Duration::from_millis(1100));
    // The kill ends the worker: the next inspect shows it exited.
    std::thread::spawn(move || {
        let until = Instant::now() + Duration::from_secs(20);
        while Instant::now() < until {
            if engine.calls().iter().any(|argv| argv[1] == "kill") {
                engine.answer(
                    &["podman", "container", "inspect", "--format"],
                    inspected("exited", &stopped_id, &git_common),
                );
                return;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    });
    repo.engine
        .answer(&["podman", "kill"], Answer::success(WORKER_ID));

    let exited = repo.wait_for(&supervisor, ExecutionState::Exited);

    assert_eq!(exited.exit.unwrap()["stopped_by"], "deadline");
    let calls = repo.engine.calls();
    assert!(calls.contains(&vec![
        "podman".to_owned(),
        "stop".to_owned(),
        "--time".to_owned(),
        "5".to_owned(),
        WORKER_ID.to_owned()
    ]));
    assert!(calls.contains(&vec![
        "podman".to_owned(),
        "kill".to_owned(),
        WORKER_ID.to_owned()
    ]));
    repo.only_podman();
}

#[test]
fn a_worker_with_foreign_labels_makes_the_execution_unknown_and_nothing_is_touched() {
    let repo = Repo::new("foreign", 3600);
    let supervisor = repo.supervisor(Vec::new());
    repo.launch(&supervisor).unwrap();
    repo.engine.answer(
        &["podman", "container", "inspect", "--format"],
        inspected("exited", "another-execution", &repo.git_common),
    );
    repo.engine.clear();

    repo.wait_for(&supervisor, ExecutionState::Unknown);

    assert!(
        !repo
            .engine
            .calls()
            .iter()
            .any(|argv| ["rm", "logs", "stop", "kill"].contains(&argv[1].as_str()))
    );
}

#[test]
fn a_failed_start_rolls_back_newest_first_and_the_execution_is_lost_for_good() {
    let repo = Repo::new("failed", 3600);
    let supervisor = repo.supervisor(Vec::new());
    repo.engine
        .answer(&["podman", "start"], Answer::failure("cannot start"));

    let refused = repo.launch(&supervisor).unwrap_err();

    assert_eq!(refused.code, ErrorCode::EngineUnavailable);
    assert_eq!(repo.execution().unwrap().state, ExecutionState::Lost);
    assert_eq!(
        repo.engine.calls().last().unwrap(),
        &vec![
            "podman".to_owned(),
            "rm".to_owned(),
            "--force".to_owned(),
            format!("harness-{DISPATCH}")
        ]
    );
    repo.engine.clear();
    assert_eq!(
        repo.launch(&supervisor).unwrap_err().code,
        ErrorCode::EngineUnavailable
    );
    assert!(
        repo.engine.calls().is_empty(),
        "a lost launch is never repeated"
    );
}

#[test]
fn the_fallback_runtime_directory_never_denies_the_mandatory_plan_roots() {
    let repo = Repo::new("fallback", 3600);
    // The runtime without XDG_RUNTIME_DIR (ADR 0021, section 2): outside every repository.
    let dir = PathBuf::from(format!("/tmp/harness-{}", nix::unistd::getuid().as_raw()));
    let supervisor = repo.supervisor(
        [
            "",
            "0123456789abcdef.sock",
            "0123456789abcdef.token",
            "0123456789abcdef.lock",
        ]
        .into_iter()
        .map(|name| dir.join(name))
        .collect(),
    );

    let receipt = repo.launch(&supervisor).unwrap();

    assert_eq!(receipt["container_id"], WORKER_ID);
    assert!(repo.engine.calls().iter().any(|argv| argv[1] == "create"));
}

#[test]
fn a_mount_of_the_runtime_or_a_changed_image_is_refused_before_any_write() {
    let repo = Repo::new("refused", 3600);
    // The runtime's own socket would lie under the storage root of the plan.
    let socket = repo
        .root
        .join(".harness/orchestration/state/runtime/x.sock");
    let supervisor = repo.supervisor(vec![socket]);

    let refused = repo.launch(&supervisor).unwrap_err();

    assert_eq!(refused.code, ErrorCode::PolicyDenied, "{refused:?}");
    assert!(
        repo.engine.calls().is_empty(),
        "the engine is not asked before the policy"
    );
    assert!(repo.execution().is_none());

    let supervisor = repo.supervisor(Vec::new());
    repo.engine.answer(
        &[
            "podman", "image", "inspect", "--format", "{{.Id}}", WORKER_TAG,
        ],
        Answer::success("c".repeat(64)),
    );
    let refused = repo.launch(&supervisor).unwrap_err();
    assert_eq!(refused.code, ErrorCode::ApprovalMismatch, "{refused:?}");
    assert!(repo.execution().is_none());
    assert!(!repo.engine.calls().iter().any(|argv| argv[1] == "create"));
}

#[test]
fn a_missing_plan_root_and_a_protected_root_are_refused_with_their_own_remedies() {
    let repo = Repo::new("remedies", 3600);
    // Like .harness/.sandboxes/scratch before `harness install`.
    let artifacts = repo.root.join(".harness/.sandboxes/artifacts");
    fs::remove_dir(&artifacts).unwrap();

    let missing = repo.launch(&repo.supervisor(Vec::new())).unwrap_err();

    assert_eq!(missing.code, ErrorCode::PolicyDenied, "{missing:?}");
    assert!(missing.message.contains("does not exist"), "{missing:?}");
    assert!(
        missing.remedy.contains(artifacts.to_str().unwrap())
            && missing.remedy.contains("harness install"),
        "the remedy names the missing path: {missing:?}"
    );
    assert!(!missing.remedy.contains("XDG_RUNTIME_DIR"), "{missing:?}");

    fs::create_dir_all(&artifacts).unwrap();
    let socket = repo
        .root
        .join(".harness/orchestration/state/runtime/x.sock");
    let exposed = repo.launch(&repo.supervisor(vec![socket])).unwrap_err();

    assert_eq!(exposed.code, ErrorCode::PolicyDenied, "{exposed:?}");
    assert!(exposed.remedy.contains("XDG_RUNTIME_DIR"), "{exposed:?}");
    assert!(!exposed.remedy.contains("harness install"), "{exposed:?}");
    assert!(repo.engine.calls().is_empty());
    assert!(repo.execution().is_none());
}

#[test]
fn container_observe_reports_the_engine_facts_and_denies_a_protected_root() {
    let repo = Repo::new("observe", 3600);
    let supervisor = repo.supervisor(Vec::new());
    let (brief, _) = executions::dispatch_records(&repo.store(), DISPATCH)
        .unwrap()
        .unwrap();
    let plan = brief["runtime_access"].clone();

    let observed = supervisor
        .container_observe(&repo.request(
            "container.observe",
            "obs-1".to_owned(),
            json!({"plan": plan}),
        ))
        .unwrap();

    assert_eq!(observed["engine"], "podman");
    assert_eq!(observed["engine_mode"], "rootless");
    assert_eq!(observed["selinux"], true);
    assert_eq!(observed["worker_image_id"], WORKER_PIN);
    assert_eq!(observed["blockers"], json!([]));

    let mut denied = plan.clone();
    denied["requirements"][0]["path"] = json!("/run/podman");
    repo.engine.clear();
    let refused = supervisor
        .container_observe(&repo.request(
            "container.observe",
            "obs-2".to_owned(),
            json!({"plan": denied}),
        ))
        .unwrap_err();
    assert_eq!(refused.code, ErrorCode::PolicyDenied);
    assert!(repo.engine.calls().is_empty());
}

const FOREIGN_ID: &str = "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210";
const PROXY_ID: &str = "89abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567";

/// The engine lists a worker of this repository, a running sidecar of it and a worker of another
/// repository; its label filter narrows nothing.
fn two_repositories(repo: &Repo, worker_status: &str) {
    let labels = |repository: &Path| {
        json!({"harness.dispatch": DISPATCH, "harness.repository": repository,
               "harness.execution": "exec-old"})
    };
    let object = |id: &str, name: String, status: &str, repository: &Path| {
        json!({"Id": id, "Name": format!("/{name}"), "State": {"Status": status},
               "Config": {"Labels": labels(repository)}})
    };
    let elsewhere = Path::new("/elsewhere/.git");
    let listed = json!([
        object(
            WORKER_ID,
            format!("harness-{DISPATCH}"),
            worker_status,
            &repo.git_common
        ),
        object(
            PROXY_ID,
            format!("harness-{DISPATCH}-proxy"),
            "running",
            &repo.git_common
        ),
        object(
            FOREIGN_ID,
            format!("harness-{DISPATCH}"),
            "exited",
            elsewhere
        ),
    ]);
    repo.engine
        .answer(
            &["podman", "ps"],
            Answer::success(format!("{WORKER_ID}\n{PROXY_ID}\n{FOREIGN_ID}\n")),
        )
        .answer(&["podman", "network", "ls"], Answer::success(""))
        .answer(
            &["podman", "container", "inspect"],
            Answer::success(listed.to_string()),
        )
        .answer(
            &["podman", "inspect", "--format", "{{.Id}}", FOREIGN_ID],
            Answer::success(FOREIGN_ID),
        )
        .answer(
            &["podman", "inspect", "--format", "{{.Id}}"],
            Answer::failure("Error: no such object"),
        );
}

#[test]
fn container_objects_lists_only_this_repository_with_the_engines_asked_and_recorded() {
    let repo = Repo::new("objects", 3600);
    let supervisor = repo.supervisor(Vec::new());
    two_repositories(&repo, "exited");

    let listed = supervisor
        .container_objects(&repo.request(
            "container.objects",
            "objects-1".to_owned(),
            json!({"engines": ["podman"]}),
        ))
        .unwrap();

    let objects = listed["objects"].as_array().unwrap();
    let ids: Vec<&str> = objects.iter().map(|o| o["id"].as_str().unwrap()).collect();
    assert_eq!(ids, [WORKER_ID, PROXY_ID]);
    assert_eq!(objects[0]["engine"], "podman");
    assert_eq!(objects[0]["role"], "worker");
    assert_eq!(objects[0]["execution"], "exec-old");
    assert_eq!(objects[0]["execution_state"], Value::Null);
    repo.only_podman();
}

#[test]
fn container_remove_refuses_a_foreign_object_and_a_running_worker_and_never_a_volume() {
    let repo = Repo::new("remove", 3600);
    let supervisor = repo.supervisor(Vec::new());
    two_repositories(&repo, "running");
    let remove = |ids: &[&str]| {
        let objects: Vec<Value> = ids
            .iter()
            .map(|id| json!({"engine": "podman", "id": id}))
            .collect();
        supervisor
            .container_remove(&repo.request(
                "container.remove",
                format!("remove-{}", ids.len()),
                json!({"objects": objects}),
            ))
            .unwrap()
    };

    let refused = remove(&[FOREIGN_ID, WORKER_ID, PROXY_ID]);

    let failed: Vec<&str> = refused["failed"]
        .as_array()
        .unwrap()
        .iter()
        .map(|f| f["object"].as_str().unwrap())
        .collect();
    assert_eq!(failed.len(), 3, "{refused}");
    assert!(failed[0].contains(FOREIGN_ID), "{refused}");
    assert!(
        !repo.engine.calls().iter().any(|argv| argv[1] == "rm"),
        "nothing is removed"
    );

    repo.engine.clear();
    two_repositories(&repo, "exited");
    let missing = "0".repeat(64);
    let done = remove(&[PROXY_ID, WORKER_ID, &missing]);

    assert_eq!(
        done["removed"],
        json!([
            format!("container harness-{DISPATCH}"),
            format!("container harness-{DISPATCH}-proxy")
        ])
    );
    assert_eq!(done["missing"], json!([missing]));
    let removals: Vec<Vec<String>> = repo
        .engine
        .calls()
        .into_iter()
        .filter(|argv| argv[1] == "rm")
        .collect();
    assert_eq!(
        removals,
        [
            vec!["podman".to_owned(), "rm".to_owned(), WORKER_ID.to_owned()],
            vec![
                "podman".to_owned(),
                "rm".to_owned(),
                "--force".to_owned(),
                PROXY_ID.to_owned()
            ],
        ]
    );
    repo.only_podman();
}

#[test]
fn container_remove_refuses_an_object_of_a_live_execution() {
    let repo = Repo::new("live", 3600);
    let supervisor = repo.supervisor(Vec::new());
    repo.launch(&supervisor).unwrap();
    let id = repo.execution().unwrap().id;
    repo.engine
        .answer(&["podman", "ps"], Answer::success(WORKER_ID))
        .answer(&["podman", "network", "ls"], Answer::success(""))
        .answer(
            &["podman", "container", "inspect", WORKER_ID],
            Answer::success(
                json!([{"Id": WORKER_ID, "Name": format!("/harness-{DISPATCH}"), "State": {"Status": "exited"},
                        "Config": {"Labels": {"harness.dispatch": DISPATCH, "harness.repository": repo.git_common,
                                              "harness.execution": id}}}])
                .to_string(),
            ),
        );
    repo.engine.clear();

    let refused = supervisor
        .container_remove(&repo.request(
            "container.remove",
            "remove-live".to_owned(),
            json!({"objects": [{"engine": "podman", "id": WORKER_ID}]}),
        ))
        .unwrap();

    assert_eq!(refused["failed"].as_array().unwrap().len(), 1, "{refused}");
    assert!(!repo.engine.calls().iter().any(|argv| argv[1] == "rm"));
}
