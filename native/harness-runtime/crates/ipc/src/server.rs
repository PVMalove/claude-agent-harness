//! The IPC server shared by every platform: connection handling, the start errors and the lock.
//! The transport is `server_unix` (Unix domain socket) or `server_windows` (named pipe); both
//! check the peer and serve one thread per connection (ADR 0021, section 2). A lock file in the
//! runtime directory makes the start of two runtimes for one repository exclusive.

use std::fmt;
use std::fs::{File, OpenOptions, TryLockError};
use std::io::{self, Read, Write};
use std::path::PathBuf;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use contracts::{
    ErrorBody, ErrorCode, FailureDomain, FrameError, Response, encode, read_frame, write_frame,
};

use crate::paths::{PathError, RuntimePaths};
use crate::session::{Session, Shared};

/// How long the accept loop waits between checks of the stop flag.
pub(crate) const ACCEPT_POLL: Duration = Duration::from_millis(25);
/// A connection that sends nothing for this long is closed.
pub(crate) const IDLE_TIMEOUT: Duration = Duration::from_secs(30);

/// Why the server refused to start. `harness-runtime serve` exits with code 2 for each.
#[derive(Debug)]
pub enum StartError {
    Paths(PathError),
    /// Another runtime already answers on the socket.
    AlreadyRunning(PathBuf),
    /// The runtime directory belongs to another user.
    ForeignDirectory(PathBuf),
    /// The runtime directory, or its parent, would let another user replace the runtime: a
    /// symlink, not a directory, group or other permission bits, or a parent that others may
    /// write without the sticky bit.
    UnsafeDirectory {
        dir: PathBuf,
        reason: &'static str,
        remedy: &'static str,
    },
    Io {
        context: String,
        source: io::Error,
    },
}

impl fmt::Display for StartError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Paths(err) => err.fmt(f),
            Self::AlreadyRunning(socket) => {
                write!(f, "another runtime already serves {}", socket.display())
            }
            Self::ForeignDirectory(dir) => {
                write!(
                    f,
                    "the runtime directory {} belongs to another user",
                    dir.display()
                )
            }
            Self::UnsafeDirectory { dir, reason, .. } => {
                write!(f, "the runtime directory {} {reason}", dir.display())
            }
            Self::Io { context, source } => write!(f, "{context}: {source}"),
        }
    }
}

impl std::error::Error for StartError {}

impl StartError {
    /// The protocol error code that the first `REMEDY:` line of `harness-runtime serve` shows.
    pub const fn code(&self) -> ErrorCode {
        match self {
            Self::Paths(err) => err.code(),
            Self::AlreadyRunning(_) => ErrorCode::LeaseHeld,
            Self::ForeignDirectory(_) | Self::UnsafeDirectory { .. } => ErrorCode::UnauthorizedPeer,
            Self::Io { .. } => ErrorCode::Internal,
        }
    }

    /// What the operator does next.
    pub const fn remedy(&self) -> &'static str {
        match self {
            Self::Paths(err) => err.remedy(),
            Self::AlreadyRunning(_) => {
                "use the runtime that already serves the repository, or stop it first"
            }
            Self::ForeignDirectory(_) => FOREIGN_REMEDY,
            Self::UnsafeDirectory { remedy, .. } => remedy,
            Self::Io { .. } => {
                "check the permissions of the runtime directory, then repeat the command"
            }
        }
    }
}

impl From<PathError> for StartError {
    fn from(err: PathError) -> Self {
        Self::Paths(err)
    }
}

const FOREIGN_REMEDY: &str = "remove the runtime directory, or set XDG_RUNTIME_DIR to a private directory of this user, then repeat the command";

pub(crate) fn io_context(context: impl Into<String>) -> impl FnOnce(io::Error) -> StartError {
    let context = context.into();
    move |source| StartError::Io { context, source }
}

/// The supervisor of a serving runtime: the repository is the Git common directory of the
/// checkout that holds `state_dir`, and the runtime's own directory, socket, token and lock are
/// protected from every container mount (ADR 0022, section 4).
pub(crate) fn supervisor_of(
    paths: &RuntimePaths,
    state_dir: Option<&std::path::Path>,
) -> supervisor::Supervisor {
    let depth = std::path::Path::new(state::migrate::STATE_REL)
        .components()
        .count();
    let repository = state_dir
        .and_then(|dir| dir.ancestors().nth(depth))
        .and_then(|root| crate::paths::git_common_dir(root).ok());
    supervisor::Supervisor::new(state_dir.map(std::path::Path::to_path_buf)).with_runtime(
        repository,
        vec![
            paths.dir.clone(),
            paths.socket.clone(),
            paths.token.clone(),
            paths.lock.clone(),
        ],
    )
}

