"""Current state and usage statistics for the Harness and Orchestration sections.

Stdlib-only, like the rest of the textual-free seam: the installation state comes from
`.harness/harness.lock` and the skills directory, the pipeline statistics from a `LedgerView`
already read by harness.console.reports."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .json_fields import strings, text
from .reports import BatchSummary, LedgerView

# The batch lifecycle in order: planned → awaiting-approval ↔ active → a terminal state.
BATCH_STATES = (
    "planned",
    "awaiting-approval",
    "active",
    "blocked",
    "completed",
    "failed",
    "not-required",
    "abandoned",
)
STATE_LABELS = {
    "planned": "запланирован",
    "awaiting-approval": "ждёт подтверждения",
    "active": "в работе",
    "blocked": "заблокирован",
    "completed": "завершён",
    "failed": "провален",
    "not-required": "не требуется",
    "abandoned": "отменён",
}
ALL_STATES = "all"


@dataclass(frozen=True)
class PipelineStats:
    """How much the pipeline has been used and how its runs ended."""

    batches: int
    tickets: int
    by_state: dict[str, int] = field(default_factory=dict)
    dispatches_by_role: dict[str, int] = field(default_factory=dict)
    reports_by_outcome: dict[str, int] = field(default_factory=dict)
    qa_runs: int = 0
    qa_attempts_failed: int = 0
    first_at: str = ""
    last_at: str = ""


def pipeline_stats(view: LedgerView) -> PipelineStats:
    dates = sorted(batch.created_at for batch in view.batches if batch.created_at)
    return PipelineStats(
        batches=len(view.batches),
        tickets=len({batch.ticket for batch in view.batches}),
        by_state=dict(Counter(batch.state for batch in view.batches)),
        dispatches_by_role=dict(
            Counter(text(record.get("role"), "?") for record in view.dispatch_records())
        ),
        reports_by_outcome=dict(Counter(entry.outcome for entry in view.reports)),
        qa_runs=len(view.qa_runs),
        qa_attempts_failed=len(view.qa_attempts),
        first_at=dates[0] if dates else "",
        last_at=dates[-1] if dates else "",
    )


def short_time(value: str) -> str:
    """An ISO timestamp as `YYYY-MM-DD HH:MM`; anything else is returned unchanged."""
    return (
        value[:16].replace("T", " ") if len(value) >= 16 and value[10] == "T" else value
    )


def _counts(counts: dict[str, int]) -> str:
    return (
        ", ".join(f"{name} {count}" for name, count in sorted(counts.items())) or "нет"
    )


def pipeline_stats_text(stats: PipelineStats) -> str:
    if not stats.batches:
        return "Пайплайн ещё не запускался: batch в леджере нет."
    states = ", ".join(
        f"{STATE_LABELS.get(state, state)} {stats.by_state[state]}"
        for state in state_order(stats.by_state)
    )
    period = f"{short_time(stats.first_at) or '?'} — {short_time(stats.last_at) or '?'}"
    return "\n".join(
        (
            f"Запусков (batch): {stats.batches}, тикетов: {stats.tickets}",
            f"По состояниям: {states}",
            f"Диспатчи по ролям: {_counts(stats.dispatches_by_role)}",
            f"Отчёты по итогам: {_counts(stats.reports_by_outcome)}",
            f"QA-прогонов: {stats.qa_runs}, неудачных попыток qa-lane: {stats.qa_attempts_failed}",
            f"Период: {period}",
        )
    )


def state_order(by_state: dict[str, int]) -> list[str]:
    """Known lifecycle states in lifecycle order, then any unknown state the ledger holds."""
    known = [state for state in BATCH_STATES if by_state.get(state)]
    return known + sorted(state for state in by_state if state not in BATCH_STATES)


def state_filter_items(view: LedgerView) -> list[tuple[str, str]]:
    """(key, label) for the state filter: every lifecycle state with its count, "all" first."""
    counts = Counter(batch.state for batch in view.batches)
    items = [(ALL_STATES, f"все ({len(view.batches)})")]
    states = list(BATCH_STATES) + sorted(s for s in counts if s not in BATCH_STATES)
    items.extend(
        (state, f"{STATE_LABELS.get(state, state)} ({counts.get(state, 0)})")
        for state in states
    )
    return items


def batches_in_state(view: LedgerView, state: str) -> list[BatchSummary]:
    """The batches in one state, newest first; `ALL_STATES` returns every batch."""
    if state == ALL_STATES:
        return list(view.batches)
    return [batch for batch in view.batches if batch.state == state]


def batch_label(batch: BatchSummary) -> str:
    state = STATE_LABELS.get(batch.state, batch.state)
    return f"{batch.ticket} · {state} · {short_time(batch.created_at) or 'дата неизвестна'}\n  {batch.batch_id}"


@dataclass(frozen=True)
class HarnessState:
    """What is installed in the repository."""

    installed: bool
    installed_version: str
    capabilities: tuple[str, ...]
    skills: int
    managed_files: int
    updated_at: str
    orchestration: bool


def harness_state(repo: Path) -> HarnessState:
    lock_path = repo / ".harness" / "harness.lock"
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = None
    skills_dir = repo / ".harness" / "skills"
    skills = (
        sum(1 for path in skills_dir.iterdir() if (path / "SKILL.md").is_file())
        if skills_dir.is_dir()
        else 0
    )
    if not isinstance(payload, dict):
        return HarnessState(False, "", (), skills, 0, "", False)
    files = payload.get("files")
    updated = datetime.fromtimestamp(lock_path.stat().st_mtime, tz=timezone.utc)
    return HarnessState(
        installed=True,
        installed_version=text(payload.get("package_version"), "?"),
        capabilities=tuple(strings(payload.get("capabilities"))),
        skills=skills,
        managed_files=len(files) if isinstance(files, dict) else 0,
        updated_at=updated.strftime("%Y-%m-%d %H:%M UTC"),
        orchestration=(
            repo / ".harness" / "orchestration" / "coordinator.py"
        ).is_file(),
    )


def harness_state_text(state: HarnessState, harness_version: str) -> str:
    if not state.installed:
        return "Харнесс не установлен: нет читаемого .harness/harness.lock."
    version = state.installed_version
    if version != harness_version:
        version += f" (версия пакета харнесса: {harness_version})"
    return "\n".join(
        (
            f"Установлена версия: {version}",
            f"Capability: {', '.join(state.capabilities) or 'нет'}",
            f"Скилов: {state.skills}, управляемых файлов: {state.managed_files}",
            f"Lock обновлён: {state.updated_at}",
            f"Оркестрация: {'подключена' if state.orchestration else 'не подключена'}",
        )
    )


def usage_text(view: LedgerView) -> str:
    """Pipeline usage for the Harness section: a one-line summary of the ledger."""
    if view.unavailable:
        return f"Использование пайплайна: {view.unavailable}"
    stats = pipeline_stats(view)
    return (
        f"Использование пайплайна: запусков {stats.batches}, тикетов {stats.tickets}, "
        f"диспатчей {sum(stats.dispatches_by_role.values())}, отчётов "
        f"{sum(stats.reports_by_outcome.values())}"
    )
