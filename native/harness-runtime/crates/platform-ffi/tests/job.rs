//! Job objects and process handles against the real operating system: a job without limits and
//! with an owner-only DACL, a name that is never taken over, a name that lives with the member's
//! copy of the handle and ends with the last handle, a suspended child that runs only after
//! resume, descendants that stay in the job with the console host of the hidden console of their
//! leader until `TerminateJobObject` (printed as `JOB-MEMBERS`), a held handle that
//! keeps the creation time and the exit code, 259 included, the executable image and the creator
//! of a process, the system directory with the console host, and a job opened to assign that
//! takes a process. The children are Python scripts of the `python` on PATH. Runs in the Windows
//! CI job only.
#![cfg(windows)]

#[path = "../../supervisor/tests/windows_job/mod.rs"]
mod windows_job;

use std::io::{BufRead, BufReader};
use std::os::windows::process::CommandExt;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::mpsc;
use std::time::{Duration, Instant};

use platform_ffi::{
    CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW, CREATE_SUSPENDED, Job, JobAccess, Process,
    current_user_sid, parent_pids, process_creation_time, system_directory,
};
use windows_job::Expected;

const SLEEPER: &str = "import time\nwhile True: time.sleep(0.1)";

fn job_name(name: &str) -> String {
    format!(r"Global\pf-test-job-{name}-{}", std::process::id())
}

fn python(script: &str) -> Command {
    let mut command = Command::new("python");
    command
        .args(["-c", script])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    command
}

/// A child as the supervisor starts one: suspended, in a new process group, with a hidden console.
fn suspended(script: &str) -> Child {
    python(script)
        .creation_flags(CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW)
        .spawn()
        .unwrap()
}

/// Kill and reap the children a test started, whatever happened.
struct Reaper(Vec<Child>);

