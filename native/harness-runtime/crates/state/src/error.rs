//! How an operation of the store ends badly. The three kinds are the exit codes of
//! `harness-runtime migrate` (ADR 0020, section 5).

use std::fmt;
use std::io;
use std::path::Path;

use contracts::ErrorCode;

/// Why an operation did not complete.
#[derive(Debug)]
pub enum Error {
    /// A precondition refused the operation before it changed anything (exit code 2). `code` is
    /// the protocol error code that names the refusal (ADR 0021, section 3).
    Refused {
        code: ErrorCode,
        message: String,
        remedy: String,
    },
    /// An I/O or store failure after the operation began; the partial store is discarded and
    /// the authority is the one it was (exit code 3).
    Interrupted(String),
    /// An invariant broke and an operator must look: the legacy state changed during the
    /// operation, or the parity check found a difference (exit code 4).
    Invariant(String),
}

pub type Result<T> = std::result::Result<T, Error>;

impl Error {
    pub fn refused(code: ErrorCode, message: impl Into<String>, remedy: impl Into<String>) -> Self {
        Self::Refused {
            code,
            message: message.into(),
            remedy: remedy.into(),
        }
    }

    /// The protocol error code that the first `REMEDY:` line of `harness-runtime migrate` shows.
    pub const fn code(&self) -> ErrorCode {
        match self {
            Self::Refused { code, .. } => *code,
            Self::Interrupted(_) => ErrorCode::Internal,
            Self::Invariant(_) => ErrorCode::InvariantViolation,
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

    /// What the operator does next. Every failure of `harness-runtime migrate` names it.
    pub fn remedy(&self) -> &str {
        match self {
            Self::Refused { remedy, .. } => remedy,
            Self::Interrupted(_) => {
                "repeat `harness-runtime migrate --repo <repo>`: the file ledger is the authority \
                 until the commit point, the next run finishes a stopped one, and a repeat after \
                 the commit point answers already_cut_over"
            }
            Self::Invariant(_) => {
                "stop every writer of the state directory and read the differences above; then \
                 repeat `harness-runtime migrate --repo <repo>`, or, for a state that is cut over \
                 but inconsistent, restore it from the snapshot named in AUTHORITY.json"
            }
        }
    }

    /// An I/O failure while the legacy state is only read: nothing changed, so it refuses.
    pub fn unreadable(path: &Path, source: &io::Error) -> Self {
        Self::refused(
            ErrorCode::Internal,
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn each_kind_names_its_protocol_error_code() {
        assert_eq!(
            Error::refused(ErrorCode::StoreBusy, "m", "r").code(),
            ErrorCode::StoreBusy
        );
        assert_eq!(
            Error::Interrupted("x".to_owned()).code(),
            ErrorCode::Internal
        );
        assert_eq!(Error::invariant("x").code(), ErrorCode::InvariantViolation);
        assert_eq!(
            Error::unreadable(Path::new("/p"), &io::Error::other("x")).code(),
            ErrorCode::Internal
        );
    }
}
