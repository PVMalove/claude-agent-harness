"""Чистые операционные защитные правила координатора оркестрации (issue #250).

Ничто здесь не взаимодействует с git, реестром или системными часами. Координатор собирает
факты и передаёт их сюда; данные функции лишь канонизируют, хешируют и классифицируют их,
поэтому одинаковые входные данные всегда дают один и тот же дайджест, ключ, уровень или замечание.
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
# Bound only when a brief carries a non-empty carried-items section (issue #499) or a human-approved
# rebase target (issue #504), so every transition without one keeps the digest it always had.
OPTIONAL_TRANSITION_FIELDS = ("carried_items_sha256", "rebase_target_sha")
# A new dispatch (issue #624) always binds the sha256 of its resolved runtime access plan.
ACCESS_TRANSITION_FIELD = "runtime_access_sha256"
# A role that only reads (or the publish boundary) is re-run as a new dispatch, never resumed, so it
# alone carries a retry idempotency key.
KEYED_READ_ONLY_ROLES = ("architect", "code-review", "qa")
ATTENTION_REASONS = (
    "unknown-reason",
    "infrastructure-retry-repeated",
    "tooling-retry-repeated",
    "retry-queued-too-long",
    "stale-evidence",
    "stale-dispatch",
)
# A tool that blocks the same candidate a third time in a row needs a fixed tool, not another re-run
# (issue #500).
MAX_CONSECUTIVE_TOOLING_RETRIES = 2
CONTEXT_PRESSURE_LEVELS = ("ok", "warning", "critical")


class GuardError(HarnessError):
    """Входные данные защитного правила некорректны."""


def _digest(value: object) -> str:
    """Вычислить SHA256-хеш канонического представления объекта в формате JSON."""
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
    carried_items_sha256: str | None = None,
    rebase_target_sha: str | None = None,
) -> dict[str, object]:
    """Сконструировать словарь перехода между этапами жизненного цикла."""
    transition: dict[str, object] = {
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
    if carried_items_sha256 is not None:
        transition["carried_items_sha256"] = carried_items_sha256
    if rebase_target_sha is not None:
        transition["rebase_target_sha"] = rebase_target_sha
    return transition


def carried_items_digest(section: Mapping[str, object]) -> str:
    """Дайджест секции carried_items задания, который связывается с переходом (issue #499)."""
    return _digest(dict(section))


def transition_digest(transition: Mapping[str, object]) -> str:
    """Канонический дайджест, с которым связывается подтверждение; порядок ключей не имеет значения."""
    extra = {*OPTIONAL_TRANSITION_FIELDS, ACCESS_TRANSITION_FIELD}
    if set(transition) - extra != set(TRANSITION_FIELDS):
        raise GuardError(
            "a transition must carry exactly the fields an approval binds",
            remedy=f"provide exactly: {', '.join(TRANSITION_FIELDS)}, plus "
            "carried_items_sha256 only when the brief carries items, rebase_target_sha only "
            "when it carries an approved rebase target and runtime_access_sha256 for a new brief",
        )
    return _digest(dict(transition))


def keyed_role(role: str, purpose: str) -> str | None:
    """Имя роли для ключа идемпотентности или None, если диспетчеризация не является восстановлением только для чтения."""
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
    """Один ключ на комбинацию (роль, кандидат, база, область, причина, проверки): одинаковые ключи означают один и тот же запрос."""
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
    """Определить уровень давления контекста (ok, warning, critical) по наблюдаемому числу токенов."""
    if observed_tokens >= context_limit:
        return "critical"
    return "warning" if observed_tokens >= warning_threshold else "ok"


def pressure_level(
    observed_tokens: int, context_limit: int, warning_ratio: float
) -> tuple[int, str]:
    """Вернуть кортеж (warning_threshold, level) для наблюдаемого количества токенов."""
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
    """Сформировать замечание о необходимости внимания человека; key идентифицирует событие для однократного подтверждения."""
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


def tooling_retry_streak(decisions: Sequence[Mapping[str, object]]) -> int:
    """Число подряд идущих последних решений с маршрутом ``tooling-retry`` на одном candidate.

    Серию обрывает первое другое решение (accept, другой маршрут) или другой ``candidate_commit``
    в routing record; ``None`` равен ``None`` (у architect candidate нет).
    """
    streak = 0
    candidate: object = None
    for decision in reversed(decisions):
        routing = decision.get("routing")
        if not isinstance(routing, Mapping) or routing.get("route") != "tooling-retry":
            break
        if streak and routing.get("candidate_commit") != candidate:
            break
        candidate = routing.get("candidate_commit")
        streak += 1
    return streak


def top_finding(findings: Sequence[dict[str, str]]) -> dict[str, str]:
    """Наиболее приоритетное замечание: причина с наименьшим индексом в ATTENTION_REASONS."""
    return min(findings, key=lambda finding: ATTENTION_REASONS.index(finding["reason"]))
