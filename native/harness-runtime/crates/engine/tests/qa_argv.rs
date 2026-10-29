//! The QA run of ADR 0022 and 0023 at the engine boundary: the exact foreground `run --rm --init`
//! of one QA command, the run-wide sidecar and network stage, the QA object names in the object
//! listing, and the attached runs of the scripted engine.

use std::fs::File;
use std::path::PathBuf;
use std::time::Duration;

use engine::argv::{self, QaCommand, QaRun, Spec};
use engine::facts::Mode;
use engine::ops::{self, ObjectKind};
use engine::plan::{HostUser, Labels, Plan};
use engine::{Answer, Attached, Engine, Kind, Run, Scripted};
use serde_json::json;

const PIN: &str = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const ID: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
const NET: &str = "89abcdef0123456789abcdef0123456789abcdef0123456789abcdef01234567";

fn labels() -> Labels {
    Labels {
        dispatch: "d1".to_owned(),
        repository: "/work/repo/.git".to_owned(),
        plan_digest: "digest".to_owned(),
        operation: "qa-op".to_owned(),
        execution: "qa-op".to_owned(),
    }
}

fn strings(parts: &[&str]) -> Vec<String> {
    parts.iter().map(|part| (*part).to_owned()).collect()
}

fn run<'a>(
    labels: &'a Labels,
    hosts: bool,
    environment: &'a [String],
    settings: &'a [(String, String)],
    mounts: &'a [(String, bool)],
) -> QaRun<'a> {
    QaRun {
        engine: Kind::Podman,
        base: "harness-d1-qa2",
        hosts,
        environment,
        settings,
        labels,
        workdir: "/work/runs/qa/run-1/checkout",
        mounts,
        image_id: PIN,
        user: HostUser {
            uid: 1000,
            gid: 1000,
        },
        mode: Mode::Rootless,
        selinux: true,
    }
}

#[test]
fn a_shell_command_runs_in_a_foreground_rm_init_container_from_the_pinned_id() {
    let labels = labels();
    let environment = strings(&["PIP_INDEX_URL"]);
    let settings = vec![("TMPDIR".to_owned(), "/work/runs/qa/run-1/tmp".to_owned())];
    let mounts = vec![
        ("/work/repo/.git".to_owned(), false),
        ("/work/runs".to_owned(), true),
    ];
    let qa = run(&labels, false, &environment, &settings, &mounts);

    let argv = argv::qa_run_argv(&qa, "gate-1", &QaCommand::Shell("make lint".to_owned()));

    let mut expected = strings(&[
        "podman",
        "run",
        "--rm",
        "--init",
        "--pull",
        "never",
        "--name",
        "harness-d1-qa2-gate-1",
        "--network",
        "none",
        "--userns=keep-id",
        "--security-opt",
        "label=disable",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        "/harness-home:rw,exec,nosuid,nodev,mode=1777",
        "--env",
        "HOME=/harness-home",
        "--env",
        "GIT_CONFIG_COUNT=1",
        "--env",
        "GIT_CONFIG_KEY_0=safe.directory",
        "--env",
        "GIT_CONFIG_VALUE_0=*",
        "--env",
        "HARNESS_EXECUTOR=docker",
        "--env",
        "PIP_INDEX_URL",
        "--env",
        "TMPDIR=/work/runs/qa/run-1/tmp",
        "--label",
        "harness.dispatch=d1",
        "--label",
        "harness.repository=/work/repo/.git",
        "--label",
        "harness.plan_digest=digest",
        "--label",
        "harness.operation=qa-op",
        "--label",
        "harness.execution=qa-op",
        "--workdir",
        "/work/runs/qa/run-1/checkout",
        "--mount",
        "type=bind,source=/work/runs,target=/work/runs",
        "--mount",
        "type=bind,source=/work/repo/.git,target=/work/repo/.git,readonly",
        "--entrypoint",
        "/bin/sh",
    ]);
    expected.extend(strings(&[PIN, "-c", "make lint"]));
    assert_eq!(argv, expected);
    for forbidden in ["--privileged", "volume", "-d", "--detach", "--restart"] {
        assert!(!argv.iter().any(|part| part == forbidden), "{forbidden}");
    }
    assert!(!argv.iter().any(|part| part.contains(".sock")));
}

#[test]
fn an_argv_command_keeps_its_words_and_hosts_route_through_the_run_sidecar() {
    let labels = labels();
    let qa = run(&labels, true, &[], &[], &[]);

    let argv = argv::qa_run_argv(
        &qa,
        "probe-1",
        &QaCommand::Argv(strings(&["python3", "-c", "print(1)"])),
    );

    let at = |flag: &str| argv.iter().position(|part| part == flag).expect(flag);
    assert_eq!(argv[at("--network") + 1], "harness-d1-qa2-net");
    assert!(argv.contains(&"HTTPS_PROXY=http://harness-d1-qa2-proxy:3128".to_owned()));
    assert!(argv.contains(&"NO_PROXY=localhost,127.0.0.1,::1".to_owned()));
    assert_eq!(
        argv[at("--entrypoint")..],
        strings(&["--entrypoint", "python3", PIN, "-c", "print(1)"])
    );
    assert_eq!(argv::qa_base("d1", 2), "harness-d1-qa2");
}

