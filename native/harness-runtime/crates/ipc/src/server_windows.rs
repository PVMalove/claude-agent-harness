//! The Windows named pipe server: the pipe carries a DACL for the current user only and refuses
//! remote clients, the token file is private to the user, and the peer check compares the user
//! SID of the client process with the runtime's own (ADR 0021, section 2). A lock file in the
//! runtime directory makes the start of two runtimes for one repository exclusive.

use std::fs::{self, File};
use std::io::{self, Write};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, PoisonError};
use std::thread;

use platform_ffi::{
    PipeConnection, PipeListener, create_private_file, current_user_sid, process_user_sid,
};

use crate::paths::RuntimePaths;
use crate::peer::{PeerId, same_user};
use crate::server::{
    ACCEPT_POLL, IDLE_TIMEOUT, StartError, io_context, serve_connection, take_lock,
};
use crate::session::Shared;
use crate::token::generate_token;

/// A bound runtime: its pipe is listening and its token is written.
pub struct Server {
    listener: Mutex<PipeListener>,
    paths: RuntimePaths,
    shared: Arc<Shared>,
    /// Holds the exclusive lock of this repository's runtime until the server is dropped.
    _lock: File,
}

impl Server {
    /// Take the lock, write the private token, then create the pipe. The lock holder is the only
    /// runtime of the repository, so it alone touches the token file.
    pub fn bind(paths: RuntimePaths) -> Result<Self, StartError> {
        Self::bind_serving(paths, None)
    }

    /// Like [`Server::bind`], and the runtime answers `state.read` from the runtime store of
    /// `state_dir` once that store is the authority of the repository (ADR 0020, section 1).
    pub fn bind_serving(
        paths: RuntimePaths,
        state_dir: Option<std::path::PathBuf>,
    ) -> Result<Self, StartError> {
        fs::create_dir_all(&paths.dir)
            .map_err(io_context(format!("cannot create {}", paths.dir.display())))?;
        let lock = take_lock(&paths)?;
        let sid =
            current_user_sid().map_err(io_context("cannot read the SID of the current user"))?;
        let token = generate_token().map_err(io_context("cannot read the random source"))?;
        write_token(&paths.token, &token, &sid)?;
        let pipe = paths.socket.to_string_lossy().into_owned();
        let listener = PipeListener::bind(&pipe, &sid).map_err(|err| {
            if err.kind() == io::ErrorKind::AddrInUse {
                StartError::AlreadyRunning(paths.socket.clone())
            } else {
                io_context(format!("cannot create the pipe {pipe}"))(err)
            }
        })?;
        let shared = Arc::new(Shared {
            token,
            repo_id: paths.repo_id.clone(),
            server_version: env!("CARGO_PKG_VERSION").to_owned(),
            state_dir,
            snapshots: Arc::default(),
        });
        Ok(Self {
            listener: Mutex::new(listener),
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
        let own = current_user_sid()?;
        self.serve_until_as(stop, own)
    }

    /// Like [`Server::serve_until`], but a client must run as the user with SID `expected`.
    /// Tests use another SID to see the refusal of a foreign peer on a real pipe.
    pub fn serve_until_as(&self, stop: &AtomicBool, expected: PeerId) -> io::Result<()> {
        while !stop.load(Ordering::Relaxed) {
            let accepted = self
                .listener
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .accept(ACCEPT_POLL)?;
            if let Some(connection) = accepted {
                let shared = Arc::clone(&self.shared);
                let expected = expected.clone();
                thread::spawn(move || {
                    // A broken connection ends its own thread, never the server.
                    let _ = serve_pipe(connection, &expected, &shared);
                });
            }
        }
        Ok(())
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.paths.token);
    }
}

/// Write the token to a new file that only the user can access from its creation.
fn write_token(path: &Path, token: &str, sid: &str) -> Result<(), StartError> {
    match fs::remove_file(path) {
        Ok(()) => {}
        Err(err) if err.kind() == io::ErrorKind::NotFound => {}
        Err(err) => {
            return Err(io_context(format!("cannot replace {}", path.display()))(
                err,
            ));
        }
    }
    let mut file = create_private_file(path, sid)
        .map_err(io_context(format!("cannot create {}", path.display())))?;
    file.write_all(token.as_bytes())
        .map_err(io_context(format!("cannot write {}", path.display())))
}

/// Check the peer of an accepted pipe connection and serve it. Dropping the connection closes the
/// pipe handle; the client still reads what the server wrote before.
fn serve_pipe(
    mut connection: PipeConnection,
    expected: &str,
    shared: &Shared,
) -> Result<(), contracts::FrameError> {
    connection.set_timeout(Some(IDLE_TIMEOUT));
    let pid = connection.client_process_id().ok().filter(|pid| *pid > 0);
    let permitted = pid
        .ok_or_else(|| std::io::Error::other("no client process id"))
        .and_then(process_user_sid)
        .is_ok_and(|sid| same_user(&sid, expected));
    serve_connection(&mut connection, permitted, pid, shared)
}
