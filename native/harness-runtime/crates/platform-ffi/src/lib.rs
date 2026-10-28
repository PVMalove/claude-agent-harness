//! The one isolated platform FFI crate of `harness-runtime` (ADR 0022, section 1).
//!
//! Every other crate of the workspace forbids `unsafe`. Here `unsafe` lives only in the `windows`
//! module, behind a safe API: the SID of the current user and of another process, a DACL that
//! grants a file or a pipe to one user only, a private file, random bytes from the operating
//! system and the creation time of a running process. On other platforms the crate holds only the
//! platform-neutral SDDL parser.

pub mod sddl;

#[cfg(windows)]
#[allow(unsafe_code)]
mod windows;

#[cfg(windows)]
pub use windows::{
    OwnerOnly, PipeConnection, PipeListener, connect_client, create_private_file, current_user_sid,
    dacl_of_path, process_creation_time, process_user_sid, random_bytes,
};
