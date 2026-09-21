"""Fixed vocabulary of the orchestration coordinator.

Every name here is a compile-time constant of the lifecycle contract: record field sets, the
state-machine vocabularies a request is validated against, and the policy defaults a project may
override.  The module imports nothing from the harness, so every other orchestration module may
depend on it.
"""

from __future__ import annotations

import re
from pathlib import Path

STATE_REL = Path(".harness/orchestration/state")
SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|credential|password|secret|(?:access|auth|refresh|id|bearer)[_-]?token|(?:^|[_-])token(?:$|[_-](?:id|value|secret|key)$))",
    re.IGNORECASE,
)
REPORT_OUTCOMES = {"completed", "blocked", "failed"}
DECISIONS = {"accept", "override-warning", "retry", "block", "fail", "abandon"}
TERMINAL_BATCH_STATES = {"completed", "failed", "blocked", "not-required", "abandoned"}
# Why a role stopped, as the coordinator records it. Only the first two are operational evidence:
# they never change what a role would conclude, so they alone may re-run a read-only role (or the
# publish boundary) on the same candidate. Everything else, or anything unclear, needs a developer.
OPERATIONAL_REASON_CATEGORIES = (
    "verification-infrastructure",
    "transport",
    "context-pressure",
)
DEVELOPER_REASON_CATEGORIES = ("code", "requirements", "candidate-change")
RETRY_REASON_CATEGORIES = (
    *OPERATIONAL_REASON_CATEGORIES,
    *DEVELOPER_REASON_CATEGORIES,
    "unknown",
)
# The role a next-action dispatch runs as: ``publish`` is a purpose of the developer role.
NEXT_ACTION_DISPATCH_ROLE = {
    "architect": "architect",
    "developer-retry": "developer",
    "verification": "verification",
    "code-review": "code-review",
    "qa": "qa",
    "publish": "developer",
}
DISPATCH_PURPOSES = {"work", "verification", "publish"}
ROLE_TRANSPORTS = {"orca", "in-process"}
DEFAULT_ZONE = "repository"
DEFAULT_PROFILE = "session"
DEFAULT_STALE_AFTER_SECONDS = 900
DEFAULT_COMMUNICATION_POLICY = {
    "agent_to_agent_language": "en",
    "coordinator_report_language": "ru",
}
LIVE_DISPATCH_STATES = {"dispatched", "working"}
REVIEW_SEVERITIES = {"none", "clean", "warning", "blocker"}
FINDING_SEVERITIES = {"info", "warning", "blocker"}
QA_LEASE_FIELDS = {"dispatch_id", "host", "pid", "acquired_at", "expires_at"}
QA_QUEUE_FIELDS = {"dispatch_id", "sequence", "queued_at"}
PLAN_FIELDS = (
    "batch_id",
    "created_at",
    "base_commit",
    "integration_ref",
    "branch_start_commit",
    "ticket",
    "branch",
    "worktree",
    "zone",
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
)
LEGACY_PLAN_FIELDS = tuple(
    field
    for field in PLAN_FIELDS
    if field
    not in {"scope_preflight", "harness_runtime_sha256", "communication_policy"}
)
PRE_APPROVAL_LEGACY_PLAN_FIELDS = tuple(
    field for field in LEGACY_PLAN_FIELDS if field != "approval_policy"
)
DISPATCH_FIELDS = {
    "dispatch_id",
    "batch_id",
    "ticket",
    "role",
    "access",
    "zone",
    "write_paths",
    "branch",
    "worktree",
    "definition_of_done",
    "prohibited_changes",
    "verification_commands",
    "required_gates",
    "dependencies",
    "resolved_runtime",
    "resolved_provider_profile",
    "resolved_model",
    "resolved_effort",
    "resolved_transport",
    "coordinator_approval",
    "candidate_commit",
    "review_base",
    "review_scope",
    "risk_assessment_id",
    "purpose",
    "state",
    "created_at",
    "delta_review_of",
    "delta_review_axis",
    "context_package_id",
    "context_package_sha256",
    "context_package_summary",
    "worker_attestation_required",
    "communication_policy",
    "snapshot_commit",
    "report_staging_path",
    "allowed_tools",
    "context_budget",
    "transition",
    "transition_digest",
    "retry_idempotency_key",
    "orchestration_policy",
}
# The four fields of the transition-bound approval contract (issue #250) are all present or all absent.
POLICY_BRIEF_FIELDS = frozenset(
    {"transition", "transition_digest", "retry_idempotency_key", "orchestration_policy"}
)
DEFAULT_TEST_PATH_PATTERNS = ("tests/**", "**/tests/**", "**/test_*.py", "**/*_test.py")
REPORT_FIELDS = {
    "dispatch_id",
    "ticket",
    "role",
    "outcome",
    "output",
    "commit_sha",
    "changed_files",
    "checks_run",
    "risks",
    "blockers",
    "next_coordinator_action",
}
REPORT_OPTIONAL_FIELDS = {"risk_triggers", "review", "report_language"}
RISK_ASSESSMENT_FIELDS = {
    "risk_assessment_id",
    "batch_id",
    "candidate_commit",
    "base_commit",
    "changed_files",
    "matched_triggers",
    "developer_triggers",
    "review_required",
    "review_scope",
    "created_at",
}
CONTEXT_PACKAGE_FIELDS = {
    "context_package_id",
    "batch_id",
    "base_commit",
    "candidate_commit",
    "diff",
    "starting_files",
    "symbol_graph",
    "related_tests",
    "precedent_cards",
    "file_hashes",
    "size_bytes",
    "created_at",
    "role",
    "inclusion_reason",
    "estimated_tokens",
}
LEGACY_CONTEXT_PACKAGE_FIELDS = CONTEXT_PACKAGE_FIELDS - {"estimated_tokens"}
CHECKPOINT_NO_CONTEXT_PACKAGE = "not applicable — no context package registered"
CHECKPOINT_INPUT_FIELDS = {
    "dispatch_id",
    "commit_sha",
    "changed_files",
    "remaining_definition_of_done",
    "passing_checks",
    "risks",
    "blockers",
    "context_package_id",
}
CHECKPOINT_FIELDS = CHECKPOINT_INPUT_FIELDS | {
    "checkpoint_id",
    "batch_id",
    "created_at",
}
# Fixed runtime-adapter termination vocabulary, not a project policy value -- a rate-limit signal
# always authorizes a continuation automatically, whatever project a batch belongs to.
RATE_LIMIT_TERMINATION_REASONS = {"rate_limit", "rate-limit", "429"}
PLANNED_TRIGGER_KINDS = {"context-limit", "tdd-cycles", "failure-log", "vertical-slice"}
PLANNED_TRIGGER_THRESHOLD_KEY = {
    "context-limit": "context_limit",
    "tdd-cycles": "tdd_cycle_count",
    "failure-log": "failure_log_bytes",
}
DEFAULT_ADAPTIVE_CONTINUATION_POLICY = {
    "context_limit": 150_000,
    "tdd_cycle_count": 3,
    "failure_log_bytes": 20_000,
    "context_warn_ratio": 0.8,
}
DEFAULT_CONTEXT_PACKAGE_POLICY = {
    "max_tokens": 200_000,
    "context_window_tokens": 250_000,
    "reserved_prompt_tokens": 20_000,
    "symbol_graph_depth": 2,
    "max_related_tests": 25,
}
DEFAULT_CONTINUATION_POLICY = {"max_continuations": 2, "max_rate_limit_resumes": 1}
DEFAULT_RETRY_POLICY = {"max_developer_retries": 1}
DEFAULT_PREFLIGHT_POLICY = {
    "require_estimates": True,
    "max_definition_of_done_items": 5,
    "max_dependencies": 3,
    "max_expected_files": 12,
    "max_expected_services": 1,
    "max_expected_changed_lines": 800,
    "max_expected_context_tokens": 80_000,
}
DEFAULT_ATTENTION_POLICY = {
    "retry_queue_seconds": 3_600,
    "max_infrastructure_retries": 2,
    "stale_dispatch_seconds": DEFAULT_STALE_AFTER_SECONDS,
}
# Policy fields where zero is a meaningful "tolerate none"; every other numeric policy value is positive.
ZERO_ALLOWED_POLICY_FIELDS = {
    ("retry_policy", "max_developer_retries"),
    ("attention_policy", "max_infrastructure_retries"),
}
APPROVAL_CLOCK_SKEW_SECONDS = 300
# Only a value the provider or runtime observed is context telemetry; a model's own claim never is.
CONTEXT_TELEMETRY_SOURCES = ("probe", "provider-usage", "runtime-adapter")
CONTEXT_PRESSURE_FIELDS = {
    "pressure_id",
    "dispatch_id",
    "observed_tokens",
    "context_limit",
    "warning_threshold",
    "level",
    "recorded_at",
    "source",
    "action_required",
    "required_worker_action",
    "record_sha256",
}
ATTENTION_EVENT_KINDS = {"raised", "resolved"}
ATTENTION_STATE_FIELDS = (
    "attention_reason",
    "attention_since",
    "last_safe_action",
    "recommended_human_action",
)
DEFAULT_RATE_LIMIT_RETRY_SECONDS = 60
MAX_CHECK_EVIDENCE_CHARS = 1_600
CONTINUATION_FACTS_FIELDS = {
    "dispatch_id",
    "remaining_definition_of_done",
    "risks",
    "dependencies",
}
TELEMETRY_FIELDS = {
    "dispatch_id",
    "session_kind",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "max_context_tokens",
    "tool_calls",
    "tool_output_bytes",
    "poll_turns",
    "restart_reason",
    "recorded_at",
}


SCRATCH_REL = Path(".harness") / "scratch"
AGENT_INBOX_REL = SCRATCH_REL / "inbox"
# Cyrillic in a brief means the coordinator leaked its own report language into an agent handoff.
NON_ENGLISH_BRIEF_PATTERN = re.compile(r"[\u0400-\u04FF\u0500-\u052F]")
