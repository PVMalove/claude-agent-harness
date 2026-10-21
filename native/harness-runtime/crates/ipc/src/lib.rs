//! Local IPC transport of `harness-runtime` (ADR 0021, section 2): a Unix domain socket in a
//! `0700` runtime directory with a `SO_PEERCRED` peer check, or on Windows a named pipe with a
//! DACL for the current user and a client SID check, and a capability token on both.

mod paths;
mod peer;
mod server;
#[cfg(unix)]
mod server_unix;
#[cfg(windows)]
mod server_windows;
mod session;
mod token;

pub use paths::{
    MAX_SOCKET_PATH_BYTES, PathError, RuntimePaths, git_common_dir, pipe_name, repo_id,
    resolve_paths,
};
pub use peer::{PeerId, same_user};
pub use server::StartError;
#[cfg(unix)]
pub use server_unix::Server;
#[cfg(windows)]
pub use server_windows::Server;
pub use session::{Reply, Session, Shared};
pub use token::{generate_token, tokens_equal};
