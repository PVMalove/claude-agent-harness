//! The sequential runner of a QA run through the scripted engine: one foreground container per
//! command, separate stream files with their sha256, the real exit code, the command groups and
//! `not_run` for every command the run never reached, the deadline and the explicit stop.

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::Duration;

use contracts::FailureDomain;
use engine::argv::QaRun;
use engine::facts::Mode;
use engine::plan::{HostUser, Labels};
use engine::{Answer, Kind, Run, Scripted};
use qa::plan::RunPlan;
use qa::runner::{Limits, Runner, StoppedBy};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

const PIN: &str = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

fn labels() -> Labels {
    Labels {
        dispatch: "d1".to_owned(),
        repository: "/work/repo/.git".to_owned(),
        plan_digest: "digest".to_owned(),
        operation: "qa-op".to_owned(),
        execution: "qa-op".to_owned(),
    }
}

fn directory(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("qa-runner-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    dir
}

fn limits(timeout_ms: u64) -> Limits {
    Limits {
        command_timeout: Duration::from_millis(timeout_ms),
        stop_grace: Duration::from_secs(1),
        poll: Duration::from_millis(5),
        end_wait: Duration::from_millis(200),
    }
}

fn plan(groups: &Value) -> RunPlan {
    RunPlan::parse(groups).expect("plan")
}

fn sha256(text: &str) -> String {
    Sha256::digest(text.as_bytes())
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn container(stage: &str) -> String {
    format!("harness-d1-qa1-{stage}")
}

fn run_prefix(stage: &str) -> Vec<String> {
    [
        "podman", "run", "--rm", "--init", "--pull", "never", "--name",
    ]
    .iter()
    .map(|part| (*part).to_owned())
    .chain([container(stage)])
    .collect()
}

fn attach(engine: &Scripted, stage: &str, run: Run) {
    let prefix = run_prefix(stage);
    let parts: Vec<&str> = prefix.iter().map(String::as_str).collect();
    engine.attach(&parts, run);
}

fn runner<'a>(
    engine: &'a Scripted,
    qa: &'a QaRun<'a>,
    dir: &'a Path,
    limits: Limits,
) -> Runner<'a> {
    Runner {
        engine,
        run: qa,
        directory: dir,
        limits,
        junit_dir: None,
    }
}

fn qa_run(labels: &Labels) -> QaRun<'_> {
    QaRun {
        engine: Kind::Podman,
        base: "harness-d1-qa1",
        hosts: false,
        environment: &[],
        settings: &[],
        labels,
        workdir: "/work/runs/qa/checkout",
        mounts: &[],
        image_id: PIN,
        user: HostUser { uid: 1, gid: 1 },
        mode: Mode::Rootless,
        selinux: false,
    }
}

#[test]
fn a_failed_gate_command_leaves_the_rest_of_its_group_not_run_with_real_codes_and_streams() {
    let engine = Scripted::new(Kind::Podman);
    attach(&engine, "gate-1", Run::finish("lint ok\n", "", 0));
    attach(&engine, "gate-2", Run::finish("", "2 failed\n", 1));
    let labels = labels();
    let qa = qa_run(&labels);
    let dir = directory("gate");
    let plan = plan(&json!([
        {"group": "gate", "run_when": "after-success", "stop_on_failure": true, "commands": [
            {"stage_id": "gate-1", "command": "make lint"},
            {"stage_id": "gate-2", "command": ["python3", "-m", "pytest", "-q"]},
            {"stage_id": "gate-3", "command": "make docs"},
        ]},
    ]));
    let stop = AtomicBool::new(false);

    let outcome = runner(&engine, &qa, &dir, limits(5_000)).run(&plan, &stop, |_| {});

    assert_eq!(outcome.stopped_by, None);
    let shown: Vec<(String, String, Option<i32>)> = outcome
        .commands
        .iter()
        .map(|item| {
            (
                item.stage_id.clone(),
                item.result.to_owned(),
                item.exit_code,
            )
        })
        .collect();
    assert_eq!(
        shown,
        [
            ("gate-1".to_owned(), "ran".to_owned(), Some(0)),
            ("gate-2".to_owned(), "ran".to_owned(), Some(1)),
            ("gate-3".to_owned(), "not_run".to_owned(), None),
        ]
    );
    let second = &outcome.commands[1];
    assert_eq!(
        second.domain, None,
        "a failed test is the command's own result"
    );
    let stderr = second.stderr.as_ref().expect("stderr");
    assert_eq!(
        std::fs::read_to_string(&stderr.path).expect("read"),
        "2 failed\n"
    );
    assert_eq!(stderr.sha256, sha256("2 failed\n"));
    assert_eq!(stderr.size, 9);
    let stdout = second.stdout.as_ref().expect("stdout");
    assert_ne!(stdout.path, stderr.path, "the streams are separate files");
    assert_eq!(stdout.sha256, sha256(""));
    let record: Value = serde_json::from_str(
        &std::fs::read_to_string(dir.join("001.json")).expect("the record of the command"),
    )
    .expect("json");
    assert_eq!(record["exit_code"], 1);
    assert_eq!(record["command"], json!(["python3", "-m", "pytest", "-q"]));
    assert!(
        !dir.join("002.json").exists(),
        "a command that never ran has no record"
    );
    assert_eq!(
        engine.calls().len(),
        2,
        "one container per command that ran"
    );
    assert!(outcome.commands[2].stdout.is_none());
    let json = outcome.to_json();
    assert_eq!(json["commands"][2]["result"], "not_run");
}

