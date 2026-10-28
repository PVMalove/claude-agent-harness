//! Where the socket (on Windows, the named pipe), the token and the lock live (ADR 0021,
//! section 2).
//!
//! The runtime directory is `$XDG_RUNTIME_DIR/harness/` when the variable is absolute; without it
//! `/tmp/harness-<uid>/` on Unix and `%LOCALAPPDATA%\\harness\\` on Windows, outside every
//! repository, so no container plan root holds it. The socket is `<runtime dir>/<repo-id>.sock`,
//! the token `<runtime dir>/<repo-id>.token` and the instance lock `<runtime dir>/<repo-id>.lock`.
//! `<repo-id>` is the first 16 hex digits of the SHA-256 of the absolute path of the common git
//! directory, so a linked worktree and its main checkout reach one runtime. On Windows the
//! endpoint is the named pipe `\\\\.\\pipe\\harness-runtime-<repo-id>` and the path hashed is the one
//! without the `\\\\?\\` prefix.

use std::env;
use std::ffi::OsString;
use std::fmt;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use contracts::ErrorCode;
use sha2::{Digest, Sha256};

/// A Unix socket path longer than this does not fit `sockaddr_un` on every platform.
pub const MAX_SOCKET_PATH_BYTES: usize = 100;

/// The paths of one repository's runtime.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RuntimePaths {
    pub repo_id: String,
    pub dir: PathBuf,
    pub socket: PathBuf,
    pub token: PathBuf,
    pub lock: PathBuf,
}

/// Why the paths could not be determined.
#[derive(Debug)]
pub enum PathError {
    NotARepository(PathBuf),
    SocketPathTooLong {
        path: PathBuf,
        bytes: usize,
    },
    /// On Windows neither `XDG_RUNTIME_DIR` nor `LOCALAPPDATA` names an absolute directory.
    NoRuntimeDirectory,
    Io {
        path: PathBuf,
        source: io::Error,
    },
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
            Self::NoRuntimeDirectory => f.write_str(
                "neither XDG_RUNTIME_DIR nor LOCALAPPDATA names an absolute runtime directory",
            ),
            Self::Io { path, source } => write!(f, "cannot read {}: {source}", path.display()),
        }
    }
}

impl std::error::Error for PathError {}

impl PathError {
    /// The protocol error code that the first `REMEDY:` line of `harness-runtime serve` shows.
    pub const fn code(&self) -> ErrorCode {
        match self {
            Self::NotARepository(_) => ErrorCode::NotFound,
            Self::SocketPathTooLong { .. } => ErrorCode::MalformedRequest,
            Self::NoRuntimeDirectory => ErrorCode::NotFound,
            Self::Io { .. } => ErrorCode::Internal,
        }
    }

    /// What the operator does next.
    pub const fn remedy(&self) -> &'static str {
        match self {
            Self::NotARepository(_) => "pass the root of a git working tree with --repo <path>",
            Self::SocketPathTooLong { .. } => "set XDG_RUNTIME_DIR to a shorter absolute directory",
            Self::NoRuntimeDirectory => {
                "set XDG_RUNTIME_DIR or LOCALAPPDATA to an absolute directory"
            }
            Self::Io { .. } => "make the path readable for this user, then repeat the command",
        }
    }
}

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
        return canonical(&dot_git).map_err(io_error(&dot_git));
    }
    if !dot_git.is_file() {
        return Err(PathError::NotARepository(repo_root.to_path_buf()));
    }
    let pointer = fs::read_to_string(&dot_git).map_err(io_error(&dot_git))?;
    let Some(target) = pointer.trim().strip_prefix("gitdir:") else {
        return Err(PathError::NotARepository(repo_root.to_path_buf()));
    };
    let git_dir = canonical(&repo_root.join(target.trim())).map_err(io_error(&dot_git))?;
    let commondir = git_dir.join("commondir");
    match fs::read_to_string(&commondir) {
        Ok(text) => canonical(&git_dir.join(text.trim())).map_err(io_error(&commondir)),
        Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(git_dir),
        Err(err) => Err(io_error(&commondir)(err)),
    }
}

/// The name of the repository's Windows named pipe: `\\.\pipe\harness-runtime-<repo-id>`.
pub fn pipe_name(repo_id: &str) -> String {
    format!(r"\\.\pipe\harness-runtime-{repo_id}")
}

