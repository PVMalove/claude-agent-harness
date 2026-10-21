//! The Windows FFI against the real operating system. Runs in the Windows CI job only.
#![cfg(windows)]

use std::fs;
use std::path::PathBuf;

use platform_ffi::{
    OwnerOnly, create_private_file, current_user_sid, dacl_of_path, process_user_sid, random_bytes,
};

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
