//! Wire contracts shared by the `harness-runtime` server and its clients (ADR 0021).

mod frame;
mod protocol;
mod vocabulary;

pub use frame::{FrameError, MAX_FRAME_LEN, decode, encode, read_frame, write_frame};
pub use protocol::{
    ErrorBody, HelloPayload, HelloResult, PROTOCOL_VERSION, ProtocolVersion, Request, Response,
    Status, VersionRange, negotiate,
};
pub use vocabulary::{ErrorCode, FailureDomain};