/// A Windows path without its `\\?\` prefix: `\\?\C:\x` becomes `C:\x` and `\\?\UNC\s\sh\x`
/// becomes `\\s\sh\x`. The Python client hashes the path in that form, and the `\\?\` form is
/// not normalised, so a `..` in it would not resolve. Any other path stays as it is.
#[cfg(any(windows, test))]
fn strip_verbatim(path: &Path) -> PathBuf {
    let text = path.to_string_lossy();
    if let Some(rest) = text.strip_prefix(r"\\?\UNC\") {
        return PathBuf::from(format!(r"\\{rest}"));
    }
    if let Some(rest) = text.strip_prefix(r"\\?\") {
        let mut chars = rest.chars();
        let drive = chars.next().is_some_and(|c| c.is_ascii_alphabetic());
        if drive && chars.next() == Some(':') {
            return PathBuf::from(rest);
        }
    }
    path.to_path_buf()
}

/// The canonical absolute path in the form both runtime and client hash: `fs::canonicalize` on
/// Unix, and the same without the `\\?\` prefix on Windows.
pub(crate) fn canonical(path: &Path) -> io::Result<PathBuf> {
    let resolved = fs::canonicalize(path)?;
    #[cfg(windows)]
    let resolved = strip_verbatim(&resolved);
    Ok(resolved)
}

/// The bytes of a path that are hashed: the raw bytes on Unix, UTF-8 on Windows.
fn path_bytes(path: &Path) -> Vec<u8> {
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStrExt;
        path.as_os_str().as_bytes().to_vec()
    }
    #[cfg(not(unix))]
    {
        path.to_string_lossy().into_owned().into_bytes()
    }
}

/// The first 16 hex digits of the SHA-256 of the path's bytes.
pub fn repo_id(common_git_dir: &Path) -> String {
    let digest = Sha256::digest(path_bytes(common_git_dir));
    digest[..8]
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// The IPC endpoint: a Unix socket file in the runtime directory, whose path must fit
/// `sockaddr_un`, or the named pipe of the repository on Windows.
#[cfg(unix)]
fn endpoint(dir: &Path, id: &str) -> Result<PathBuf, PathError> {
    use std::os::unix::ffi::OsStrExt;

    let socket = dir.join(format!("{id}.sock"));
    let bytes = socket.as_os_str().as_bytes().len();
    if bytes > MAX_SOCKET_PATH_BYTES {
        return Err(PathError::SocketPathTooLong {
            path: socket,
            bytes,
        });
    }
    Ok(socket)
}

#[cfg(not(unix))]
fn endpoint(_dir: &Path, id: &str) -> Result<PathBuf, PathError> {
    Ok(PathBuf::from(pipe_name(id)))
}

/// The platform whose rule picks the runtime directory; a test reads the Windows rule anywhere.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Platform {
    Unix,
    Windows,
}

/// What the runtime directory is chosen from: a command reads it once from its environment.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Environment {
    pub platform: Platform,
    pub xdg_runtime_dir: Option<OsString>,
    pub local_app_data: Option<OsString>,
    pub uid: u32,
}

impl Environment {
    /// The platform, `XDG_RUNTIME_DIR`, `LOCALAPPDATA` and the uid of this process.
    pub fn of_process() -> Self {
        Self {
            platform: if cfg!(windows) {
                Platform::Windows
            } else {
                Platform::Unix
            },
            xdg_runtime_dir: env::var_os("XDG_RUNTIME_DIR"),
            local_app_data: env::var_os("LOCALAPPDATA"),
            uid: process_uid(),
        }
    }

    /// This process with `XDG_RUNTIME_DIR` set to `xdg`.
    pub fn with_xdg(xdg: &Path) -> Self {
        Self {
            xdg_runtime_dir: Some(xdg.as_os_str().to_owned()),
            ..Self::of_process()
        }
    }
}

#[cfg(unix)]
pub(crate) fn process_uid() -> u32 {
    nix::unistd::getuid().as_raw()
}

#[cfg(not(unix))]
pub(crate) const fn process_uid() -> u32 {
    0
}

