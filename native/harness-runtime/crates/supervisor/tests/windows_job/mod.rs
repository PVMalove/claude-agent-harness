//! The members of a job object as the Windows tests see them, apart from the code under test: the
//! pid, the pid of the creator (`parent_pids`) and the executable image of every live member.
//!
//! A test prints them as one line `JOB-MEMBERS <context>: pid=… parent=… image=…; …` with
//! `writeln!` to stderr, which libtest does not capture, so the log of the Windows CI job shows
//! the composition even when the test passes. The expected composition is computed by the test:
//! the console host is `conhost.exe` in the system directory of Windows.

#![allow(dead_code)]

use std::collections::BTreeSet;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use platform_ffi::{Job, Process, parent_pids, system_directory};

/// One live member of a job.
#[derive(Clone, Debug)]
pub struct Member {
    pub pid: u32,
    /// The pid of the process that created it, when the snapshot lists it.
    pub parent: Option<u32>,
    /// The full path of its executable image, or why it cannot be read.
    pub image: String,
}

/// Every live member of `job`.
pub fn members(job: &Job) -> Vec<Member> {
    let pids = job.processes().unwrap();
    // After the list, so every member listed is in the snapshot.
    let parents = parent_pids().unwrap();
    pids.into_iter()
        .filter_map(|pid| {
            let image = match Process::open(pid) {
                Ok(None) => return None,
                Ok(Some(process)) => {
                    if !matches!(process.exit_code(), Ok(None)) {
                        return None;
                    }
                    process.image_path().map_or_else(
                        |err| format!("<the image cannot be read: {err}>"),
                        |path| path.display().to_string(),
                    )
                }
                Err(err) => format!("<the process cannot be opened: {err}>"),
            };
            Some(Member {
                pid,
                parent: parents.get(&pid).copied(),
                image,
            })
        })
        .collect()
}

/// The members as one line: `pid=… parent=… image=…; …`.
pub fn text(members: &[Member]) -> String {
    let listed: Vec<String> = members
        .iter()
        .map(|member| {
            let parent = member
                .parent
                .map_or_else(|| "?".to_owned(), |parent| parent.to_string());
            format!("pid={} parent={parent} image={}", member.pid, member.image)
        })
        .collect();
    listed.join("; ")
}

/// Print `JOB-MEMBERS <context>: …` to stderr, past the capture of libtest.
pub fn print(context: &str, members: &[Member]) {
    let _ = writeln!(
        std::io::stderr(),
        "JOB-MEMBERS {context}: {}",
        text(members)
    );
}

/// The pids of `members`.
pub fn pids(members: &[Member]) -> BTreeSet<u32> {
    members.iter().map(|member| member.pid).collect()
}

/// The executable image a test expects of a member.
#[derive(Clone, Debug)]
pub enum Image {
    /// This full path.
    Path(PathBuf),
    /// Any path with this file name.
    FileName(&'static str),
}

/// What a test expects of one member of a job.
#[derive(Clone, Debug)]
pub struct Expected {
    /// What the member is, for the message of a failure.
    pub what: &'static str,
    /// Its pid, when the test knows it.
    pub pid: Option<u32>,
    /// The pid of its creator, when the test knows it.
    pub parent: Option<u32>,
    pub image: Image,
}

impl Expected {
    /// The Python process `pid`.
    pub const fn python(what: &'static str, pid: u32) -> Self {
        Self {
            what,
            pid: Some(pid),
            parent: None,
            image: Image::FileName("python.exe"),
        }
    }

    /// A Python process that `parent` created.
    pub const fn python_child_of(what: &'static str, parent: u32) -> Self {
        Self {
            what,
            pid: None,
            parent: Some(parent),
            image: Image::FileName("python.exe"),
        }
    }

    /// The console host of the hidden console of `parent`: `conhost.exe` of the system
    /// directory, created by `parent`.
    pub fn console_host_of(parent: u32) -> Self {
        Self {
            what: "console host",
            pid: None,
            parent: Some(parent),
            image: Image::Path(system_directory().unwrap().join("conhost.exe")),
        }
    }

    fn matches(&self, member: &Member) -> bool {
        let image = match &self.image {
            Image::Path(path) => member
                .image
                .eq_ignore_ascii_case(&path.display().to_string()),
            Image::FileName(name) => Path::new(&member.image)
                .file_name()
                .is_some_and(|file| file.eq_ignore_ascii_case(name)),
        };
        image
            && self.pid.is_none_or(|pid| pid == member.pid)
            && self
                .parent
                .is_none_or(|parent| member.parent == Some(parent))
    }
}

/// Whether `members` are exactly `expected`: each expected member matches a member of its own,
/// and no member is left over. An entry with a pid goes before the entries without one.
pub fn composed_of(members: &[Member], expected: &[Expected]) -> bool {
    if members.len() != expected.len() {
        return false;
    }
    let mut taken = vec![false; members.len()];
    expected.iter().all(|want| {
        let found = (0..members.len()).find(|&at| !taken[at] && want.matches(&members[at]));
        found.inspect(|&at| taken[at] = true).is_some()
    })
}

/// Wait up to `seconds` until the members of `job` are exactly `expected`, print them as
/// `JOB-MEMBERS <context>` and return them. A job that is not composed so by then fails the test
/// with the members it holds.
pub fn wait_for(job: &Job, context: &str, expected: &[Expected], seconds: u64) -> Vec<Member> {
    let until = Instant::now() + Duration::from_secs(seconds);
    loop {
        let members = members(job);
        let composed = composed_of(&members, expected);
        if composed || Instant::now() >= until {
            print(context, &members);
            assert!(
                composed,
                "{context}: the job is not composed of {expected:?} within {seconds} s; it holds: {}",
                text(&members)
            );
            return members;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}