/// Take the exclusive lock of the repository's runtime. It is held while the server lives, and
/// the kernel drops it when the process dies, so a crash never leaves the repository locked.
pub(crate) fn take_lock(paths: &RuntimePaths) -> Result<File, StartError> {
    let path = &paths.lock;
    let mut options = OpenOptions::new();
    options.write(true).create(true).truncate(false);
    #[cfg(unix)]
    std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o600);
    let file = options
        .open(path)
        .map_err(io_context(format!("cannot open {}", path.display())))?;
    match file.try_lock() {
        Ok(()) => Ok(file),
        Err(TryLockError::WouldBlock) => Err(StartError::AlreadyRunning(paths.socket.clone())),
        Err(TryLockError::Error(source)) => Err(io_context(format!(
            "cannot lock {}",
            path.display()
        ))(source)),
    }
}

/// Hold the instance lock of the repository's runtime without serving. An operation that must not
/// run while `serve` runs (`harness-runtime rollback --to-legacy`) keeps the returned file open for
/// its whole run; a `serve` that starts meanwhile is refused, and the kernel drops the lock when
/// the process dies.
///
/// # Errors
/// `StartError::AlreadyRunning` when a runtime serves the repository; `StartError::Io` when the
/// runtime directory or the lock file cannot be created; `StartError::ForeignDirectory` or
/// `StartError::UnsafeDirectory` for a runtime directory that is not private to this user.
pub fn hold_instance_lock(paths: &RuntimePaths) -> Result<File, StartError> {
    secure_directory(&paths.dir, crate::paths::process_uid())?;
    take_lock(paths)
}

/// Create the runtime directory `0700` when it is missing, then refuse it unless it is private to
/// `owner`: a symlink, not a directory, another owner, any group or other permission bit, or a
/// parent that the group or others may write without the sticky bit. Nothing is repaired: in a
/// shared `/tmp` another user's files may have appeared while the rights were wider. The parent's
/// owner is not checked, because `/tmp` belongs to `nobody` in a user namespace.
#[cfg(unix)]
pub(crate) fn secure_directory(dir: &std::path::Path, owner: u32) -> Result<(), StartError> {
    use std::os::unix::fs::{DirBuilderExt, MetadataExt};

    const REMOVE: &str = "remove the runtime directory, or set XDG_RUNTIME_DIR to a private directory of this user, then repeat the command";
    let unsafe_dir = |reason, remedy| StartError::UnsafeDirectory {
        dir: dir.to_path_buf(),
        reason,
        remedy,
    };
    std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(dir)
        .map_err(io_context(format!("cannot create {}", dir.display())))?;
    let metadata = std::fs::symlink_metadata(dir)
        .map_err(io_context(format!("cannot read {}", dir.display())))?;
    if metadata.file_type().is_symlink() {
        return Err(unsafe_dir("is a symbolic link", REMOVE));
    }
    if !metadata.is_dir() {
        return Err(unsafe_dir("is not a directory", REMOVE));
    }
    if metadata.uid() != owner {
        return Err(StartError::ForeignDirectory(dir.to_path_buf()));
    }
    if metadata.mode() & 0o077 != 0 {
        return Err(unsafe_dir(
            "is open to the group or to others",
            "restrict the runtime directory to its owner (chmod 700), or remove it, then repeat the command",
        ));
    }
    let parent = dir
        .parent()
        .map(std::fs::canonicalize)
        .transpose()
        .map_err(io_context(format!(
            "cannot resolve the parent of {}",
            dir.display()
        )))?;
    if let Some(parent) = parent {
        let mode = std::fs::metadata(&parent)
            .map_err(io_context(format!("cannot read {}", parent.display())))?
            .mode();
        if mode & 0o022 != 0 && mode & 0o1000 == 0 {
            return Err(unsafe_dir(
                "lies in a directory that others may write and that has no sticky bit",
                REMOVE,
            ));
        }
    }
    Ok(())
}

/// Create the runtime directory; on Windows the token and the pipe are closed to other users by
/// their DACL.
#[cfg(not(unix))]
pub(crate) fn secure_directory(dir: &std::path::Path, _owner: u32) -> Result<(), StartError> {
    std::fs::create_dir_all(dir).map_err(io_context(format!("cannot create {}", dir.display())))
}

pub(crate) fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| {
            u64::try_from(elapsed.as_millis()).unwrap_or(u64::MAX)
        })
}

