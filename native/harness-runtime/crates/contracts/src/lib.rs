//! Wire contracts shared by the `harness-runtime` server and its clients (ADR 0021).

mod frame;
mod protocol;
#[macro_use]
mod vocabulary;
mod stage_evidence;

pub use frame::{FrameError, MAX_FRAME_LEN, decode, encode, read_frame, write_frame};
pub use protocol::{
    ErrorBody, HelloPayload, HelloResult, PROTOCOL_VERSION, ProtocolVersion, Request, Response,
    Status, VersionRange, negotiate,
};
pub use stage_evidence::{
    FileRef, SCHEMA as STAGE_EVIDENCE_SCHEMA, StageEvidence, StageEvidenceDocument,
    StageEvidenceError, StageKind, StageResult, StageSelection,
};
pub use vocabulary::{ErrorCode, ExecutionState, FailureDomain, OperationState};
