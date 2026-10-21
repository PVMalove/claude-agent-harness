//! The Unix domain socket server: directory, socket and token permissions, the peer check and one
//! thread per connection (ADR 0021, section 2). A lock file in the runtime directory makes the
//! start of two runtimes for one repository exclusive.

use std::fmt;
use std::fs::{self, DirBuilder, File, OpenOptions, TryLockError};
use std::io::{self, Read, Write};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use contracts::{
    ErrorBody, ErrorCode, FailureDomain, FrameError, Response, encode, read_frame, write_frame,
};

use crate::paths::{PathError, RuntimePaths};
use crate::peer::{own_uid, peer_uid, same_user};
use crate::session::{Session, Shared};
use crate::token::generate_token;

/// How long the accept loop sleeps between checks of the stop flag.
const ACCEPT_POLL: Duration = Duration::from_millis(25);
/// A connection that sends nothing for this long is closed.
const IDLE_TIMEOUT: Duration = Duration::from_secs(30);

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

impl From<PathError> for StartError {
    fn from(err: PathError) -> Self {
        Self::Paths(err)
    }
}

fn io_context(context: impl Into<String>) -> impl FnOnce(io::Error) -> StartError {
    let context = context.into();
    move |source| StartError::Io { context, source }
}

/// A bound runtime: its socket is listening and its token is written.
#[derive(Debug)]
pub struct Server {
    listener: UnixListener,
    paths: RuntimePaths,
    shared: Arc<Shared>,
    /// Holds the exclusive lock of this repository's runtime until the server is dropped.
    _lock: File,
}

impl Server {
    /// Prepare the `0700` directory, take the lock, write the `0600` token, then bind the `0600`
    /// socket. The lock holder is the only runtime of the repository, so it alone touches the
    /// socket and the token files.
    pub fn bind(paths: RuntimePaths) -> Result<Self, StartError> {
        prepare_directory(&paths.dir, own_uid())?;
        let lock = take_lock(&paths)?;
        remove_stale_socket(&paths.socket)?;
        let token = generate_token().map_err(io_context("cannot read the random source"))?;
        write_token(&paths.token, &token)?;
        let listener = UnixListener::bind(&paths.socket).map_err(io_context(format!(
            "cannot bind {}",
            paths.socket.display()
        )))?;
        fs::set_permissions(&paths.socket, fs::Permissions::from_mode(0o600))
            .map_err(io_context("cannot restrict the socket to its owner"))?;
        listener
            .set_nonblocking(true)
            .map_err(io_context("cannot make the listener pollable"))?;
        let shared = Arc::new(Shared {
            token,
            repo_id: paths.repo_id.clone(),
            server_version: env!("CARGO_PKG_VERSION").to_owned(),
        });
        Ok(Self {
            listener,
            paths,
            shared,
            _lock: lock,
        })
    }

    pub fn paths(&self) -> &RuntimePaths {
        &self.paths
    }

    /// Serve connections until `stop` is set. A connection runs on its own thread.
    pub fn serve_until(&self, stop: &AtomicBool) -> io::Result<()> {
        self.serve_until_as(stop, own_uid())
    }

    /// Like [`Server::serve_until`], but a client must run as `expected_uid`. Tests use another
    /// uid to see the refusal of a foreign peer on a real socket.
    pub fn serve_until_as(&self, stop: &AtomicBool, expected_uid: u32) -> io::Result<()> {
        let own = expected_uid;
        while !stop.load(Ordering::Relaxed) {
            match self.listener.accept() {
                Ok((stream, _)) => {
                    let shared = Arc::clone(&self.shared);
                    thread::spawn(move || {
                        // A broken connection ends its own thread, never the server.
                        let _ = serve_unix_stream(stream, own, &shared);
                    });
                }
                Err(err) if err.kind() == io::ErrorKind::WouldBlock => {
                    thread::sleep(ACCEPT_POLL);
                }
                Err(err) if err.kind() == io::ErrorKind::Interrupted => {}
                Err(err) => return Err(err),
            }
        }
        Ok(())
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.paths.socket);
        let _ = fs::remove_file(&self.paths.token);
    }
}

