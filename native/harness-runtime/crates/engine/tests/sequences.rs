//! The engine sequences through `Scripted`: inspect, objects, remove, launch rollback, stop and
//! logs. Every argv starts with the engine of the scripted answer; none names a volume.

use std::time::Duration;

use engine::argv::Spec;
use engine::facts::Mode;
use engine::ops::{self, ObjectKind, WorkerState};
use engine::plan::{HostUser, Labels, Plan};
use engine::{Answer, Engine, Kind, Scripted};
use serde_json::json;

const ID: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
const OTHER: &str = "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210";
const NET: &str = "89abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567";
const REPO: &str = "/work/repo/.git";

fn container(id: &str, name: &str, repo: &str, status: &str) -> serde_json::Value {
    json!({
        "Id": id, "Name": format!("/{name}"),
        "State": {"Status": status, "ExitCode": 3, "FinishedAt": "2026-10-28T10:00:00Z", "OOMKilled": false},
        "Config": {"Labels": {"harness.dispatch": "d1", "harness.repository": repo, "harness.execution": "e1"}},
    })
}

fn no_volume(engine: &Scripted) {
    for argv in engine.calls() {
        assert!(!argv.iter().any(|part| part == "volume"), "{argv:?}");
    }
}

#[test]
fn inspect_maps_the_engine_status_and_only_a_named_missing_object_is_missing() {
    let engine = Scripted::new(Kind::Podman);
    let prefix = ["podman", "container", "inspect", "--format", "{{json .}}"];
    for (status, state) in [
        ("running", WorkerState::Running),
        ("paused", WorkerState::Running),
        ("exited", WorkerState::Exited),
        ("dead", WorkerState::Exited),
        ("created", WorkerState::Unknown),
    ] {
        engine.answer(
            &prefix,
            Answer::success(container(ID, "harness-d1", REPO, status).to_string()),
        );
        let seen = ops::inspect(&engine, ID);
        assert_eq!(seen.state, state, "{status}");
        assert_eq!(seen.label("harness.execution"), Some("e1"));
    }
    assert_eq!(ops::inspect(&engine, ID).exit_code, None);
    engine.answer(
        &prefix,
        Answer::success(container(ID, "harness-d1", REPO, "exited").to_string()),
    );
    assert_eq!(ops::inspect(&engine, ID).exit_code, Some(3));
    for (answer, state) in [
        (
            Answer::failure("Error: no such container abc"),
            WorkerState::Missing,
        ),
        (
            Answer::failure("Error: container abc not found"),
            WorkerState::Missing,
        ),
        (
            Answer::failure(
                "dial unix /run/podman/podman.sock: connect: no such file or directory",
            ),
            WorkerState::Unknown,
        ),
        (
            Answer::silent("No such file or directory: podman"),
            WorkerState::Unknown,
        ),
        (Answer::success("not json"), WorkerState::Unknown),
    ] {
        engine.answer(&prefix, answer);
        assert_eq!(ops::inspect(&engine, ID).state, state);
    }
    assert!(engine.calls().iter().all(|argv| argv[0] == "podman"));
}

#[test]
fn objects_returns_only_this_repository_even_when_the_filter_narrows_nothing() {
    let engine = Scripted::new(Kind::Podman);
    // The engine ignores the label filter: both repositories' objects are listed.
    engine
        .answer(&["podman", "ps"], Answer::success(format!("{ID}\n{OTHER}\n")))
        .answer(&["podman", "network", "ls"], Answer::success(format!("{NET}\n")))
        .answer(
            &["podman", "container", "inspect"],
            Answer::success(
                json!([
                    container(ID, "harness-d1", REPO, "exited"),
                    container(OTHER, "harness-d1", "/elsewhere/.git", "exited"),
                ])
                .to_string(),
            ),
        )
        .answer(
            &["podman", "network", "inspect"],
            Answer::success(
                json!([{"Id": NET, "Name": "harness-d1-net",
                        "Labels": {"harness.dispatch": "d1", "harness.repository": "/elsewhere/.git"}}])
                .to_string(),
            ),
        );

    let found = ops::objects(&engine, REPO, None).expect("objects");

    assert_eq!(found.len(), 1, "{found:?}");
    assert_eq!(
        (found[0].id.as_str(), found[0].role.as_str()),
        (ID, "worker")
    );
    assert_eq!(found[0].execution.as_deref(), Some("e1"));
    assert_eq!(found[0].engine, Kind::Podman);
    assert_eq!(
        engine.calls()[0],
        [
            "podman",
            "ps",
            "--all",
            "--quiet",
            "--no-trunc",
            "--filter",
            "label=harness.repository=/work/repo/.git"
        ]
    );
    no_volume(&engine);
    engine.answer(&["podman", "ps"], Answer::failure("cannot connect"));
    assert_eq!(ops::objects(&engine, REPO, None), None);
}

