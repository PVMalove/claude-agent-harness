//! The engine matrix of ADR 0025 (Docker and Podman, rootful and rootless, with and without
//! SELinux) as golden argv exported from the Python `EngineMatrixChecks`, run through `Scripted`.

use engine::argv::Spec;
use engine::plan::{HostUser, Labels, Plan};
use engine::{Answer, Kind, Scripted, ops};
use serde_json::Value;

const OPERATION: &str = "op-807";
const EXECUTION: &str = "exec-807";

fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/engine_matrix.json")).expect("fixture")
}

fn text(value: &Value, key: &str) -> String {
    value[key].as_str().expect(key).to_owned()
}

fn argv_list(value: &Value) -> Vec<Vec<String>> {
    serde_json::from_value(value.clone()).expect("argv list")
}

/// The fixture's one documented delta: the operation and execution labels follow the plan digest.
fn with_execution_labels(argv: &[String]) -> Vec<String> {
    let mut out = Vec::new();
    for part in argv {
        out.push(part.clone());
        if part.starts_with("harness.plan_digest=") {
            out.extend([
                "--label".to_owned(),
                format!("harness.operation={OPERATION}"),
                "--label".to_owned(),
                format!("harness.execution={EXECUTION}"),
            ]);
        }
    }
    out
}

fn scripted(row: &Value) -> Scripted {
    let kind = Kind::parse(&text(row, "engine")).expect("engine");
    let bin = kind.as_str();
    let engine = Scripted::new(kind);
    let plan = &row["plan"]["container"];
    engine
        .answer(
            &[bin, "info"],
            Answer::success(row["info"].to_string() + "\n"),
        )
        .answer(
            &[bin, "network", "create"],
            Answer::success(text(row, "network_id") + "\n"),
        )
        .answer(&[bin, "network", "connect"], Answer::success("\n"))
        .answer(&[bin, "start"], Answer::success("started\n"));
    for key in ["worker_image", "proxy_image"] {
        let tag = text(plan, key);
        let id = text(&row["images"], &tag);
        engine.answer(
            &[bin, "image", "inspect", "--format", "{{.Id}}", &tag],
            Answer::success(id + "\n"),
        );
    }
    let fx = fixture();
    let dispatch = text(&fx, "dispatch_id");
    for (name, id) in [
        (
            format!("harness-{dispatch}-proxy"),
            text(row, "proxy_container_id"),
        ),
        (
            format!("harness-{dispatch}"),
            text(row, "worker_container_id"),
        ),
    ] {
        engine.answer(
            &[bin, "create", "--pull", "never", "--name", &name],
            Answer::success(id + "\n"),
        );
    }
    engine
}

#[test]
fn every_row_observes_and_launches_with_the_golden_argv() {
    let fx = fixture();
    let rows = fx["rows"].as_array().expect("rows");
    assert_eq!(rows.len(), 8, "Docker/Podman x rootful/rootless x SELinux");
    let user = HostUser {
        uid: u32::try_from(fx["uid"].as_u64().expect("uid")).expect("uid"),
        gid: u32::try_from(fx["gid"].as_u64().expect("gid")).expect("gid"),
    };
    let command: Vec<String> = serde_json::from_value(fx["command"].clone()).expect("command");
    for row in rows {
        let name = text(row, "name");
        let plan = Plan::parse(&row["plan"]).expect("plan");
        let engine = scripted(row);

        let observation = ops::observe(&engine, &plan);

        assert!(
            observation.blockers.is_empty(),
            "{name}: {:?}",
            observation.blockers
        );
        assert_eq!(observation.mode.as_str(), text(row, "mode"), "{name}");
        assert_eq!(Value::Bool(observation.selinux), row["selinux"], "{name}");
        assert_eq!(
            observation.environment_id,
            text(row, "environment_id"),
            "{name}"
        );
        assert_eq!(
            observation.worker_image_id.as_deref(),
            row["worker_image_id"].as_str(),
            "{name}"
        );
        assert_eq!(
            observation.proxy_image_id.as_deref(),
            row["proxy_image_id"].as_str(),
            "{name}"
        );
        assert_eq!(engine.calls(), argv_list(&row["observe_argv"]), "{name}");
        engine.clear();

        let labels = Labels {
            dispatch: text(&fx, "dispatch_id"),
            repository: text(&fx, "repository"),
            plan_digest: plan.plan_digest.clone(),
            operation: OPERATION.to_owned(),
            execution: EXECUTION.to_owned(),
        };
        let worker_pin = text(&fx["runtime_images"], "worker");
        let proxy_pin = text(&fx["runtime_images"], "proxy");
        let spec = Spec {
            plan: &plan,
            labels: &labels,
            command: &command,
            worker_image_id: &worker_pin,
            proxy_image_id: Some(&proxy_pin),
            user,
            mode: observation.mode,
            selinux: observation.selinux,
        };

        let launched = ops::launch(&engine, &spec).expect("launch");

        assert_eq!(
            launched.worker_id,
            text(row, "worker_container_id"),
            "{name}"
        );
        assert_eq!(
            launched.proxy_id.as_deref(),
            row["proxy_container_id"].as_str(),
            "{name}"
        );
        let expected: Vec<Vec<String>> = argv_list(&row["launch_argv"])
            .iter()
            .map(|argv| with_execution_labels(argv))
            .collect();
        assert_eq!(engine.calls(), expected, "{name}");
    }
}

#[test]
fn the_golden_launch_keeps_the_adr_0025_defaults() {
    let fx = fixture();
    for row in fx["rows"].as_array().expect("rows") {
        for argv in argv_list(&row["launch_argv"]) {
            assert!(!argv.iter().any(|part| part == "volume"), "{argv:?}");
            if argv[1] != "create" {
                continue;
            }
            let joined = argv.join(" ");
            for required in [
                "--pull never",
                "--cap-drop ALL",
                "--security-opt no-new-privileges",
            ] {
                assert!(joined.contains(required), "{required} missing: {joined}");
            }
            assert!(!joined.contains("--privileged"), "{joined}");
        }
    }
}
