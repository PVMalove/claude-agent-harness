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
    /// State of a row of the `operations` table (ADR 0021, section 4).
    OperationState {
        Intent => "intent",
        Committed => "committed",
        Failed => "failed",
    }
}

vocabulary! {
    /// State of the execution of a dispatch (ADR 0021, section 6; ADR 0022, section 2).
    ExecutionState {
        Launching => "launching",
        Running => "running",
        Stopping => "stopping",
        Exited => "exited",
        Lost => "lost",
        Unknown => "unknown",
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

vocabulary! {
    /// Verdict of `qa.evidence.verify` (`qa_evidence_verify` of ADR 0021, section 7) on the stored
    /// QA evidence of one Release Gate pair.
    QaEvidenceVerdict {
        Ok => "ok",
        Refused => "refused",
    }
}

vocabulary! {
    /// Why `qa.evidence.verify` refuses a Release Gate pair (ADR 0023, section 5; ADR 0026,
    /// section 4).
    QaEvidenceViolation {
        PairNotRecorded => "pair_not_recorded",
        NoFullQa => "no_full_qa",
        SelectionNotFull => "selection_not_full",
        StageNotPassed => "stage_not_passed",
        StageCandidateMismatch => "stage_candidate_mismatch",
        PlanMismatch => "plan_mismatch",
        EvidenceUnverifiable => "evidence_unverifiable",
        CiNotCollected => "ci_not_collected",
    }
}