#[test]
fn a_failed_preparation_skips_the_gate_and_runs_the_diagnostic_groups() {
    let engine = Scripted::new(Kind::Podman);
    attach(&engine, "prep-1", Run::finish("", "no network\n", 2));
    attach(&engine, "probe-1", Run::finish("", "", 1));
    attach(&engine, "check-1", Run::finish("", "", 0));
    let labels = labels();
    let qa = qa_run(&labels);
    let dir = directory("groups");
    let plan = plan(&json!([
        {"group": "preparation", "run_when": "always", "stop_on_failure": true,
         "commands": [{"stage_id": "prep-1", "command": "make bootstrap"},
                      {"stage_id": "prep-2", "command": "make other"}]},
        {"group": "probes", "run_when": "after-failure", "stop_on_failure": false,
         "commands": [{"stage_id": "probe-1", "command": "probe"}]},
        {"group": "checks", "run_when": "after-failure", "stop_on_failure": false,
         "commands": [{"stage_id": "check-1", "command": "uv lock --check --offline"}]},
        {"group": "gate", "run_when": "after-success", "stop_on_failure": true,
         "commands": [{"stage_id": "gate-1", "command": "make lint"}]},
    ]));
    let stop = AtomicBool::new(false);
    let mut seen = Vec::new();

    let outcome = runner(&engine, &qa, &dir, limits(5_000))
        .run(&plan, &stop, |record| seen.push(record.stage_id.clone()));

    let results: Vec<(&str, &str)> = outcome
        .commands
        .iter()
        .map(|item| (item.stage_id.as_str(), item.result))
        .collect();
    assert_eq!(
        results,
        [
            ("prep-1", "ran"),
            ("prep-2", "not_run"),
            ("probe-1", "ran"),
            ("check-1", "ran"),
            ("gate-1", "not_run"),
        ]
    );
    assert_eq!(seen, ["prep-1", "probe-1", "check-1"]);
    assert_eq!(outcome.commands[0].group, "preparation");
}

#[test]
fn a_command_past_its_deadline_is_stopped_with_its_grace_and_is_a_timeout() {
    let engine = Scripted::new(Kind::Podman);
    attach(&engine, "gate-1", Run::until_stopped("partial\n", ""));
    engine.answer(&["podman", "stop"], Answer::success(""));
    let labels = labels();
    let qa = qa_run(&labels);
    let dir = directory("deadline");
    let plan = plan(&json!([
        {"group": "gate", "run_when": "after-success", "stop_on_failure": true, "commands": [
            {"stage_id": "gate-1", "command": "sleep 100"},
            {"stage_id": "gate-2", "command": "make lint"},
        ]},
    ]));
    let stop = AtomicBool::new(false);

    let outcome = runner(&engine, &qa, &dir, limits(30)).run(&plan, &stop, |_| {});

    let first = &outcome.commands[0];
    assert_eq!(
        first.exit_code,
        Some(143),
        "the real code of the stopped container"
    );
    assert_eq!(first.domain, Some(FailureDomain::Timeout));
    assert_eq!(first.stopped_by, Some(StoppedBy::Deadline));
    assert_eq!(outcome.commands[1].result, "not_run");
    assert_eq!(outcome.stopped_by, Some(StoppedBy::Deadline));
    let stop_call = engine
        .calls()
        .into_iter()
        .find(|argv| argv[1] == "stop")
        .expect("the container was stopped");
    assert_eq!(
        stop_call,
        ["podman", "stop", "--time", "1", &container("gate-1")]
    );
}

