//! Closed vocabularies of the IPC protocol (ADR 0021, section 3).
//!
//! The `systemFields.v1` registry (`harness/orchestration/system_fields.v1.json`) holds the same
//! values; `tests/registry_drift.rs` fails on any difference, in both directions.

use serde::{Deserialize, Serialize};

/// Error code of a runtime response, plus the client-side `runtime_unavailable`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorCode {
    ProtocolUnsupported,
    UnauthorizedPeer,
    MalformedRequest,
    UnknownOperation,
    ApprovalMismatch,
    RevisionConflict,
    StoreBusy,
    LeaseHeld,
    FencingMismatch,
    IdempotencyConflict,
    NotFound,
    InvariantViolation,
    DeadlineExceeded,
    EngineUnavailable,
    PolicyDenied,
    Internal,
    /// Client side only: the runtime does not answer, so the runtime never sends it.
    RuntimeUnavailable,
}

impl ErrorCode {
    /// Every code, in the order of the registry.
    pub const ALL: [Self; 17] = [
        Self::ProtocolUnsupported,
        Self::UnauthorizedPeer,
        Self::MalformedRequest,
        Self::UnknownOperation,
        Self::ApprovalMismatch,
        Self::RevisionConflict,
        Self::StoreBusy,
        Self::LeaseHeld,
        Self::FencingMismatch,
        Self::IdempotencyConflict,
        Self::NotFound,
        Self::InvariantViolation,
        Self::DeadlineExceeded,
        Self::EngineUnavailable,
        Self::PolicyDenied,
        Self::Internal,
        Self::RuntimeUnavailable,
    ];

    /// The exact machine value, as it travels in JSON and stands in the registry.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::ProtocolUnsupported => "protocol_unsupported",
            Self::UnauthorizedPeer => "unauthorized_peer",
            Self::MalformedRequest => "malformed_request",
            Self::UnknownOperation => "unknown_operation",
            Self::ApprovalMismatch => "approval_mismatch",
            Self::RevisionConflict => "revision_conflict",
            Self::StoreBusy => "store_busy",
            Self::LeaseHeld => "lease_held",
            Self::FencingMismatch => "fencing_mismatch",
            Self::IdempotencyConflict => "idempotency_conflict",
            Self::NotFound => "not_found",
            Self::InvariantViolation => "invariant_violation",
            Self::DeadlineExceeded => "deadline_exceeded",
            Self::EngineUnavailable => "engine_unavailable",
            Self::PolicyDenied => "policy_denied",
            Self::Internal => "internal",
            Self::RuntimeUnavailable => "runtime_unavailable",
        }
    }
}

/// Failure domain of an error: the taxonomy of ADR 0019.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FailureDomain {
    EnvironmentPreparation,
    Infrastructure,
    Policy,
    Tooling,
    CodeTest,
    Timeout,
    Cancelled,
    Unknown,
}

impl FailureDomain {
    /// Every domain, in the order of the registry.
    pub const ALL: [Self; 8] = [
        Self::EnvironmentPreparation,
        Self::Infrastructure,
        Self::Policy,
        Self::Tooling,
        Self::CodeTest,
        Self::Timeout,
        Self::Cancelled,
        Self::Unknown,
    ];

    /// The exact machine value, as it travels in JSON and stands in the registry.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::EnvironmentPreparation => "environment_preparation",
            Self::Infrastructure => "infrastructure",
            Self::Policy => "policy",
            Self::Tooling => "tooling",
            Self::CodeTest => "code_test",
            Self::Timeout => "timeout",
            Self::Cancelled => "cancelled",
            Self::Unknown => "unknown",
        }
    }
}
