//! Where the socket and the token live (ADR 0021, section 2).
//!
//! The runtime directory is `$XDG_RUNTIME_DIR/harness/`, or `<repo>/.harness/orchestration/state/
//! runtime/` without it. The socket is `<runtime dir>/<repo-id>.sock` and the token
//! `<runtime dir>/<repo-id>.token`. `<repo-id>` is the first 16 hex digits of the SHA-256 of the
//! absolute path of the common git directory.

use std::fmt;
use std::fs;
use std::io;
use std::os::unix::ffi::OsStrExt;
use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};

/// A Unix socket path longer than this does not fit `sockaddr_un` on every platform.
pub const MAX_SOCKET_PATH_BYTES: usize = 100;

const STATE_RUNTIME_DIR: &str = ".harness/orchestration/state/runtime";

/// The three paths of one repository's runtime.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RuntimePaths {
    pub repo_id: String,
    pub dir: PathBuf,
    pub socket: PathBuf,
    pub token: PathBuf,
}

/// Why the paths could not be determined.
#[derive(Debug)]
pub enum PathError {
    NotARepository(PathBuf),
    SocketPathTooLong { path: PathBuf, bytes: usize },
    Io { path: PathBuf, source: io::Error },
}

impl fmt::Display for PathError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotARepository(path) => {
                write!(
                    f,
                    "{} is not the root of a git working tree",
                    path.display()
                )
            }
            Self::SocketPathTooLong { path, bytes } => write!(
                f,
                "the socket path {} is {bytes} bytes long; the limit is {MAX_SOCKET_PATH_BYTES}",
                path.display()
            ),
            Self::Io { path, source } => write!(f, "cannot read {}: {source}", path.display()),
        }
    }
}

impl std::error::Error for PathError {}

fn io_error(path: &Path) -> impl FnOnce(io::Error) -> PathError + '_ {
    move |source| PathError::Io {
        path: path.to_path_buf(),
        source,
    }
}

/// The absolute common git directory of a working tree root: `.git` itself, or, for a linked
/// worktree, the directory its `commondir` file names.
pub fn git_common_dir(repo_root: &Path) -> Result<PathBuf, PathError> {
    let dot_git = repo_root.join(".git");
    if dot_git.is_dir() {
        return fs::canonicalize(&dot_git).map_err(io_error(&dot_git));
    }
    if !dot_git.is_file() {
        return Err(PathError::NotARepository(repo_root.to_path_buf()));
    }
    let pointer = fs::read_to_string(&dot_git).map_err(io_error(&dot_git))?;
    let Some(target) = pointer.trim().strip_prefix("gitdir:") else {
        return Err(PathError::NotARepository(repo_root.to_path_buf()));
    };
    let git_dir = fs::canonicalize(repo_root.join(target.trim())).map_err(io_error(&dot_git))?;
    let commondir = git_dir.join("commondir");
    match fs::read_to_string(&commondir) {
        Ok(text) => fs::canonicalize(git_dir.join(text.trim())).map_err(io_error(&commondir)),
        Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(git_dir),
        Err(err) => Err(io_error(&commondir)(err)),
    }
}

/// The first 16 hex digits of the SHA-256 of the path's bytes.
pub fn repo_id(common_git_dir: &Path) -> String {
    let digest = Sha256::digest(common_git_dir.as_os_str().as_bytes());
    digest[..8]
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// Resolve the runtime paths of a repository. An unset, empty or relative `XDG_RUNTIME_DIR` is
/// ignored.
pub fn resolve_paths(
    repo_root: &Path,
    xdg_runtime_dir: Option<&Path>,
) -> Result<RuntimePaths, PathError> {
    let root = fs::canonicalize(repo_root).map_err(io_error(repo_root))?;
    let id = repo_id(&git_common_dir(&root)?);
    let dir = match xdg_runtime_dir {
        Some(xdg) if xdg.is_absolute() => xdg.join("harness"),
        _ => root.join(STATE_RUNTIME_DIR),
    };
    let socket = dir.join(format!("{id}.sock"));
    let bytes = socket.as_os_str().as_bytes().len();
    if bytes > MAX_SOCKET_PATH_BYTES {
        return Err(PathError::SocketPathTooLong {
            path: socket,
            bytes,
        });
    }
    let token = dir.join(format!("{id}.token"));
    Ok(RuntimePaths {
        repo_id: id,
        dir,
        socket,
        token,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("hr-paths-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        fs::canonicalize(dir).unwrap()
    }

    #[test]
    fn the_repo_id_is_sixteen_hex_digits_of_the_sha256_of_the_path() {
        // Computed with `printf %s /r/.git | sha256sum`.
        assert_eq!(repo_id(Path::new("/r/.git")), "2d09593ea5e01029");
    }

    #[test]
    fn a_plain_checkout_has_its_dot_git_as_the_common_dir() {
        let root = scratch("plain");
        fs::create_dir(root.join(".git")).unwrap();

        assert_eq!(git_common_dir(&root).unwrap(), root.join(".git"));
    }

    #[test]
    fn a_linked_worktree_follows_its_commondir() {
        let root = scratch("linked");
        let common = root.join("main/.git");
        let admin = common.join("worktrees/w");
        fs::create_dir_all(&admin).unwrap();
        fs::write(admin.join("commondir"), "../..\n").unwrap();
        let worktree = root.join("w");
        fs::create_dir(&worktree).unwrap();
        fs::write(
            worktree.join(".git"),
            format!("gitdir: {}\n", admin.display()),
        )
        .unwrap();

        assert_eq!(git_common_dir(&worktree).unwrap(), common);
    }

    #[test]
    fn a_directory_that_is_no_repository_is_refused() {
        let root = scratch("none");

        assert!(matches!(
            git_common_dir(&root),
            Err(PathError::NotARepository(_))
        ));
    }

    #[test]
    fn the_socket_and_the_token_sit_in_the_xdg_directory_when_it_is_set() {
        let root = scratch("xdg");
        fs::create_dir(root.join(".git")).unwrap();
        let xdg = scratch("xdg-run");

        let paths = resolve_paths(&root, Some(&xdg)).unwrap();

        assert_eq!(paths.dir, xdg.join("harness"));
        assert_eq!(
            paths.socket,
            xdg.join(format!("harness/{}.sock", paths.repo_id))
        );
        assert_eq!(
            paths.token,
            xdg.join(format!("harness/{}.token", paths.repo_id))
        );
    }

    #[test]
    fn without_xdg_the_state_root_holds_the_runtime_directory() {
        let root = scratch("state");
        fs::create_dir(root.join(".git")).unwrap();

        let paths = resolve_paths(&root, None).unwrap();

        assert_eq!(paths.dir, root.join(STATE_RUNTIME_DIR));
        assert_eq!(resolve_paths(&root, Some(Path::new("run"))).unwrap(), paths);
    }

    #[test]
    fn a_socket_path_over_the_limit_is_refused() {
        let root = scratch("long");
        fs::create_dir(root.join(".git")).unwrap();
        let xdg = root.join("x".repeat(MAX_SOCKET_PATH_BYTES));

        assert!(matches!(
            resolve_paths(&root, Some(&xdg)),
            Err(PathError::SocketPathTooLong { .. })
        ));
    }
}
