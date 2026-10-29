//! The one isolated platform FFI crate of `harness-runtime` (ADR 0022, section 1).
//!
//! Every other crate of the workspace forbids `unsafe`. Here `unsafe` lives only in the `windows`
//! module and its child module `job`, behind a safe API: the SID of the current user and of
//! another process, a DACL that grants a file or a pipe to one user only, a private file, random
//! bytes from the operating system, the creation time of a running process, named job objects,
//! process handles that resume threads and outlive their process, and console control events. On
//! other platforms the crate holds only the platform-neutral SDDL parser.

pub mod sddl;

#[cfg(windows)]
#[allow(unsafe_code)]
mod windows;

#[cfg(windows)]
pub use windows::{
    CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW, CREATE_SUSPENDED, DETACHED_PROCESS, Job, JobAccess,
    OwnerOnly, PipeConnection, PipeListener, Process, connect_client, create_private_file,
    current_user_sid, dacl_of_path, ignore_ctrl_events, process_creation_time, process_user_sid,
    random_bytes, send_ctrl_break,
};
