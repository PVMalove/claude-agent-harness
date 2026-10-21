"""Python bridge to the Rust ``harness-runtime`` process (ADR 0019, ADR 0021).

The closed vocabularies of the IPC protocol live here next to the client code. The
``systemFields.v1`` registry holds the same values, and ``native/harness-runtime/crates/contracts``
holds them on the Rust side; a drift check in each language fails CI on any difference.
"""

from __future__ import annotations

# ADR 0021, section 3: the error codes the runtime answers with, then the client-side code for a
# runtime that does not answer.
RUNTIME_ERROR_CODES = (
    "protocol_unsupported",
    "unauthorized_peer",
    "malformed_request",
    "unknown_operation",
    "approval_mismatch",
    "revision_conflict",
    "store_busy",
    "lease_held",
    "fencing_mismatch",
    "idempotency_conflict",
    "not_found",
    "invariant_violation",
    "deadline_exceeded",
    "engine_unavailable",
    "policy_denied",
    "internal",
    "runtime_unavailable",
)
# The failure-domain taxonomy of ADR 0019.
FAILURE_DOMAINS = (
    "environment_preparation",
    "infrastructure",
    "policy",
    "tooling",
    "code_test",
    "timeout",
    "cancelled",
    "unknown",
)
