//! The peer check: the client must run as the same user as the runtime (ADR 0021, section 2).
//! On Unix a peer is a uid from `SO_PEERCRED`; on Windows it is the user SID of the client
//! process (`GetNamedPipeClientProcessId`, then the SID of that process token). The pid of the
//! client comes from the same kernel sources and names the owner of a QA lease (ADR 0020,
//! section 7); a request never names it.

#[cfg(unix)]
mod imp {
    use std::io;
    use std::os::unix::net::UnixStream;

    /// What identifies the user of a peer.
    pub type PeerId = u32;

    /// Whether the client's uid is the runtime's own uid.
    pub const fn same_user(peer_uid: PeerId, own_uid: PeerId) -> bool {
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

    /// The pid of the process that connected (`SO_PEERCRED`), as numbered in the PID namespace
    /// of the runtime; `None` when the kernel reports none (0 for a process the runtime cannot
    /// see).
    #[cfg(target_os = "linux")]
    pub fn peer_pid(stream: &UnixStream) -> Option<u32> {
        use nix::sys::socket::{getsockopt, sockopt::PeerCredentials};

        getsockopt(stream, PeerCredentials)
            .ok()
            .and_then(|credentials| u32::try_from(credentials.pid()).ok())
            .filter(|pid| *pid > 0)
    }

    /// Peer credentials are implemented for Linux only.
    #[cfg(not(target_os = "linux"))]
    pub fn peer_pid(_stream: &UnixStream) -> Option<u32> {
        None
    }

    /// The uid of the runtime itself.
    pub fn own_uid() -> PeerId {
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
}

#[cfg(windows)]
mod imp {
    /// What identifies the user of a peer: a SID string such as `S-1-5-21-1-2-3-1001`.
    pub type PeerId = String;

    /// Whether the client's SID is the runtime's own SID.
    pub fn same_user(peer_sid: &str, own_sid: &str) -> bool {
        peer_sid == own_sid
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn only_the_runtimes_own_sid_passes() {
            assert!(same_user("S-1-5-21-1-2-3-1001", "S-1-5-21-1-2-3-1001"));
            assert!(!same_user("S-1-5-21-1-2-3-1002", "S-1-5-21-1-2-3-1001"));
            assert!(!same_user("S-1-1-0", "S-1-5-21-1-2-3-1001"));
        }
    }
}

pub use imp::*;