fn plan(hosts: &[&str]) -> Plan {
    Plan::parse(&json!({
        "container": {"engine": "podman", "worker_image": "w:1", "proxy_image": "p:1"},
        "network": {"hosts": hosts},
        "requirements": [{"resource": "checkout", "path": "/work/repo", "access": "read"}],
        "plan_digest": "digest",
    }))
    .expect("plan")
}

fn spec<'a>(plan: &'a Plan, labels: &'a Labels) -> Spec<'a> {
    Spec {
        plan,
        labels,
        command: &[],
        worker_image_id: PIN,
        proxy_image_id: Some("sha256:p"),
        user: HostUser { uid: 1, gid: 1 },
        mode: Mode::Rootless,
        selinux: false,
    }
}

#[test]
fn the_run_sidecar_is_one_network_and_one_proxy_named_after_the_run() {
    let plan = plan(&["pypi.org"]);
    let labels = labels();
    let engine = Scripted::new(Kind::Podman);
    engine
        .answer(&["podman", "network"], Answer::success(NET))
        .answer(&["podman", "create"], Answer::success(ID))
        .answer(&["podman", "start"], Answer::success(""));

    let sidecar = ops::sidecar(&engine, &spec(&plan, &labels), "harness-d1-qa2").expect("sidecar");

    assert_eq!(sidecar.proxy_id.as_deref(), Some(ID));
    assert_eq!(
        sidecar.objects,
        vec![
            (ObjectKind::Network, "harness-d1-qa2-net".to_owned()),
            (ObjectKind::Container, "harness-d1-qa2-proxy".to_owned()),
        ]
    );
    let calls = engine.calls();
    assert_eq!(calls.len(), 4, "{calls:?}");
    assert_eq!(
        calls[0][..4],
        strings(&["podman", "network", "create", "--internal"])
    );
    assert_eq!(
        calls[0].last().map(String::as_str),
        Some("harness-d1-qa2-net")
    );
    assert_eq!(
        calls[1][..6],
        strings(&[
            "podman",
            "create",
            "--pull",
            "never",
            "--name",
            "harness-d1-qa2-proxy"
        ])
    );
    assert!(calls[1].contains(&"HARNESS_EGRESS_ALLOWED_HOSTS=pypi.org".to_owned()));
    assert_eq!(
        calls[2],
        strings(&["podman", "network", "connect", "podman", ID])
    );
    assert_eq!(calls[3], strings(&["podman", "start", ID]));
}

#[test]
fn an_offline_run_has_no_sidecar_and_a_failed_sidecar_is_rolled_back() {
    let labels = labels();
    let offline = plan(&[]);
    let engine = Scripted::new(Kind::Podman);

    let sidecar = ops::sidecar(&engine, &spec(&offline, &labels), "harness-d1-qa2").expect("none");

    assert_eq!(sidecar.proxy_id, None);
    assert!(sidecar.objects.is_empty());
    assert!(engine.calls().is_empty());

    let online = plan(&["pypi.org"]);
    engine
        .answer(&["podman", "network"], Answer::success(NET))
        .answer(&["podman", "create"], Answer::success(ID))
        .answer(&["podman", "start"], Answer::failure("cannot start"))
        .answer(&["podman", "rm"], Answer::success(""));
    let failure =
        ops::sidecar(&engine, &spec(&online, &labels), "harness-d1-qa2").expect_err("start fails");
    assert_eq!(failure.removals.len(), 2);
    let calls = engine.calls();
    assert_eq!(
        calls[calls.len() - 2..],
        [
            strings(&["podman", "rm", "--force", "harness-d1-qa2-proxy"]),
            strings(&["podman", "network", "rm", "harness-d1-qa2-net"]),
        ]
    );
}

