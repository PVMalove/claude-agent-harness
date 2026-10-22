//! How an operation of the store ends badly. The three kinds are the exit codes of
//! `harness-runtime migrate` (ADR 0020, section 5).

use std::fmt;
use std::io;
use std::path::Path;

/// Why an operation did not complete.
#[derive(Debug)]
pub enum Error {
    /// A precondition refused the operation before it changed anything (exit code 2).
    Refused { message: String, remedy: String },
    /// An I/O or store failure after the operation began; the partial store is discarded and
    /// the authority is the one it was (exit code 3).
    Interrupted(String),
    /// An invariant broke and an operator must look: the legacy state changed during the
    /// operation, or the parity check found a difference (exit code 4).
    Invariant(String),
}

pub type Result<T> = std::result::Result<T, Error>;

impl Error {
    pub fn refused(message: impl Into<String>, remedy: impl Into<String>) -> Self {
        Self::Refused {
            message: message.into(),
            remedy: remedy.into(),
        }
    }

    pub fn invariant(message: impl Into<String>) -> Self {
        Self::Invariant(message.into())
    }

    /// The exit code of `harness-runtime migrate` for this failure.
    pub const fn exit_code(&self) -> u8 {
        match self {
            Self::Refused { .. } => 2,
            Self::Interrupted(_) => 3,
            Self::Invariant(_) => 4,
        }
    }

    /// An I/O failure while the legacy state is only read: nothing changed, so it refuses.
    pub fn unreadable(path: &Path, source: &io::Error) -> Self {
        Self::refused(
            format!("cannot read {}: {source}", path.display()),
            "make the state directory readable, then repeat the command",
        )
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Refused { message, .. } => f.write_str(message),
            Self::Interrupted(message) | Self::Invariant(message) => f.write_str(message),
        }
    }
}

impl std::error::Error for Error {}

impl From<io::Error> for Error {
    fn from(err: io::Error) -> Self {
        Self::Interrupted(err.to_string())
    }
}

impl From<rusqlite::Error> for Error {
    fn from(err: rusqlite::Error) -> Self {
        Self::Interrupted(format!("the store failed: {err}"))
    }
}
