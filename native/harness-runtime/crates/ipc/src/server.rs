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
            Self::ForeignDirectory(_) => ErrorCode::UnauthorizedPeer,
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
            Self::ForeignDirectory(_) => {
                "set XDG_RUNTIME_DIR to a directory that this user owns, then repeat the command"
            }
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

pub(crate) fn io_context(context: impl Into<String>) -> impl FnOnce(io::Error) -> StartError {
    let context = context.into();
    move |source| StartError::Io { context, source }
}

/// Take the exclusive lock of the repository's runtime. It is held while the server lives, and
/// the kernel drops it when the process dies, so a crash never leaves the repository locked.
pub(crate) fn take_lock(paths: &RuntimePaths) -> Result<File, StartError> {
    let path = paths.dir.join(format!("{}.lock", paths.repo_id));
    let mut options = OpenOptions::new();
    options.write(true).create(true).truncate(false);
    #[cfg(unix)]
    std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o600);
    let file = options
        .open(&path)
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
/// runtime directory or the lock file cannot be created.
pub fn hold_instance_lock(paths: &RuntimePaths) -> Result<File, StartError> {
    let mut builder = std::fs::DirBuilder::new();
    builder.recursive(true);
    #[cfg(unix)]
    std::os::unix::fs::DirBuilderExt::mode(&mut builder, 0o700);
    builder
        .create(&paths.dir)
        .map_err(io_context(format!("cannot create {}", paths.dir.display())))?;
    take_lock(paths)
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
/// Test hook of a debug build (`HARNESS_RUNTIME_TEST_FAIL_AT=state.write:respond`): once, the
/// runtime closes the connection after a `state.write` was committed and before it answers, so a
/// test can lose the response of a committed write. A release build never drops a response.
#[cfg(debug_assertions)]
fn drops_the_response(body: &[u8], reply: &crate::session::Reply) -> bool {
    use contracts::{Status, decode};

    reply.response.status == Status::Ok
        && decode::<serde_json::Value>(body)
            .is_ok_and(|request| request["operation"] == "state.write")
        && state::hooks::fail_once("state.write:respond")
}

#[cfg(not(debug_assertions))]
fn drops_the_response(_body: &[u8], _reply: &crate::session::Reply) -> bool {
    false
}

pub(crate) fn serve_connection<S: Read + Write>(
    stream: &mut S,
    permitted: bool,
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
    let mut session = Session::new();
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

        serve_connection(&mut stream, false, &shared()).unwrap();

        assert_eq!(first_error(&stream.output), ErrorCode::UnauthorizedPeer);
        assert_eq!(stream.input.position(), 0);
    }

    #[test]
    fn a_permitted_peer_is_served_over_any_byte_stream() {
        let mut stream = Duplex::new((MAX_FRAME_LEN + 1).to_be_bytes().to_vec());

        serve_connection(&mut stream, true, &shared()).unwrap();

        assert_eq!(first_error(&stream.output), ErrorCode::MalformedRequest);
    }
}
