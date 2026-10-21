//! Local IPC transport of `harness-runtime` (ADR 0021, section 2): a Unix domain socket in a
//! `0700` runtime directory, a `SO_PEERCRED` peer check and a capability token.

#![cfg(unix)]

mod paths;
mod peer;
mod server;
mod session;
mod token;

pub use paths::{
    MAX_SOCKET_PATH_BYTES, PathError, RuntimePaths, git_common_dir, repo_id, resolve_paths,
};
pub use peer::same_user;
pub use server::{Server, StartError};
pub use session::{Reply, Session, Shared};
pub use token::{generate_token, tokens_equal};