/// A Windows path with a drive (`C:\\`) or a UNC server (`\\\\server`), read as text so that
/// every platform applies the same rule.
fn windows_absolute(text: &str) -> bool {
    let separator = |byte: u8| byte == b'\\' || byte == b'/';
    match text.as_bytes() {
        [drive, b':', next, ..] => drive.is_ascii_alphabetic() && separator(*next),
        [first, second, third, ..] => separator(*first) && separator(*second) && !separator(*third),
        _ => false,
    }
}

/// The runtime directory of ADR 0021, section 2: `$XDG_RUNTIME_DIR/harness` when the variable is
/// absolute; otherwise `/tmp/harness-<uid>` on Unix (the literal `/tmp`, never `TMPDIR`) and
/// `%LOCALAPPDATA%\\harness` on Windows. A pure function of `env`.
///
/// # Errors
/// `PathError::NoRuntimeDirectory` on Windows when neither variable is an absolute directory.
pub fn runtime_dir(env: &Environment) -> Result<PathBuf, PathError> {
    let text = |value: &Option<OsString>| {
        value
            .as_ref()
            .map(|value| value.to_string_lossy().into_owned())
    };
    match env.platform {
        Platform::Unix => Ok(match &env.xdg_runtime_dir {
            Some(xdg) if xdg.as_encoded_bytes().first() == Some(&b'/') => {
                PathBuf::from(xdg).join("harness")
            }
            _ => PathBuf::from(format!("/tmp/harness-{}", env.uid)),
        }),
        Platform::Windows => [text(&env.xdg_runtime_dir), text(&env.local_app_data)]
            .into_iter()
            .flatten()
            .find(|base| windows_absolute(base))
            .map(|base| PathBuf::from(format!("{}\\harness", base.trim_end_matches(['\\', '/']))))
            .ok_or(PathError::NoRuntimeDirectory),
    }
}

