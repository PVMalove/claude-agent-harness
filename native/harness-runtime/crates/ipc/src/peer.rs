//! The peer check: the client must run as the same user as the runtime (ADR 0021, section 2).

use std::io;
use std::os::unix::net::UnixStream;

/// Whether the client's uid is the runtime's own uid.
pub const fn same_user(peer_uid: u32, own_uid: u32) -> bool {
    peer_uid == own_uid
}

/// The uid of the process that connected: `SO_PEERCRED`.
#[cfg(target_os = "linux")]
pub fn peer_uid(stream: &UnixStream) -> io::Result<u32> {
    use nix::sys::socket::{getsockopt, sockopt::PeerCredentials};

    getsockopt(stream, PeerCredentials)
        .map(|credentials| credentials.uid())
        .map_err(io::Error::from)
}

/// Peer credentials are implemented for Linux only; elsewhere every peer is refused.
#[cfg(not(target_os = "linux"))]
pub fn peer_uid(_stream: &UnixStream) -> io::Result<u32> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "peer credentials are implemented for Linux only",
    ))
}

/// The uid of the runtime itself.
pub fn own_uid() -> u32 {
    nix::unistd::getuid().as_raw()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_the_runtimes_own_uid_passes() {
        assert!(same_user(1001, 1001));
        assert!(!same_user(1002, 1001));
        assert!(!same_user(0, 1001));
    }
}
