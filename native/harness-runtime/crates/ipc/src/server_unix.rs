//! The Unix domain socket server: directory, socket and token permissions, the peer check and one
//! thread per connection (ADR 0021, section 2). A lock file in the runtime directory makes the
//! start of two runtimes for one repository exclusive.

use std::fs::{self, DirBuilder, File, OpenOptions};
use std::io::{self, Write};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;

use contracts::FrameError;

use crate::paths::RuntimePaths;
use crate::peer::{PeerId, own_uid, peer_pid, peer_uid, same_user};
use crate::server::{
    ACCEPT_POLL, IDLE_TIMEOUT, StartError, io_context, serve_connection, take_lock,
};
use crate::session::Shared;
use crate::token::generate_token;

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
        Self::bind_serving(paths, None)
    }

    /// Like [`Server::bind`], and the runtime answers `state.read` from the runtime store of
    /// `state_dir` once that store is the authority of the repository (ADR 0020, section 1).
    pub fn bind_serving(
        paths: RuntimePaths,
        state_dir: Option<std::path::PathBuf>,
    ) -> Result<Self, StartError> {
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
            state_dir,
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
    pub fn serve_until_as(&self, stop: &AtomicBool, expected_uid: PeerId) -> io::Result<()> {
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

fn prepare_directory(dir: &Path, owner: PeerId) -> Result<(), StartError> {
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

/// Prepare an accepted Unix socket, check its peer and serve it.
fn serve_unix_stream(
    mut stream: UnixStream,
    own: PeerId,
    shared: &Shared,
) -> Result<(), FrameError> {
    // The accepted socket must block, whatever the listener's mode was.
    stream.set_nonblocking(false).map_err(FrameError::Io)?;
    stream
        .set_read_timeout(Some(IDLE_TIMEOUT))
        .map_err(FrameError::Io)?;
    let permitted = peer_uid(&stream).is_ok_and(|uid| same_user(uid, own));
    let pid = peer_pid(&stream);
    serve_connection(&mut stream, permitted, pid, shared)
}

#[cfg(test)]
mod tests {
    use super::*;

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