#[test]
fn remove_goes_worker_sidecar_network_and_stops_at_a_running_worker() {
    let engine = Scripted::new(Kind::Podman);
    let object = |id: &str, role: &str, kind| ops::Object {
        engine: Kind::Podman,
        kind,
        id: id.to_owned(),
        name: role.to_owned(),
        dispatch: "d1".to_owned(),
        role: role.to_owned(),
        state: "exited".to_owned(),
        execution: None,
    };
    let objects = [
        object(NET, "network", ObjectKind::Network),
        object(OTHER, "proxy", ObjectKind::Container),
        object(ID, "worker", ObjectKind::Container),
    ];
    engine
        .answer(&["podman", "rm"], Answer::success("ok"))
        .answer(
            &["podman", "network", "rm"],
            Answer::failure("Error: no such network x"),
        );

    let removal = ops::remove(&engine, &objects);

    assert_eq!(removal.removed, ["container worker", "container proxy"]);
    assert_eq!(removal.missing, ["network network"]);
    assert_eq!(
        engine.calls(),
        [
            vec!["podman", "rm", ID],
            vec!["podman", "rm", "--force", OTHER],
            vec!["podman", "network", "rm", NET],
        ]
    );

    engine.clear();
    engine.answer(
        &["podman", "rm", ID],
        Answer::failure("container is running"),
    );
    let removal = ops::remove(&engine, &objects);
    assert_eq!(removal.failed.len(), 3, "{removal:?}");
    assert_eq!(engine.calls(), [vec!["podman", "rm", ID]]);
}

#[test]
fn a_failed_stage_rolls_back_every_attempted_object_newest_first() {
    let plan = Plan::parse(&json!({
        "container": {"engine": "podman", "worker_image": "w:1", "proxy_image": "p:1"},
        "network": {"hosts": ["api.example.com"]},
        "requirements": [{"resource": "checkout", "path": "/work/repo", "access": "read"}],
        "plan_digest": "d",
    }))
    .expect("plan");
    let labels = Labels {
        dispatch: "d1".to_owned(),
        repository: REPO.to_owned(),
        plan_digest: "d".to_owned(),
        operation: "o".to_owned(),
        execution: "e".to_owned(),
    };
    let command = vec!["/adapter".to_owned()];
    let spec = Spec {
        plan: &plan,
        labels: &labels,
        command: &command,
        worker_image_id: "sha256:w",
        proxy_image_id: Some("sha256:p"),
        user: HostUser { uid: 1, gid: 1 },
        mode: Mode::Rootless,
        selinux: false,
    };
    let engine = Scripted::new(Kind::Podman);
    engine
        .answer(&["podman", "network"], Answer::success(NET))
        .answer(&["podman", "create"], Answer::success(OTHER))
        .answer(&["podman", "start"], Answer::success(""))
        .answer(&["podman", "start", ID], Answer::failure("cannot start"))
        .answer(
            &[
                "podman",
                "create",
                "--pull",
                "never",
                "--name",
                "harness-d1",
            ],
            Answer::success(ID),
        )
        .answer(&["podman", "rm"], Answer::success(""));

    let failure = ops::launch(&engine, &spec).expect_err("start fails");

    assert_eq!(failure.detail, "podman start failed: cannot start");
    assert_eq!(
        ops::planned_objects(&plan, "d1"),
        failure.objects,
        "every planned object was attempted"
    );
    let calls = engine.calls();
    assert_eq!(
        calls[calls.len() - 3..],
        [
            vec!["podman", "rm", "--force", "harness-d1"],
            vec!["podman", "rm", "--force", "harness-d1-proxy"],
            vec!["podman", "network", "rm", "harness-d1-net"],
        ]
    );
    assert_eq!(failure.removals.len(), 3);
    no_volume(&engine);
}

#[test]
fn stop_kill_and_logs_use_the_engine_of_the_row() {
    let engine = Scripted::new(Kind::Podman);
    engine
        .answer(&["podman", "stop"], Answer::success(ID))
        .answer(&["podman", "kill"], Answer::success(ID))
        .answer(
            &["podman", "logs"],
            Answer {
                answered: true,
                code: Some(0),
                stdout: "out\n".to_owned(),
                stderr: "err\n".to_owned(),
            },
        );
    let dir = std::env::temp_dir().join(format!("engine-logs-{}", std::process::id()));
    std::fs::create_dir_all(&dir).expect("dir");

    assert!(ops::stop(&engine, ID, Duration::from_secs(7)).0);
    assert!(ops::kill(&engine, ID).0);
    ops::logs(
        &engine,
        ID,
        &dir.join("stdout.log"),
        &dir.join("stderr.log"),
    )
    .expect("logs");

    assert_eq!(
        std::fs::read_to_string(dir.join("stdout.log")).expect("out"),
        "out\n"
    );
    assert_eq!(
        std::fs::read_to_string(dir.join("stderr.log")).expect("err"),
        "err\n"
    );
    assert_eq!(
        engine.calls(),
        [
            vec!["podman", "stop", "--time", "7", ID],
            vec!["podman", "kill", ID],
            vec!["podman", "logs", ID],
        ]
    );
    std::fs::remove_dir_all(dir).expect("cleanup");
}

#[test]
fn the_cli_engine_runs_only_its_own_binary() {
    let answer = engine::Docker.run(
        &["podman".to_owned(), "info".to_owned()],
        Duration::from_secs(1),
    );
    assert!(!answer.answered, "{answer:?}");
}
