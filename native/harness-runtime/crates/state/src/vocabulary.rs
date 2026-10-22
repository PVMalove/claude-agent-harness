//! The vocabularies and field sets of the file ledger that the import checks. `tests/orchestration/
//! test_runtime_store_canonical.py` fails when a list differs from its Python source in
//! `harness/orchestration/core/constants.py` and the modules that name it.

pub const BATCH_STATES: [&str; 9] = [
    "planned",
    "awaiting-approval",
    "active",
    "blocked",
    "failed",
    "completed",
    "not-required",
    "abandoned",
    "paused",
];

pub const DISPATCH_STATES: [&str; 11] = [
    "proposed",
    "approved",
    "dispatched",
    "working",
    "blocked",
    "failed",
    "reported",
    "checkpointed",
    "rate_limited",
    "abandoned",
    "cancelled",
];

/// `RecoveryRoute`.
pub const RECOVERY_ROUTES: [&str; 16] = [
    "developer-retry",
    "same-candidate-rerun",
    "verification",
    "architect-retry",
    "abandon",
    "report-completion",
    "carry-over",
    "tooling-retry",
    "bypass-rerun",
    "narrowed-retry",
    "fix-forward",
    "rebase-fix-forward",
    "supersede",
    "environmental-restart",
    "rewind",
    "accept-conditional",
];

/// `RetryReason`.
pub const RETRY_REASONS: [&str; 9] = [
    "verification-infrastructure",
    "transport",
    "context-pressure",
    "tooling",
    "block-bypass",
    "code",
    "requirements",
    "candidate-change",
    "unknown",
];

/// `CONTEXT_TELEMETRY_SOURCES`.
pub const CONTEXT_TELEMETRY_SOURCES: [&str; 3] = ["probe", "provider-usage", "runtime-adapter"];

/// `CONTEXT_PRESSURE_FIELDS`, sorted.
pub const CONTEXT_PRESSURE_FIELDS: [&str; 11] = [
    "action_required",
    "context_limit",
    "dispatch_id",
    "level",
    "observed_tokens",
    "pressure_id",
    "record_sha256",
    "recorded_at",
    "required_worker_action",
    "source",
    "warning_threshold",
];

/// `CARRIED_ITEM_RECORD_FIELDS`, sorted.
pub const CARRIED_ITEM_RECORD_FIELDS: [&str; 8] = [
    "attached_at",
    "attached_by",
    "expected_evidence",
    "files",
    "item_id",
    "record_sha256",
    "source",
    "summary",
];

/// `ATTENTION_REASONS`.
pub const ATTENTION_REASONS: [&str; 6] = [
    "unknown-reason",
    "infrastructure-retry-repeated",
    "tooling-retry-repeated",
    "retry-queued-too-long",
    "stale-evidence",
    "stale-dispatch",
];

/// `ATTENTION_STATE_FIELDS`.
pub const ATTENTION_STATE_FIELDS: [&str; 4] = [
    "attention_reason",
    "attention_since",
    "last_safe_action",
    "recommended_human_action",
];

/// `ATTENTION_EVENT_KINDS`, sorted.
pub const ATTENTION_EVENT_KINDS: [&str; 2] = ["raised", "resolved"];

/// `AUTO_DECISION_KINDS`.
pub const AUTO_DECISION_KINDS: [&str; 5] = [
    "batch-approve",
    "dispatch",
    "continuation",
    "decision",
    "carry-over",
];

/// `AUTO_DECISION_FIELDS`, sorted.
pub const AUTO_DECISION_FIELDS: [&str; 8] = [
    "approved_at",
    "approved_by",
    "dispatch_id",
    "evidence",
    "kind",
    "rationale",
    "record_sha256",
    "sequence",
];

/// `AUTO_EVIDENCE_FIELDS` of the kind `batch-approve`, sorted.
pub const AUTO_EVIDENCE_BATCH_APPROVE: [&str; 3] = [
    "definition_of_done_items",
    "plan_sha256",
    "scope_preflight_status",
];

/// `AUTO_EVIDENCE_FIELDS` of the kind `dispatch`, sorted.
pub const AUTO_EVIDENCE_DISPATCH: [&str; 6] = [
    "brief_sha256",
    "lifted_milestones",
    "reason_category",
    "report_sha256",
    "route_preview",
    "transition_digest",
];

/// `AUTO_EVIDENCE_FIELDS` of the kind `continuation`, sorted.
pub const AUTO_EVIDENCE_CONTINUATION: [&str; 4] = [
    "checkpoint_id",
    "continuations_spent",
    "max_continuations",
    "trigger",
];

