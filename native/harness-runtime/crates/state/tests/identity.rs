//! The identity of a process and the verdict on its liveness (ADR 0020, section 7).

use state::identity::{self, Identity, Observer, Probe, Verdict, parse_stat, verdict};

fn here() -> Observer {
    Observer {
        host: "host-a".to_owned(),
        boot_id: Some("boot-a".to_owned()),
        pid_namespace: Some("pid:[4026531836]".to_owned()),
    }
}

fn recorded() -> Identity {
    Identity {
        host: "host-a".to_owned(),
        boot_id: Some("boot-a".to_owned()),
        pid_namespace: Some("pid:[4026531836]".to_owned()),
        pid: 4242,
        start_time: "1000".to_owned(),
    }
}

#[test]
fn the_verdict_table() {
    let running = |start: &str| Probe::Running(Some(start.to_owned()));
    let other_host = Identity {
        host: "host-b".to_owned(),
        ..recorded()
    };
    let other_boot = Identity {
        boot_id: Some("boot-b".to_owned()),
        ..recorded()
    };
    let other_namespace = Identity {
        pid_namespace: Some("pid:[4026532000]".to_owned()),
        ..recorded()
    };
    let no_pid = Identity {
        pid: 0,
        ..recorded()
    };
    let half_known = Identity {
        boot_id: None,
        ..recorded()
    };
    let cases: [(Option<&Identity>, Probe, Verdict); 13] = [
        (Some(&recorded()), running("1000"), Verdict::Alive),
        (Some(&recorded()), Probe::Absent, Verdict::Dead),
        (Some(&recorded()), running("2000"), Verdict::PidReused),
        (Some(&recorded()), Probe::Running(None), Verdict::Unknown),
        (Some(&recorded()), Probe::Unknown, Verdict::Unknown),
        // Another host, boot or namespace is never judged by the pid: not even an absent one.
        (Some(&other_host), Probe::Absent, Verdict::OnAnotherHost),
        (Some(&other_boot), Probe::Absent, Verdict::OtherEnvironment),
        (
            Some(&other_namespace),
            Probe::Absent,
            Verdict::OtherEnvironment,
        ),
        (
            Some(&other_namespace),
            running("1000"),
            Verdict::OtherEnvironment,
        ),
        // A value only one side knows cannot prove the same environment.
        (Some(&half_known), Probe::Absent, Verdict::Unknown),
        (Some(&no_pid), Probe::Absent, Verdict::Unknown),
        (None, Probe::Absent, Verdict::Unknown),
        (None, running("1000"), Verdict::Unknown),
    ];
    for (owner, probe, expected) in cases {
        assert_eq!(
            verdict(owner, &here(), &probe),
            expected,
            "{owner:?} {probe:?}"
        );
    }
}

#[test]
fn a_platform_that_reveals_no_environment_judges_by_the_pid_and_the_start_time() {
    let observer = Observer {
        host: "host-a".to_owned(),
        boot_id: None,
        pid_namespace: None,
    };
    let owner = Identity {
        boot_id: None,
        pid_namespace: None,
        ..recorded()
    };
    assert_eq!(
        verdict(Some(&owner), &observer, &Probe::Absent),
        Verdict::Dead
    );
    assert_eq!(
        verdict(
            Some(&owner),
            &observer,
            &Probe::Running(Some("1000".to_owned()))
        ),
        Verdict::Alive
    );
}

#[test]
fn every_verdict_has_its_wire_name() {
    let names: Vec<&str> = [
        Verdict::Alive,
        Verdict::Dead,
        Verdict::PidReused,
        Verdict::OtherEnvironment,
        Verdict::OnAnotherHost,
        Verdict::Unknown,
    ]
    .iter()
    .map(|verdict| verdict.as_str())
    .collect();
    assert_eq!(
        names,
        [
            "owner-alive",
            "owner-dead",
            "owner-pid-reused",
            "owner-in-other-environment",
            "owner-on-another-host",
            "owner-unknown",
        ]
    );
}

#[test]
fn the_start_time_is_counted_after_the_last_parenthesis() {
    let fields: Vec<String> = (4..=52).map(|field| field.to_string()).collect();
    let line = format!("4242 (a) b (c d) S {}\n", fields.join(" "));
    // Field 22 is the 19th field after the state (field 3).
    assert_eq!(parse_stat(&line), Some(('S', "22".to_owned())));
    assert_eq!(parse_stat("4242 (no fields) S 1 2"), None);
    assert_eq!(parse_stat("no parenthesis at all"), None);
}

#[test]
fn an_identity_round_trips_through_json() {
    let owner = recorded();
    assert_eq!(Identity::from_json(&owner.to_json()), Some(owner));
    assert_eq!(Identity::from_json(&serde_json::json!({"pid": 1})), None);
}

#[cfg(any(target_os = "linux", windows))]
mod processes {
    use super::*;
    use std::process::{Child, Command};

    fn sleeper() -> Child {
        #[cfg(unix)]
        let child = Command::new("sleep").arg("30").spawn();
        #[cfg(windows)]
        let child = Command::new("ping").args(["-n", "30", "127.0.0.1"]).spawn();
        child.unwrap()
    }

    #[test]
    fn a_live_child_is_alive() {
        let mut child = sleeper();
        let observer = identity::observer();
        let owner = identity::observe(i64::from(child.id()), &observer).unwrap();

        assert_eq!(identity::judge(Some(&owner)), Verdict::Alive);

        child.kill().unwrap();
        child.wait().unwrap();
    }

    #[test]
    fn a_killed_child_is_dead() {
        let mut child = sleeper();
        let observer = identity::observer();
        let owner = identity::observe(i64::from(child.id()), &observer).unwrap();
        child.kill().unwrap();
        child.wait().unwrap();

        assert_eq!(identity::probe(owner.pid), Probe::Absent);
        assert_eq!(identity::judge(Some(&owner)), Verdict::Dead);
    }

    #[test]
    fn a_recorded_start_time_that_does_not_match_is_a_reused_pid() {
        let mut child = sleeper();
        let observer = identity::observer();
        let owner = Identity {
            start_time: "1".to_owned(),
            ..identity::observe(i64::from(child.id()), &observer).unwrap()
        };

        assert_eq!(identity::judge(Some(&owner)), Verdict::PidReused);

        child.kill().unwrap();
        child.wait().unwrap();
    }
}
