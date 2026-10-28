//! The Unix domain socket server: directory, socket and token permissions, the peer check and one
//! thread per connection (ADR 0021, section 2). A lock file in the runtime directory makes the
//! start of two runtimes for one repository exclusive.

use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;

use contracts::FrameError;

use crate::paths::RuntimePaths;
use crate::peer::{PeerId, own_uid, peer_pid, peer_uid, same_user};
use crate::server::{
    ACCEPT_POLL, IDLE_TIMEOUT, StartError, io_context, secure_directory, serve_connection,
    take_lock,
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
    /// A shared lock on the runtime directory: `systemd-tmpfiles` never ages a directory that a
    /// process holds a BSD lock on, so the socket, token and lock of a live runtime stay.
    _directory: File,
}

impl Server {
    /// Check the private `0700` directory and hold a shared lock on it, take the lock, write the
    /// `0600` token, then bind the `0600` socket. The lock holder is the only runtime of the
    /// repository, so it alone touches the socket and the token files.
    pub fn bind(paths: RuntimePaths) -> Result<Self, StartError> {
        Self::bind_serving(paths, None)
    }

    /// Like [`Server::bind`], and the runtime answers `state.read` from the runtime store of
    /// `state_dir` once that store is the authority of the repository (ADR 0020, section 1).
    pub fn bind_serving(
        paths: RuntimePaths,
        state_dir: Option<std::path::PathBuf>,
    ) -> Result<Self, StartError> {
        secure_directory(&paths.dir, own_uid())?;
        let directory = hold_directory(&paths.dir)?;
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
            supervisor: Arc::new(crate::server::supervisor_of(&paths, state_dir.as_deref())),
            state_dir,
            snapshots: Arc::default(),
        });
        Ok(Self {
            listener,
            paths,
            shared,
            _lock: lock,
            _directory: directory,
        })
    }

    pub fn paths(&self) -> &RuntimePaths {
        &self.paths
    }

    /// The process supervisor of this server's executions (ADR 0022).
    pub fn supervisor(&self) -> &Arc<supervisor::Supervisor> {
        &self.shared.supervisor
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

/// A shared BSD lock on the runtime directory for the life of the server.
fn hold_directory(dir: &Path) -> Result<File, StartError> {
    let file = File::open(dir).map_err(io_context(format!("cannot open {}", dir.display())))?;
    file.lock_shared()
        .map_err(io_context(format!("cannot lock {}", dir.display())))?;
    Ok(file)
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
    use std::os::unix::fs::{MetadataExt, symlink};

    use super::*;

    fn scratch(name: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("hr-dir-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
        dir
    }

    fn mode(path: &Path) -> u32 {
        fs::symlink_metadata(path).unwrap().mode() & 0o7777
    }

    #[test]
    fn a_missing_directory_is_created_private_to_its_owner() {
        let base = scratch("fresh");
        let dir = base.join("harness");

        secure_directory(&dir, own_uid()).unwrap();

        assert_eq!(mode(&dir), 0o700);
        let _ = fs::remove_dir_all(&base);
    }

    #[test]
    fn a_directory_owned_by_another_user_is_refused_before_it_is_touched() {
        let base = scratch("foreign");
        let dir = base.join("harness");
        fs::create_dir(&dir).unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();

        let outcome = secure_directory(&dir, own_uid().wrapping_add(1));

        assert!(matches!(outcome, Err(StartError::ForeignDirectory(_))));
        assert_eq!(mode(&dir), 0o700);
        let _ = fs::remove_dir_all(&base);
    }

    #[test]
    fn a_directory_open_to_others_is_refused_and_never_repaired() {
        let base = scratch("open");
        let dir = base.join("harness");
        fs::create_dir(&dir).unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o755)).unwrap();

        let outcome = secure_directory(&dir, own_uid());

        let Err(refused @ StartError::UnsafeDirectory { .. }) = outcome else {
            panic!("{outcome:?}");
        };
        assert_eq!(refused.code(), contracts::ErrorCode::UnauthorizedPeer);
        assert!(refused.remedy().contains("chmod 700"));
        assert_eq!(mode(&dir), 0o755);
        let _ = fs::remove_dir_all(&base);
    }

    #[test]
    fn a_symlink_to_a_private_directory_is_refused() {
        let base = scratch("link");
        let target = base.join("target");
        fs::create_dir(&target).unwrap();
        fs::set_permissions(&target, fs::Permissions::from_mode(0o700)).unwrap();
        let dir = base.join("harness");
        symlink(&target, &dir).unwrap();

        let outcome = secure_directory(&dir, own_uid());

        assert!(
            matches!(&outcome, Err(StartError::UnsafeDirectory { reason, .. }) if reason.contains("symbolic link")),
            "{outcome:?}"
        );
        let _ = fs::remove_dir_all(&base);
    }

    #[test]
    fn a_parent_that_others_may_write_needs_the_sticky_bit() {
        let base = scratch("parent");
        let dir = base.join("harness");
        fs::set_permissions(&base, fs::Permissions::from_mode(0o777)).unwrap();

        let outcome = secure_directory(&dir, own_uid());

        assert!(
            matches!(&outcome, Err(StartError::UnsafeDirectory { reason, .. }) if reason.contains("sticky")),
            "{outcome:?}"
        );
        fs::set_permissions(&base, fs::Permissions::from_mode(0o1777)).unwrap();
        secure_directory(&dir, own_uid()).unwrap();
        fs::set_permissions(&base, fs::Permissions::from_mode(0o700)).unwrap();
        let _ = fs::remove_dir_all(&base);
    }
}