#[test]
fn the_object_listing_names_the_roles_of_a_qa_run() {
    let engine = Scripted::new(Kind::Podman);
    let repo = "/work/repo/.git";
    let item = |id: &str, name: &str| {
        json!({"Id": id, "Name": format!("/{name}"), "State": {"Status": "running"},
               "Config": {"Labels": {"harness.dispatch": "d1", "harness.repository": repo,
                                     "harness.execution": "qa-op"}}})
    };
    engine
        .answer(&["podman", "ps"], Answer::success(format!("{ID}\n{NET}")))
        .answer(&["podman", "network", "ls"], Answer::success("n1"))
        .answer(
            &["podman", "container", "inspect"],
            Answer::success(
                json!([
                    item(ID, "harness-d1-qa2-gate-1"),
                    item(NET, "harness-d1-qa2-proxy")
                ])
                .to_string(),
            ),
        )
        .answer(
            &["podman", "network", "inspect"],
            Answer::success(
                json!([{"Id": "n1", "Name": "harness-d1-qa2-net",
                        "Labels": {"harness.dispatch": "d1", "harness.repository": repo,
                                   "harness.execution": "qa-op"}}])
                .to_string(),
            ),
        );

    let found = ops::objects(&engine, repo, Some("d1")).expect("objects");

    let roles: Vec<(&str, &str)> = found
        .iter()
        .map(|object| (object.name.as_str(), object.role.as_str()))
        .collect();
    assert_eq!(
        roles,
        [
            ("harness-d1-qa2-gate-1", "worker"),
            ("harness-d1-qa2-proxy", "proxy"),
            ("harness-d1-qa2-net", "network"),
        ]
    );
}

fn streams(name: &str) -> (PathBuf, File, File) {
    let dir = std::env::temp_dir().join(format!("engine-attached-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("dir");
    let out = File::create(dir.join("out")).expect("out");
    let err = File::create(dir.join("err")).expect("err");
    (dir, out, err)
}

fn wait(run: &mut Box<dyn Attached>) -> Option<i32> {
    for _ in 0..200 {
        if let Some(exit) = run.try_wait().expect("wait") {
            return exit.code;
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    panic!("the attached run did not end");
}

#[test]
fn a_scripted_attached_run_writes_both_streams_and_ends_with_its_code() {
    let engine = Scripted::new(Kind::Podman);
    engine.attach(
        &[
            "podman",
            "run",
            "--rm",
            "--init",
            "--pull",
            "never",
            "--name",
            "harness-d1-qa2-gate-1",
        ],
        Run::finish("out\n", "err\n", 3),
    );
    let (dir, out, err) = streams("finish");

    let mut attached = engine
        .spawn(
            &strings(&[
                "podman",
                "run",
                "--rm",
                "--init",
                "--pull",
                "never",
                "--name",
                "harness-d1-qa2-gate-1",
            ]),
            out,
            err,
        )
        .expect("spawn");

    assert_eq!(wait(&mut attached), Some(3));
    assert_eq!(
        std::fs::read_to_string(dir.join("out")).expect("out"),
        "out\n"
    );
    assert_eq!(
        std::fs::read_to_string(dir.join("err")).expect("err"),
        "err\n"
    );
    let (_, out, err) = streams("unscripted");
    assert!(
        engine
            .spawn(&strings(&["podman", "run", "x"]), out, err)
            .is_err()
    );
    std::fs::remove_dir_all(dir).expect("cleanup");
}

#[test]
fn a_hanging_scripted_run_ends_only_when_its_container_is_stopped_or_killed() {
    let engine = Scripted::new(Kind::Podman);
    engine
        .attach(&["podman", "run"], Run::until_stopped("partial\n", ""))
        .answer(&["podman", "stop"], Answer::success(""))
        .answer(&["podman", "kill"], Answer::success(""));
    let argv = |name: &str| strings(&["podman", "run", "--rm", "--name", name]);

    let (dir, out, err) = streams("hang");
    let mut stopped = engine
        .spawn(&argv("harness-d1-qa2-a"), out, err)
        .expect("spawn");
    let (_, out, err) = streams("hang-kill");
    let mut killed = engine
        .spawn(&argv("harness-d1-qa2-b"), out, err)
        .expect("spawn");
    let (_, out, err) = streams("hang-cli");
    let mut cli = engine
        .spawn(&argv("harness-d1-qa2-c"), out, err)
        .expect("spawn");

    assert_eq!(stopped.try_wait().expect("wait"), None);
    assert!(ops::stop(&engine, "harness-d1-qa2-a", Duration::from_secs(1)).0);
    assert!(ops::kill(&engine, "harness-d1-qa2-b").0);
    cli.kill();

    assert_eq!(wait(&mut stopped), Some(143));
    assert_eq!(wait(&mut killed), Some(137));
    assert_eq!(wait(&mut cli), None, "a killed CLI has no exit code");
    assert_eq!(
        std::fs::read_to_string(dir.join("out")).expect("out"),
        "partial\n"
    );
}

#[test]
fn the_cli_engine_attaches_only_its_own_binary() {
    let (dir, out, err) = streams("cli");
    let refused = engine::Docker.spawn(&strings(&["podman", "run"]), out, err);
    assert!(refused.is_err());
    std::fs::remove_dir_all(dir).expect("cleanup");
}