#[test]
fn an_explicit_stop_ends_the_running_container_and_every_later_command_is_not_run() {
    let engine = Arc::new(Scripted::new(Kind::Podman));
    attach(&engine, "prep-1", Run::finish("", "", 0));
    attach(&engine, "gate-1", Run::until_killed("half\n", ""));
    engine
        .answer(&["podman", "stop"], Answer::success(""))
        .answer(&["podman", "kill"], Answer::success(""));
    let labels = labels();
    let qa = qa_run(&labels);
    let dir = directory("stop");
    let plan = plan(&json!([
        {"group": "preparation", "run_when": "always", "stop_on_failure": true,
         "commands": [{"stage_id": "prep-1", "command": "make bootstrap"}]},
        {"group": "probes", "run_when": "after-failure", "stop_on_failure": false,
         "commands": [{"stage_id": "probe-1", "command": "probe"}]},
        {"group": "gate", "run_when": "after-success", "stop_on_failure": true, "commands": [
            {"stage_id": "gate-1", "command": "make verify"},
            {"stage_id": "gate-2", "command": "make lint"},
        ]},
    ]));
    let stop = Arc::new(AtomicBool::new(false));
    let flag = Arc::clone(&stop);
    let stopper = thread::spawn(move || {
        thread::sleep(Duration::from_millis(50));
        flag.store(true, Ordering::SeqCst);
    });

    let outcome = runner(&engine, &qa, &dir, limits(60_000)).run(&plan, &stop, |_| {});
    stopper.join().expect("stopper");

    let gate = &outcome.commands[2];
    assert_eq!(gate.stage_id, "gate-1");
    assert_eq!(
        gate.exit_code,
        Some(137),
        "the stop was ignored, the kill ended it"
    );
    assert_eq!(gate.domain, Some(FailureDomain::Cancelled));
    assert_eq!(outcome.stopped_by, Some(StoppedBy::Command));
    assert_eq!(outcome.commands[1].result, "not_run");
    assert_eq!(outcome.commands[3].result, "not_run");
    let verbs: Vec<String> = engine
        .calls()
        .into_iter()
        .filter(|argv| argv[1] != "run")
        .map(|argv| argv[1].clone())
        .collect();
    assert_eq!(verbs, ["stop", "kill"]);
    assert_eq!(
        std::fs::read_to_string(&gate.stdout.as_ref().expect("stdout").path).expect("read"),
        "half\n",
        "the partial output is kept"
    );
}

#[test]
fn a_cli_that_cannot_start_is_infrastructure_without_an_exit_code() {
    let engine = Scripted::new(Kind::Podman);
    let labels = labels();
    let qa = qa_run(&labels);
    let dir = directory("cli");
    let plan = plan(&json!([
        {"group": "gate", "run_when": "after-success", "stop_on_failure": true,
         "commands": [{"stage_id": "gate-1", "command": "make lint"}]},
    ]));
    let stop = AtomicBool::new(false);

    let outcome = runner(&engine, &qa, &dir, limits(5_000)).run(&plan, &stop, |_| {});

    let first = &outcome.commands[0];
    assert_eq!(first.exit_code, None);
    assert_eq!(first.domain, Some(FailureDomain::Infrastructure));
    assert!(
        first
            .detail
            .as_deref()
            .is_some_and(|text| text.contains("no scripted run"))
    );
}

#[test]
fn a_plan_refuses_unsafe_stage_ids_duplicates_and_empty_commands() {
    let group = |stage: &str, command: Value| {
        json!([{"group": "gate", "run_when": "after-success", "stop_on_failure": true,
                "commands": [{"stage_id": stage, "command": command}]}])
    };
    assert!(RunPlan::parse(&group("gate-1", json!("make lint"))).is_ok());
    for stage in ["", "Gate", "net", "proxy", "a b", "-x", "a/b"] {
        assert!(
            RunPlan::parse(&group(stage, json!("x"))).is_err(),
            "{stage:?}"
        );
    }
    for command in [json!(""), json!([]), json!([1]), json!(null)] {
        assert!(
            RunPlan::parse(&group("gate-1", command.clone())).is_err(),
            "{command}"
        );
    }
    let duplicate = json!([{"group": "gate", "run_when": "after-success", "stop_on_failure": true,
        "commands": [{"stage_id": "a", "command": "x"}, {"stage_id": "a", "command": "y"}]}]);
    assert!(RunPlan::parse(&duplicate).is_err());
    let unknown = json!([{"group": "gate", "run_when": "sometimes", "stop_on_failure": true,
        "commands": [{"stage_id": "a", "command": "x"}]}]);
    assert!(RunPlan::parse(&unknown).is_err());
    assert!(RunPlan::parse(&json!([])).is_err(), "a run has a command");
}