fn prepare_directory(dir: &Path, owner: u32) -> Result<(), StartError> {
    DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(dir)
        .map_err(io_context(format!("cannot create {}", dir.display())))?;
    let metadata =
        fs::metadata(dir).map_err(io_context(format!("cannot read {}", dir.display())))?;
    if metadata.uid() != owner {
        return Err(StartError::ForeignDirectory(dir.to_path_buf()));
    }
    fs::set_permissions(dir, fs::Permissions::from_mode(0o700)).map_err(io_context(format!(
        "cannot restrict {} to its owner",
        dir.display()
    )))
}

/// Take the exclusive lock of the repository's runtime. It is held while the server lives, and
/// the kernel drops it when the process dies, so a crash never leaves the repository locked.
fn take_lock(paths: &RuntimePaths) -> Result<File, StartError> {
    let path = paths.dir.join(format!("{}.lock", paths.repo_id));
    let file = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
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

/// With the lock held, a socket file in the way is left by a runtime that died.
fn remove_stale_socket(socket: &Path) -> Result<(), StartError> {
    match fs::remove_file(socket) {
        Ok(()) => Ok(()),
        Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(err) => Err(io_context(format!(
            "cannot remove the stale socket {}",
            socket.display()
        ))(err)),
    }
}

/// Write the token to a new file that is `0600` from its creation.
fn write_token(path: &Path, token: &str) -> Result<(), StartError> {
    match fs::remove_file(path) {
        Ok(()) => {}
        Err(err) if err.kind() == io::ErrorKind::NotFound => {}
        Err(err) => {
            return Err(io_context(format!("cannot replace {}", path.display()))(
                err,
            ));
        }
    }
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)
        .map_err(io_context(format!("cannot create {}", path.display())))?;
    file.write_all(token.as_bytes())
        .map_err(io_context(format!("cannot write {}", path.display())))
}

fn now_ms() -> u64 {
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

fn refusal(code: ErrorCode, domain: FailureDomain, message: &str, remedy: &str) -> Response {
    Response::error("", ErrorBody::new(code, domain, false, message, remedy))
}

/// Prepare an accepted Unix socket, check its peer and serve it.
fn serve_unix_stream(mut stream: UnixStream, own: u32, shared: &Shared) -> Result<(), FrameError> {
    // The accepted socket must block, whatever the listener's mode was.
    stream.set_nonblocking(false).map_err(FrameError::Io)?;
    stream
        .set_read_timeout(Some(IDLE_TIMEOUT))
        .map_err(FrameError::Io)?;
    let permitted = peer_uid(&stream).is_ok_and(|uid| same_user(uid, own));
    serve_connection(&mut stream, permitted, shared)
}

/// Serve one connection of any byte stream. A peer that is not `permitted` gets
/// `unauthorized_peer` and the connection ends before the client has sent anything.
fn serve_connection<S: Read + Write>(
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

    #[test]
    fn a_directory_owned_by_another_user_is_refused_before_it_is_touched() {
        let dir = std::env::temp_dir().join(format!("hr-foreign-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o755)).unwrap();

        let outcome = prepare_directory(&dir, own_uid().wrapping_add(1));

        assert!(matches!(outcome, Err(StartError::ForeignDirectory(_))));
        assert_eq!(
            fs::metadata(&dir).unwrap().permissions().mode() & 0o777,
            0o755
        );
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_directory_of_the_own_user_is_taken_and_restricted() {
        let dir = std::env::temp_dir().join(format!("hr-own-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o755)).unwrap();

        prepare_directory(&dir, own_uid()).unwrap();

        assert_eq!(
            fs::metadata(&dir).unwrap().permissions().mode() & 0o777,
            0o700
        );
        let _ = fs::remove_dir_all(&dir);
    }
}
