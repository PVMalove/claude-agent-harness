"""Состояние lock ledger для раздела Orchestration (issue #759).

Панель читает тот же read-only ответ, что и `coordinator.py ledger lock-status`, без захвата lock,
поэтому работает и тогда, когда ledger занят. Модуль не импортирует textual: текст панели
проверяется без терминала.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path

from harness.errors import HarnessError
from harness.json_types import JsonObject

LockLoader = Callable[[Path], JsonObject]

VERDICT_TEXT = {
    "owner-alive": "владелец жив; живой владелец не снимается",
    "owner-dead": "владелец завершился; ledger release-lock снимет lock",
    "owner-in-other-environment": (
        "владелец из другого PID-пространства или загрузки; pid не проверить, "
        "снятие по таймауту"
    ),
    "owner-on-another-host": "владелец на другом хосте; проверить его можно только там",
    "owner-unknown-recent": "запись владельца не читается; снятие по таймауту",
    "owner-unknown-stale": "владелец неизвестен, таймаут прошёл; ledger release-lock снимет lock",
}


def load_lock_status(repo: Path) -> JsonObject:
    """Read-only состояние lock ledger проекта ``repo``."""
    from harness.orchestration.ledger.ledger_admin import ledger_lock_status

    return ledger_lock_status(argparse.Namespace(repo=str(repo), state_dir=None))


def duration(seconds: int) -> str:
    """Длительность для оператора: «1 ч 05 мин», «3 мин 20 с», «45 с»."""
    seconds = max(0, seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} ч {minutes:02d} мин"
    if minutes:
        return f"{minutes} мин {secs:02d} с"
    return f"{secs} с"


def _owner_line(owner: object) -> str:
    if not isinstance(owner, dict):
        return "Владелец: запись не читается"
    namespace = owner.get("pid_namespace")
    where = f" · {namespace}" if isinstance(namespace, str) else ""
    return f"Владелец: pid {owner.get('pid')} · {owner.get('host')}{where}"


def lock_text(status: JsonObject) -> str:
    """Текст панели: кто держит lock, как долго, когда его можно снять и чем."""
    if not status.get("locked"):
        return "Lock ledger свободен."
    held = int(status.get("held_seconds") or 0)
    remaining = int(status.get("remaining_seconds") or 0)
    verdict = str(status.get("verdict"))
    if status.get("release_available"):
        timeout = "доступно"
    elif verdict == "owner-alive":
        timeout = f"{duration(remaining)} (таймаут не снимает живого владельца)"
    else:
        timeout = duration(remaining)
    operation = status.get("operation") or "операция не записана"
    return "\n".join(
        (
            f"Lock ledger занят: {operation}",
            _owner_line(status.get("owner")),
            f"Держится: {duration(held)} · до снятия по таймауту: {timeout}",
            f"Вердикт: {verdict}, {VERDICT_TEXT.get(verdict, 'неизвестный вердикт')}",
            "Снять: «Снять lock ledger» (ledger release-lock; --force только с одобрением "
            "оператора и причиной)",
        )
    )


def current_lock_text(repo: Path, load: LockLoader = load_lock_status) -> str:
    """Текст панели для ``repo``; ошибка чтения состояния тоже показывается оператору."""
    try:
        return lock_text(load(repo))
    except (HarnessError, OSError) as exc:
        return f"Lock ledger: состояние недоступно: {exc}"
