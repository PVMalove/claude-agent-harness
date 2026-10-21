//! Closed vocabularies of the IPC protocol (ADR 0021, section 3).
//!
//! The `systemFields.v1` registry (`harness/orchestration/system_fields.v1.json`) holds the same
//! values; `tests/registry_drift.rs` fails on any difference, in both directions. Each vocabulary
//! is written once: the enum, `ALL` and `as_str` come from the same list, so a variant cannot be
//! missing from `ALL` and the drift test sees every variant.

use serde::{Deserialize, Serialize};

macro_rules! vocabulary {
    (
        $(#[$meta:meta])*
        $name:ident {
            $($(#[$variant_meta:meta])* $variant:ident => $text:literal,)+
        }
    ) => {
        $(#[$meta])*
        #[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
        #[serde(rename_all = "snake_case")]
        pub enum $name {
            $($(#[$variant_meta])* $variant,)+
        }

        impl $name {
            /// Every value, in the order of the registry.
            pub const ALL: [Self; [$($text),+].len()] = [$(Self::$variant),+];

            /// The exact machine value, as it travels in JSON and stands in the registry.
            pub const fn as_str(self) -> &'static str {
                match self {
                    $(Self::$variant => $text,)+
                }
            }
        }
    };
}

vocabulary! {
    /// Error code of a runtime response, plus the client-side `runtime_unavailable`.
    ErrorCode {
        ProtocolUnsupported => "protocol_unsupported",
        UnauthorizedPeer => "unauthorized_peer",
        MalformedRequest => "malformed_request",
        UnknownOperation => "unknown_operation",
        ApprovalMismatch => "approval_mismatch",
        RevisionConflict => "revision_conflict",
        StoreBusy => "store_busy",
        LeaseHeld => "lease_held",
        FencingMismatch => "fencing_mismatch",
        IdempotencyConflict => "idempotency_conflict",
        NotFound => "not_found",
        InvariantViolation => "invariant_violation",
        DeadlineExceeded => "deadline_exceeded",
        EngineUnavailable => "engine_unavailable",
        PolicyDenied => "policy_denied",
        Internal => "internal",
        /// Client side only: the runtime does not answer, so the runtime never sends it.
        RuntimeUnavailable => "runtime_unavailable",
    }
}

vocabulary! {
    /// Failure domain of an error: the taxonomy of ADR 0019.
    FailureDomain {
        EnvironmentPreparation => "environment_preparation",
        Infrastructure => "infrastructure",
        Policy => "policy",
        Tooling => "tooling",
        CodeTest => "code_test",
        Timeout => "timeout",
        Cancelled => "cancelled",
        Unknown => "unknown",
    }
}