/// `AUTO_EVIDENCE_FIELDS` of the kind `decision`, sorted.
pub const AUTO_EVIDENCE_DECISION: [&str; 9] = [
    "accepted_risks",
    "basis",
    "bug_ticket",
    "carried_item_ids",
    "commit_plan_sha256",
    "decision",
    "reason_category",
    "report_sha256",
    "route_preview",
];

/// `AUTO_EVIDENCE_FIELDS` of the kind `carry-over`, sorted.
pub const AUTO_EVIDENCE_CARRY_OVER: [&str; 2] = ["carried_item_ids", "report_sha256"];

/// `AUTO_STOP_REASONS` of the category `budget-exhausted`.
pub const AUTO_STOP_BUDGET_EXHAUSTED: [&str; 5] = [
    "retry_policy.max_developer_retries",
    "continuation_policy.max_continuations",
    "continuation_policy.max_rate_limit_resumes",
    "attention_policy.max_infrastructure_retries",
    "tooling-retry-repeated",
];

/// `AUTO_STOP_REASONS` of the category `integrity-failure`.
pub const AUTO_STOP_INTEGRITY_FAILURE: [&str; 7] = [
    "runtime-unavailable",
    "stale",
    "model-mismatch",
    "worktree-mismatch",
    "harness-snapshot-changed",
    "deterministic-gate-failed",
    "ledger-validation-failed",
];

/// `AUTO_STOP_REASONS` of the category `no-automatic-route`.
pub const AUTO_STOP_NO_AUTOMATIC_ROUTE: [&str; 4] = [
    "unknown-reason",
    "abandon-dead-end",
    "supersede-dead-end",
    "qa-environment-failure",
];

/// `AUTO_STOP_FIELDS`, sorted.
pub const AUTO_STOP_FIELDS: [&str; 6] = [
    "category",
    "detected_at",
    "detected_by",
    "evidence",
    "reason",
    "record_sha256",
];

/// `AUTO_REPORT_FIELDS`, sorted.
pub const AUTO_REPORT_FIELDS: [&str; 19] = [
    "accepted_risks",
    "batch_id",
    "branch",
    "budget",
    "candidate_commit",
    "commit_plan",
    "decisions",
    "dod_coverage",
    "findings",
    "next_human_action",
    "outcome",
    "qa",
    "record_sha256",
    "recorded_at",
    "retries",
    "review",
    "schema_version",
    "stop",
    "ticket",
];

/// `EVENT_FIELDS` of a recovery event, sorted.
pub const RECOVERY_EVENT_FIELDS: [&str; 13] = [
    "approved_at",
    "approved_by",
    "before_state",
    "dispatch_id",
    "evidence",
    "kind",
    "next_action",
    "note",
    "record_sha256",
    "sequence",
    "snapshot_commit",
    "state",
    "transition_digest",
];

/// `FIELDS` of a startup evidence point, sorted.
pub const STARTUP_EVIDENCE_FIELDS: [&str; 7] = [
    "brief_sha256",
    "commit_sha",
    "dispatch_id",
    "record_sha256",
    "recorded_at",
    "scope_sha256",
    "worktree",
];

/// The keys of the `preserved_progress` evidence of a rewind, sorted.
pub const PRESERVED_PROGRESS_FIELDS: [&str; 7] = [
    "checkout",
    "files",
    "head_commit",
    "index_sha256",
    "snapshot_commit",
    "source_dispatch_id",
    "worktree_sha256",
];

/// `PLAN_FIELDS`: the fields a batch record and its immutable plan share.
pub const PLAN_FIELDS: [&str; 20] = [
    "batch_id",
    "created_at",
    "base_commit",
    "integration_ref",
    "branch_start_commit",
    "ticket",
    "branch",
    "worktree",
    "zone",
    "allowed_paths",
    "definition_of_done",
    "prohibited_changes",
    "developer_verification_commands",
    "verification_commands",
    "required_gates",
    "dependencies",
    "approval_policy",
    "communication_policy",
    "scope_preflight",
    "harness_runtime_sha256",
];

/// The fields of `PLAN_FIELDS` that a plan written before the explicit scope, the scope
/// preflight, the runtime pin and the communication policy did not have: `LEGACY_PLAN_FIELDS` is
/// `PLAN_FIELDS` without them.
pub const PLAN_FIELDS_BEFORE_PIN: [&str; 4] = [
    "allowed_paths",
    "scope_preflight",
    "harness_runtime_sha256",
    "communication_policy",
];
