//! The Windows FFI against the real operating system. Runs in the Windows CI job only.
#![cfg(windows)]

use std::fs;
use std::io::{ErrorKind, Read, Write};
use std::path::PathBuf;
use std::time::Duration;

use platform_ffi::{
    OwnerOnly, PipeListener, connect_client, create_private_file, current_user_sid, dacl_of_path,
    process_user_sid, random_bytes,
};

const SHORT: Duration = Duration::from_millis(50);
const LONG: Duration = Duration::from_secs(5);

fn pipe_name(name: &str) -> String {
    format!(r"\\.\pipe\pf-test-{name}-{}", std::process::id())
}

fn listener(name: &str) -> (PipeListener, String) {
    let pipe = pipe_name(name);
    let listener = PipeListener::bind(&pipe, &current_user_sid().unwrap()).unwrap();
    (listener, pipe)
}

fn scratch(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("pf-{name}-{}", std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(&dir).unwrap();
    dir
}

#[test]
fn the_current_user_sid_is_a_sid_string() {
    let sid = current_user_sid().unwrap();

    assert!(sid.starts_with("S-1-"), "{sid}");
    assert!(
        sid.bytes()
            .all(|b| b.is_ascii_digit() || b == b'S' || b == b'-')
    );
}

#[test]
fn the_sid_of_this_process_by_pid_is_the_current_user_sid() {
    assert_eq!(
        process_user_sid(std::process::id()).unwrap(),
        current_user_sid().unwrap()
    );
}

#[test]
fn a_process_that_does_not_exist_has_no_sid() {
    assert!(process_user_sid(u32::MAX).is_err());
}

#[test]
fn a_private_file_has_one_allow_entry_for_its_user_and_no_inheritance() {
    let dir = scratch("private");
    let sid = current_user_sid().unwrap();
    let path = dir.join("secret.token");

    drop(create_private_file(&path, &sid).unwrap());
    let dacl = dacl_of_path(&path).unwrap();

    assert!(dacl.protected);
    assert_eq!(dacl.aces.len(), 1, "{dacl:?}");
    assert_eq!(dacl.aces[0].kind, "A");
    assert_eq!(dacl.aces[0].trustee, sid);
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn a_private_file_is_never_created_over_an_existing_one() {
    let dir = scratch("exists");
    let sid = current_user_sid().unwrap();
    let path = dir.join("secret.token");
    fs::write(&path, "old").unwrap();

    assert!(create_private_file(&path, &sid).is_err());
    assert_eq!(fs::read_to_string(&path).unwrap(), "old");
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn the_owner_only_descriptor_refuses_text_that_is_not_a_sid() {
    for text in ["", "WD", "S-1-5-1)(A;;FA;;;WD", "S-1-5-21-1 "] {
        assert!(OwnerOnly::for_user_sid(text).is_err(), "{text:?}");
    }
}

#[test]
fn random_bytes_fill_the_buffer_and_differ_between_calls() {
    let mut first = [0_u8; 32];
    let mut second = [0_u8; 32];

    random_bytes(&mut first).unwrap();
    random_bytes(&mut second).unwrap();

    assert_ne!(first, [0_u8; 32]);
    assert_ne!(first, second);
}

#[test]
fn accept_gives_up_after_the_timeout_when_no_client_comes() {
    let (mut listener, _) = listener("idle");

    assert!(listener.accept(SHORT).unwrap().is_none());
    assert!(listener.accept(SHORT).unwrap().is_none());
}

#[test]
fn a_client_connects_and_bytes_travel_both_ways() {
    let (mut listener, pipe) = listener("echo");
    let mut client = connect_client(&pipe, LONG).unwrap();
    let mut server = listener.accept(LONG).unwrap().unwrap();

    client.write_all(b"ping").unwrap();
    let mut buffer = [0_u8; 4];
    server.read_exact(&mut buffer).unwrap();
    server.write_all(&buffer).unwrap();
    let mut echoed = [0_u8; 4];
    client.read_exact(&mut echoed).unwrap();

    assert_eq!(&echoed, b"ping");
}

#[test]
fn a_client_that_connects_while_accept_waits_is_accepted() {
    let (mut listener, pipe) = listener("late");
    assert!(listener.accept(SHORT).unwrap().is_none());

    let _client = connect_client(&pipe, LONG).unwrap();

    assert!(listener.accept(LONG).unwrap().is_some());
}

#[test]
fn the_server_sees_the_process_id_of_its_client() {
    let (mut listener, pipe) = listener("pid");
    let _client = connect_client(&pipe, LONG).unwrap();
    let server = listener.accept(LONG).unwrap().unwrap();

    assert_eq!(server.client_process_id().unwrap(), std::process::id());
}

#[test]
fn a_second_client_finds_the_next_instance_ready() {
    let (mut listener, pipe) = listener("two");
    let _first = connect_client(&pipe, LONG).unwrap();
    let _second = connect_client(&pipe, LONG).unwrap();

    assert!(listener.accept(LONG).unwrap().is_some());
    assert!(listener.accept(LONG).unwrap().is_some());
}

#[test]
fn a_read_that_gets_nothing_times_out() {
    let (mut listener, pipe) = listener("timeout");
    let _client = connect_client(&pipe, LONG).unwrap();
    let mut server = listener.accept(LONG).unwrap().unwrap();
    server.set_timeout(Some(SHORT));

    let err = server.read(&mut [0_u8; 1]).unwrap_err();

    assert_eq!(err.kind(), ErrorKind::TimedOut);
}

#[test]
fn a_closed_client_ends_the_stream() {
    let (mut listener, pipe) = listener("eof");
    let client = connect_client(&pipe, LONG).unwrap();
    let mut server = listener.accept(LONG).unwrap().unwrap();

    drop(client);

    assert_eq!(server.read(&mut [0_u8; 1]).unwrap(), 0);
}

#[test]
fn the_pipe_grants_access_to_its_user_only() {
    let (listener, _) = listener("dacl");
    let sid = current_user_sid().unwrap();

    let dacl = listener.dacl().unwrap();

    assert!(dacl.protected, "{dacl:?}");
    assert_eq!(dacl.aces.len(), 1, "{dacl:?}");
    assert_eq!(dacl.aces[0].kind, "A");
    assert_eq!(dacl.aces[0].trustee, sid);
}

#[test]
fn a_pipe_name_in_use_is_refused() {
    let (_first, pipe) = listener("rival");

    let err = PipeListener::bind(&pipe, &current_user_sid().unwrap())
        .err()
        .unwrap();

    assert_eq!(err.kind(), ErrorKind::AddrInUse);
}

#[test]
fn dropping_a_listener_with_a_pending_accept_is_safe() {
    let (mut listener, pipe) = listener("drop");
    assert!(listener.accept(SHORT).unwrap().is_none());

    drop(listener);

    assert!(connect_client(&pipe, SHORT).is_err());
}