fn send(stream: &mut impl Write, response: &Response) -> Result<(), FrameError> {
    let body = encode(response).map_err(|err| FrameError::Io(io::Error::other(err)))?;
    write_frame(stream, &body)
}

pub(crate) fn refusal(
    code: ErrorCode,
    domain: FailureDomain,
    message: &str,
    remedy: &str,
) -> Response {
    Response::error("", ErrorBody::new(code, domain, false, message, remedy))
}

/// Serve one connection of any byte stream. A peer that is not `permitted` gets
/// `unauthorized_peer` and the connection ends before the client has sent anything.
/// Test hook of a debug build (`HARNESS_RUNTIME_TEST_FAIL_AT=<operation>:respond`, such as
/// `state.write:respond` or `execution.launch:respond`): once, the runtime closes the connection
/// after the operation succeeded and before it answers, so a test can lose the response of a
/// committed write or launch. A release build never drops a response.
#[cfg(debug_assertions)]
fn drops_the_response(body: &[u8], reply: &crate::session::Reply) -> bool {
    use contracts::{Status, decode};

    reply.response.status == Status::Ok
        && decode::<serde_json::Value>(body).is_ok_and(|request| {
            request["operation"]
                .as_str()
                .is_some_and(|operation| state::hooks::fail_once(&format!("{operation}:respond")))
        })
}

#[cfg(not(debug_assertions))]
fn drops_the_response(_body: &[u8], _reply: &crate::session::Reply) -> bool {
    false
}

pub(crate) fn serve_connection<S: Read + Write>(
    stream: &mut S,
    permitted: bool,
    peer_pid: Option<u32>,
    shared: &Shared,
) -> Result<(), FrameError> {
    if !permitted {
        return send(
            stream,
            &refusal(
                ErrorCode::UnauthorizedPeer,
                FailureDomain::Policy,
                "the client does not run as the user of the runtime",
                "run the client as the user that started harness-runtime",
            ),
        );
    }
    let mut session = Session::for_peer(peer_pid);
    loop {
        let body = match read_frame(stream) {
            Ok(body) => body,
            Err(FrameError::TooLarge(len)) => {
                // The body was not read, so the stream cannot be resynchronised.
                return send(
                    stream,
                    &refusal(
                        ErrorCode::MalformedRequest,
                        FailureDomain::Tooling,
                        &format!("the frame of {len} bytes exceeds the limit"),
                        "send frames of at most 16 MiB",
                    ),
                );
            }
            Err(FrameError::Closed) => return Ok(()),
            Err(err) => return Err(err),
        };
        let reply = session.handle(shared, &body, now_ms());
        if drops_the_response(&body, &reply) {
            return Ok(());
        }
        send(stream, &reply.response)?;
        if reply.close {
            return Ok(());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    use contracts::{MAX_FRAME_LEN, Status, decode};

    /// An in-memory connection: what the client sent, and what the server wrote back.
    struct Duplex {
        input: Cursor<Vec<u8>>,
        output: Vec<u8>,
    }

    impl Duplex {
        fn new(input: Vec<u8>) -> Self {
            Self {
                input: Cursor::new(input),
                output: Vec::new(),
            }
        }
    }

    impl Read for Duplex {
        fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
            self.input.read(buf)
        }
    }

    impl Write for Duplex {
        fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
            self.output.write(buf)
        }

        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    fn shared() -> Shared {
        Shared {
            token: "t".repeat(64),
            repo_id: "0123456789abcdef".to_owned(),
            server_version: "test".to_owned(),
            state_dir: None,
            snapshots: Default::default(),
            supervisor: Default::default(),
        }
    }

    fn first_error(output: &[u8]) -> ErrorCode {
        let body = read_frame(&mut Cursor::new(output)).unwrap();
        let response: Response = decode(&body).unwrap();
        assert_eq!(response.status, Status::Error);
        response.error.unwrap().code
    }

    #[test]
    fn a_refused_peer_gets_unauthorized_peer_before_anything_is_read() {
        let mut stream = Duplex::new(vec![0xff; 16]);

        serve_connection(&mut stream, false, None, &shared()).unwrap();

        assert_eq!(first_error(&stream.output), ErrorCode::UnauthorizedPeer);
        assert_eq!(stream.input.position(), 0);
    }

    #[test]
    fn a_permitted_peer_is_served_over_any_byte_stream() {
        let mut stream = Duplex::new((MAX_FRAME_LEN + 1).to_be_bytes().to_vec());

        serve_connection(&mut stream, true, None, &shared()).unwrap();

        assert_eq!(first_error(&stream.output), ErrorCode::MalformedRequest);
    }
}
