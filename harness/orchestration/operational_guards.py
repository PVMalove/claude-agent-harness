"""Pure operational guards of the orchestration coordinator (issue #250).

Nothing here touches git, the ledger or the clock.  The coordinator gathers facts and passes them
in; these functions only canonicalise, hash and classify them, so the same inputs always produce
the same digest, key, level or finding.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

from ..errors import HarnessError

# Everything an approval binds.  A dispatch that differs in any one of these needs a new approval.
TRANSITION_FIELDS = (
    "batch_id",
    "previous_dispatch_id",
    "previous_role",
    "reason_category",
    "next_role",
    "next_action",
    "purpose",
    "candidate_sha",
    "base_sha",
    "review_scope",
    "verification_commands",
    "context_package_id",
    "required_gates",
)
# A role that only reads (or the publish boundary) is re-run as a new dispatch, never resumed, so it
# alone carries a retry idempotency key.
KEYED_READ_ONLY_ROLES = ("architect", "code-review", "qa")
ATTENTION_REASONS = (
    "unknown-reason",
    "infrastructure-retry-repeated",
    "retry-queued-too-long",
    "stale-evidence",
    "stale-dispatch",
)
CONTEXT_PRESSURE_LEVELS = ("ok", "warning", "critical")


class GuardError(HarnessError):
    """A guard input is malformed."""


def _digest(value: object) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_transition(
    *,
    batch_id: str,
    previous_dispatch_id: str | None,
    previous_role: str | None,
    reason_category: str | None,
    next_role: str,
    next_action: str,
    purpose: str,
    candidate_sha: str | None,
    base_sha: str,
    review_scope: Sequence[str],
    verification_commands: Sequence[str],
    context_package_id: str | None,
    required_gates: Sequence[str],
) -> dict[str, object]:
    return {
        "batch_id": batch_id,
        "previous_dispatch_id": previous_dispatch_id,
        "previous_role": previous_role,
        "reason_category": reason_category,
        "next_role": next_role,
        "next_action": next_action,
        "purpose": purpose,
        "candidate_sha": candidate_sha,
        "base_sha": base_sha,
        "review_scope": list(review_scope),
        "verification_commands": list(verification_commands),
        "context_package_id": context_package_id,
        "required_gates": list(required_gates),
    }


def transition_digest(transition: Mapping[str, object]) -> str:
    """The canonical digest an approval is bound to; key order never matters."""
    if set(transition) != set(TRANSITION_FIELDS):
        raise GuardError(
            "a transition must carry exactly the fields an approval binds",
            remedy=f"provide exactly: {', '.join(TRANSITION_FIELDS)}",
        )
    return _digest(dict(transition))


def keyed_role(role: str, purpose: str) -> str | None:
    """The role name an idempotency key uses, or ``None`` when the dispatch is not read-only recovery."""
    if purpose == "publish":
        return "publish"
    return role if role in KEYED_READ_ONLY_ROLES else None


def retry_idempotency_key(
    *,
    role: str,
    candidate_sha: str | None,
    base_sha: str,
    review_scope: Sequence[str],
    reason_category: str,
    verification_commands: Sequence[str],
) -> str:
    """One key per (role, candidate, base, scope, reason, verification): equal keys are the same request."""
    return _digest(
        {
            "role": role,
            "candidate_sha": candidate_sha,
            "base_sha": base_sha,
            "review_scope": list(review_scope),
            "reason_category": reason_category,
            "verification_commands_digest": _digest(list(verification_commands)),
        }
    )


def level_for(observed_tokens: int, context_limit: int, warning_threshold: int) -> str:
    if observed_tokens >= context_limit:
        return "critical"
    return "warning" if observed_tokens >= warning_threshold else "ok"


def pressure_level(
    observed_tokens: int, context_limit: int, warning_ratio: float
) -> tuple[int, str]:
    """``(warning_threshold, level)`` for an observed token count."""
    warning_threshold = round(context_limit * warning_ratio)
    return warning_threshold, level_for(
        observed_tokens, context_limit, warning_threshold
    )


def attention_finding(
    reason: str,
    subject: str,
    *,
    last_safe_action: str,
    recommended_human_action: str,
) -> dict[str, str]:
    """One reason a batch needs a human; ``key`` identifies the occurrence so it is acknowledged once."""
    if reason not in ATTENTION_REASONS:
        raise GuardError(
            f"unknown attention reason {reason!r}",
            remedy=f"use one of: {', '.join(ATTENTION_REASONS)}",
        )
    return {
        "key": f"{reason}:{subject}",
        "reason": reason,
        "last_safe_action": last_safe_action,
        "recommended_human_action": recommended_human_action,
    }


def top_finding(findings: Sequence[dict[str, str]]) -> dict[str, str]:
    """The most urgent finding: the earliest reason in ``ATTENTION_REASONS``."""
    return min(findings, key=lambda finding: ATTENTION_REASONS.index(finding["reason"]))