/// Resolve the runtime paths of a repository in the runtime directory that `env` selects.
pub fn resolve_paths(repo_root: &Path, env: &Environment) -> Result<RuntimePaths, PathError> {
    let root = canonical(repo_root).map_err(io_error(repo_root))?;
    let id = repo_id(&git_common_dir(&root)?);
    let dir = runtime_dir(env)?;
    let socket = endpoint(&dir, &id)?;
    let token = dir.join(format!("{id}.token"));
    let lock = dir.join(format!("{id}.lock"));
    Ok(RuntimePaths {
        repo_id: id,
        dir,
        socket,
        token,
        lock,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("hr-paths-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        canonical(&dir).unwrap()
    }

    #[test]
    fn the_repo_id_is_sixteen_hex_digits_of_the_sha256_of_the_path() {
        // Computed with `printf %s /r/.git | sha256sum`.
        assert_eq!(repo_id(Path::new("/r/.git")), "2d09593ea5e01029");
    }

    #[test]
    fn the_repo_id_of_a_windows_path_is_the_same_on_every_platform() {
        // Computed with `printf %s 'C:\r\.git' | sha256sum`; the Python client uses the same vector.
        assert_eq!(repo_id(Path::new(r"C:\r\.git")), "34fdb197a11ee020");
    }

    #[test]
    fn the_windows_pipe_is_named_after_the_repo_id() {
        assert_eq!(
            pipe_name("34fdb197a11ee020"),
            r"\\.\pipe\harness-runtime-34fdb197a11ee020"
        );
    }

    #[test]
    fn the_verbatim_prefix_of_a_windows_path_is_dropped() {
        for (verbatim, plain) in [
            (r"\\?\C:\r\.git", r"C:\r\.git"),
            (r"\\?\UNC\srv\share\r\.git", r"\\srv\share\r\.git"),
            (r"C:\r\.git", r"C:\r\.git"),
            ("/r/.git", "/r/.git"),
            (r"\\?\Volume{1}\r", r"\\?\Volume{1}\r"),
        ] {
            assert_eq!(strip_verbatim(Path::new(verbatim)), PathBuf::from(plain));
        }
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

    #[cfg(unix)]
    #[test]
    fn the_socket_and_the_token_sit_in_the_xdg_directory_when_it_is_set() {
        let root = scratch("xdg");
        fs::create_dir(root.join(".git")).unwrap();
        let xdg = scratch("xdg-run");

        let paths = resolve_paths(&root, &Environment::with_xdg(&xdg)).unwrap();

        assert_eq!(paths.dir, xdg.join("harness"));
        for (path, suffix) in [
            (&paths.socket, "sock"),
            (&paths.token, "token"),
            (&paths.lock, "lock"),
        ] {
            assert_eq!(
                *path,
                xdg.join(format!("harness/{}.{suffix}", paths.repo_id))
            );
        }
    }

    fn unix(xdg: Option<&str>, uid: u32) -> Environment {
        Environment {
            platform: Platform::Unix,
            xdg_runtime_dir: xdg.map(OsString::from),
            local_app_data: None,
            uid,
        }
    }

    #[cfg(unix)]
    #[test]
    fn without_xdg_the_runtime_directory_lies_outside_the_repository() {
        let root = scratch("fallback");
        fs::create_dir(root.join(".git")).unwrap();

        let paths = resolve_paths(&root, &unix(None, 4242)).unwrap();

        assert_eq!(paths.dir, PathBuf::from("/tmp/harness-4242"));
        assert!(!paths.dir.starts_with(&root) && !root.starts_with(&paths.dir));
        assert_eq!(
            paths.lock,
            paths.dir.join(format!("{}.lock", paths.repo_id))
        );
        assert_eq!(
            resolve_paths(&root, &unix(Some("run"), 4242)).unwrap(),
            paths
        );
    }

    #[test]
    fn a_linked_worktree_and_its_main_checkout_share_one_runtime() {
        let root = scratch("shared");
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

        assert_eq!(
            resolve_paths(&worktree, &unix(None, 7)).unwrap(),
            resolve_paths(&root.join("main"), &unix(None, 7)).unwrap()
        );
    }

    /// Every case of the fixture the Python client passes too.
    #[test]
    fn the_runtime_directory_follows_the_shared_fixture() {
        let fixture: serde_json::Value =
            serde_json::from_str(include_str!("../tests/fixtures/runtime_paths.json")).unwrap();
        let cases = fixture["cases"].as_array().unwrap();
        assert!(cases.len() >= 10);
        for case in cases {
            let variable = |key: &str| case[key].as_str().map(OsString::from);
            let env = Environment {
                platform: match case["platform"].as_str().unwrap() {
                    "unix" => Platform::Unix,
                    _ => Platform::Windows,
                },
                xdg_runtime_dir: variable("xdg_runtime_dir"),
                local_app_data: variable("local_app_data"),
                uid: u32::try_from(case["uid"].as_u64().unwrap()).unwrap(),
            };
            let chosen = runtime_dir(&env);
            match case["directory"].as_str() {
                Some(directory) => assert_eq!(
                    chosen.unwrap(),
                    PathBuf::from(directory),
                    "{}",
                    case["name"]
                ),
                None => assert!(
                    matches!(chosen, Err(PathError::NoRuntimeDirectory)),
                    "{}",
                    case["name"]
                ),
            }
        }
    }

    #[cfg(windows)]
    #[test]
    fn the_endpoint_is_the_pipe_of_the_repository_and_the_token_sits_in_the_runtime_directory() {
        let root = scratch("pipe");
        fs::create_dir(root.join(".git")).unwrap();

        let env = Environment {
            xdg_runtime_dir: None,
            local_app_data: Some(OsString::from(r"C:\Users\user\AppData\Local")),
            ..Environment::of_process()
        };

        let paths = resolve_paths(&root, &env).unwrap();

        assert_eq!(paths.socket, PathBuf::from(pipe_name(&paths.repo_id)));
        assert_eq!(
            paths.token,
            PathBuf::from(r"C:\Users\user\AppData\Local\harness")
                .join(format!("{}.token", paths.repo_id))
        );
    }

    #[cfg(unix)]
    #[test]
    fn a_socket_path_over_the_limit_is_refused() {
        let root = scratch("long");
        fs::create_dir(root.join(".git")).unwrap();
        let xdg = root.join("x".repeat(MAX_SOCKET_PATH_BYTES));

        assert!(matches!(
            resolve_paths(&root, &Environment::with_xdg(&xdg)),
            Err(PathError::SocketPathTooLong { .. })
        ));
    }
}