impl Drop for Reaper {
    fn drop(&mut self) {
        for child in &mut self.0 {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

/// Terminate a job at the end of a test, whatever happened: the descendants of its members end
/// with it.
struct Terminate(Job);

impl Drop for Terminate {
    fn drop(&mut self) {
        let _ = self.0.terminate(1);
    }
}

/// The first line `child` prints on its piped stdout, waited for up to `seconds`.
fn first_line(child: &mut Child, seconds: u64) -> String {
    let stdout = child.stdout.take().unwrap();
    let (send, receive) = mpsc::channel();
    std::thread::spawn(move || {
        let mut line = String::new();
        let _ = BufReader::new(stdout).read_line(&mut line);
        let _ = send.send(line);
    });
    receive
        .recv_timeout(Duration::from_secs(seconds))
        .unwrap_or_else(|err| {
            panic!(
                "the child printed no line within {seconds} s ({err}); try_wait: {:?}",
                child.try_wait()
            )
        })
}

fn eventually(seconds: u64, mut done: impl FnMut() -> bool) -> bool {
    let until = Instant::now() + Duration::from_secs(seconds);
    while Instant::now() < until {
        if done() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    done()
}

fn scratch(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("pf-job-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

#[test]
fn a_created_job_has_no_limit_and_an_owner_only_dacl() {
    let sid = current_user_sid().unwrap();

    let job = Job::create(&job_name("limits"), &sid).unwrap();

    // No KILL_ON_JOB_CLOSE ends the execution with the runtime, and no breakaway is allowed.
    assert_eq!(job.limit_flags().unwrap(), 0);
    let dacl = job.dacl().unwrap();
    assert!(dacl.protected);
    assert_eq!(dacl.aces.len(), 1, "{dacl:?}");
    assert_eq!(dacl.aces[0].kind, "A");
    assert_eq!(dacl.aces[0].trustee, sid);
}

#[test]
fn a_job_name_that_exists_is_refused_and_never_taken_over() {
    let sid = current_user_sid().unwrap();
    let name = job_name("exists");
    let _first = Job::create(&name, &sid).unwrap();

    let err = Job::create(&name, &sid).unwrap_err();

    assert_eq!(err.kind(), std::io::ErrorKind::AlreadyExists, "{err}");
    assert!(Job::open(&name, JobAccess::Query).unwrap().is_some());
    assert!(
        Job::open(&job_name("nobody"), JobAccess::Control)
            .unwrap()
            .is_none()
    );
}

#[test]
fn the_name_lives_with_the_members_copy_of_the_handle_and_ends_with_the_last_handle() {
    let name = job_name("name");
    let job = Job::create(&name, &current_user_sid().unwrap()).unwrap();
    let mut reaper = Reaper(vec![suspended(SLEEPER)]);
    let process = Process::from_child(&reaper.0[0]).unwrap();
    job.assign(&process).unwrap();
    job.share_with(&process).unwrap();

    // The runtime that created the job is gone: only the member's copy keeps the name.
    drop(job);
    let found = Job::open(&name, JobAccess::Control).unwrap().unwrap();
    assert!(found.contains(&process).unwrap());
    drop(found);

    let child = &mut reaper.0[0];
    child.kill().unwrap();
    child.wait().unwrap();
    assert!(
        eventually(10, || Job::open(&name, JobAccess::Query).unwrap().is_none()),
        "the name outlived its last handle"
    );
}

#[test]
fn a_suspended_child_runs_only_after_resume() {
    let dir = scratch("resume");
    let marker = dir.join("ran");
    let script = format!("open(r'{}', 'w').close()", marker.display());
    let mut reaper = Reaper(vec![suspended(&script)]);
    let process = Process::from_child(&reaper.0[0]).unwrap();

    std::thread::sleep(Duration::from_millis(500));
    assert!(!marker.exists());
    assert_eq!(process.exit_code().unwrap(), None);

    assert!(process.resume().unwrap() >= 1);
    let status = reaper.0[0].wait().unwrap();
    assert!(status.success(), "{status:?}");
    assert!(marker.exists());
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn descendants_stay_in_the_job_until_terminate_job_object_and_an_outsider_is_untouched() {
    let job = Job::create(&job_name("members"), &current_user_sid().unwrap()).unwrap();
    let script = format!(
        "import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', {SLEEPER:?}])\n{SLEEPER}"
    );
    let mut reaper = Reaper(vec![suspended(&script), python(SLEEPER).spawn().unwrap()]);
    let leader = Process::from_child(&reaper.0[0]).unwrap();
    let outsider = Process::from_child(&reaper.0[1]).unwrap();
    job.assign(&leader).unwrap();
    leader.resume().unwrap();

    // The leader, its descendant and the console host of the hidden console of the leader.
    let members = windows_job::wait_for(
        &job,
        "descendants",
        &[
            Expected::python("leader", leader.pid()),
            Expected::python_child_of("descendant", leader.pid()),
            Expected::console_host_of(leader.pid()),
        ],
        20,
    );
    assert_eq!(
        usize::try_from(job.active_processes().unwrap()).unwrap(),
        members.len()
    );
    for member in &members {
        let process = Process::open(member.pid).unwrap().unwrap();
        assert!(job.contains(&process).unwrap(), "{member:?}");
    }
    assert!(!job.contains(&outsider).unwrap());

    job.terminate(137).unwrap();

    assert!(eventually(10, || job.active_processes().unwrap() == 0));
    assert_eq!(reaper.0[0].wait().unwrap().code(), Some(137));
    assert_eq!(outsider.exit_code().unwrap(), None);
}

#[test]
fn a_held_handle_keeps_the_creation_time_and_the_exit_code_even_259() {
    let mut reaper = Reaper(vec![
        python("import sys, time\ntime.sleep(1)\nsys.exit(259)")
            .spawn()
            .unwrap(),
    ]);
    let process = Process::from_child(&reaper.0[0]).unwrap();
    let created = process.creation_time().unwrap();
    assert_eq!(process_creation_time(process.pid()).unwrap(), Some(created));
    assert_eq!(process.exit_code().unwrap(), None);

    reaper.0[0].wait().unwrap();

    // The exit code equals STILL_ACTIVE; the signalled handle still proves the exit.
    assert_eq!(process.exit_code().unwrap(), Some(259));
    assert_eq!(process.creation_time().unwrap(), created);
    // While the handle is held, the pid names the same ended process.
    let again = Process::open(process.pid()).unwrap().unwrap();
    assert_eq!(again.creation_time().unwrap(), created);
    assert_eq!(again.exit_code().unwrap(), Some(259));
}

#[test]
fn a_process_shows_its_executable_image_and_the_pid_of_its_creator() {
    let job = Terminate(Job::create(&job_name("image"), &current_user_sid().unwrap()).unwrap());
    let script = format!(
        "import subprocess, sys, time\nchild = subprocess.Popen([sys.executable, '-c', {SLEEPER:?}])\nprint(child.pid, sys.executable, flush=True)\n{SLEEPER}"
    );
    let mut command = python(&script);
    command
        .stdout(Stdio::piped())
        .creation_flags(CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW);
    let mut reaper = Reaper(vec![command.spawn().unwrap()]);
    let leader = Process::from_child(&reaper.0[0]).unwrap();
    job.0.assign(&leader).unwrap();
    leader.resume().unwrap();

    let line = first_line(&mut reaper.0[0], 20);
    let (pid, executable) = line.trim().split_once(' ').unwrap();
    let child = Process::open(pid.parse().unwrap()).unwrap().unwrap();
    let parents = parent_pids().unwrap();

    assert_eq!(parents.get(&child.pid()), Some(&leader.pid()), "{line}");
    assert_eq!(parents.get(&leader.pid()), Some(&std::process::id()));
    let image = child.image_path().unwrap();
    assert!(
        image.to_string_lossy().eq_ignore_ascii_case(executable),
        "{image:?} is not {executable}"
    );
    let leader_image = leader.image_path().unwrap();
    assert!(
        leader_image
            .file_name()
            .is_some_and(|name| name.eq_ignore_ascii_case("python.exe")),
        "{leader_image:?}"
    );
}

#[test]
fn the_system_directory_is_system32_of_the_windows_directory_and_holds_the_console_host() {
    let root = std::env::var("SystemRoot").unwrap();

    let directory = system_directory().unwrap();

    assert!(
        directory
            .to_string_lossy()
            .eq_ignore_ascii_case(&format!(r"{root}\System32")),
        "{directory:?} under {root}"
    );
    assert!(directory.join("conhost.exe").is_file(), "{directory:?}");
}

#[test]
fn a_job_opened_to_assign_takes_a_process_and_one_opened_to_query_does_not() {
    let name = job_name("assign");
    let job = Job::create(&name, &current_user_sid().unwrap()).unwrap();
    let reaper = Reaper(vec![suspended(SLEEPER)]);
    let process = Process::from_child(&reaper.0[0]).unwrap();
    let query = Job::open(&name, JobAccess::Query).unwrap().unwrap();
    let assign = Job::open(&name, JobAccess::Assign).unwrap().unwrap();

    assert!(query.assign(&process).is_err());
    assign.assign(&process).unwrap();

    assert!(job.contains(&process).unwrap());
    assert_eq!(assign.processes().unwrap(), vec![process.pid()]);
}
