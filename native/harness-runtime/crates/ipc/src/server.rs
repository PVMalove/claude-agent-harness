//! The Unix domain socket server: directory, socket and token permissions, the peer check and one
//! thread per connection (ADR 0021, section 2).

use std::fmt;
use std::fs::{self, DirBuilder, OpenOptions};
use std::io::{self, Write};
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
}

impl Server {
    /// Prepare the `0700` directory, write the `0600` token, then bind the `0600` socket.
    pub fn bind(repo_root: &Path, paths: RuntimePaths) -> Result<Self, StartError> {
        let repo_root = fs::canonicalize(repo_root).map_err(io_context(format!(
            "cannot resolve {}",
            repo_root.display()
        )))?;
        prepare_directory(&paths.dir)?;
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
            repo_root,
            repo_id: paths.repo_id.clone(),
            server_version: env!("CARGO_PKG_VERSION").to_owned(),
        });
        Ok(Self {
            listener,
            paths,
            shared,
        })
    }

    pub fn paths(&self) -> &RuntimePaths {
        &self.paths
    }

    /// Serve connections until `stop` is set. A connection runs on its own thread.
    pub fn serve_until(&self, stop: &AtomicBool) -> io::Result<()> {
        let own = own_uid();
        while !stop.load(Ordering::Relaxed) {
            match self.listener.accept() {
                Ok((stream, _)) => {
                    let shared = Arc::clone(&self.shared);
                    thread::spawn(move || {
                        // A broken connection ends its own thread, never the server.
                        let _ = serve_connection(stream, own, &shared);
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

fn prepare_directory(dir: &Path) -> Result<(), StartError> {
    DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(dir)
        .map_err(io_context(format!("cannot create {}", dir.display())))?;
    let metadata =
        fs::metadata(dir).map_err(io_context(format!("cannot read {}", dir.display())))?;
    if metadata.uid() != own_uid() {
        return Err(StartError::ForeignDirectory(dir.to_path_buf()));
    }
    fs::set_permissions(dir, fs::Permissions::from_mode(0o700)).map_err(io_context(format!(
        "cannot restrict {} to its owner",
        dir.display()
    )))
}

/// A socket file nobody listens on is left by a runtime that died; one that answers is a rival.
fn remove_stale_socket(socket: &Path) -> Result<(), StartError> {
    if !socket.exists() {
        return Ok(());
    }
    match UnixStream::connect(socket) {
        Ok(_) => Err(StartError::AlreadyRunning(socket.to_path_buf())),
        Err(_) => fs::remove_file(socket).map_err(io_context(format!(
            "cannot remove the stale socket {}",
            socket.display()
        ))),
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

fn send(stream: &mut UnixStream, response: &Response) -> Result<(), FrameError> {
    let body = encode(response).map_err(|err| FrameError::Io(io::Error::other(err)))?;
    write_frame(stream, &body)
}

fn refusal(code: ErrorCode, domain: FailureDomain, message: &str, remedy: &str) -> Response {
    Response::error("", ErrorBody::new(code, domain, false, message, remedy))
}

fn serve_connection(mut stream: UnixStream, own: u32, shared: &Shared) -> Result<(), FrameError> {
    // The accepted socket must block, whatever the listener's mode was.
    stream.set_nonblocking(false).map_err(FrameError::Io)?;
    stream
        .set_read_timeout(Some(IDLE_TIMEOUT))
        .map_err(FrameError::Io)?;
    let permitted = peer_uid(&stream).is_ok_and(|uid| same_user(uid, own));
    if !permitted {
        return send(
            &mut stream,
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
        let body = match read_frame(&mut stream) {
            Ok(body) => body,
            Err(FrameError::TooLarge(len)) => {
                // The body was not read, so the stream cannot be resynchronised.
                return send(
                    &mut stream,
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
        send(&mut stream, &reply.response)?;
        if reply.close {
            return Ok(());
        }
    }
}
