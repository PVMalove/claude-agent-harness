"""Состояние lock ledger для раздела Orchestration (issue #759).

Панель читает тот же read-only ответ, что и `coordinator.py ledger lock-status`, без захвата lock,
поэтому работает и тогда, когда ledger занят. Текст строится сразу на языке консоли: в нём числа
и единицы времени, которые перевод по фразам исказил бы. Модуль не импортирует textual: текст
панели проверяется без терминала.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from harness.errors import HarnessError
from harness.json_types import JsonObject

LockLoader = Callable[[Path], JsonObject]


@dataclass(frozen=True)
class _Words:
    """Тексты панели на одном языке."""

    free: str
    held: str
    no_operation: str
    owner: str
    owner_unreadable: str
    age: str
    available: str
    alive_timeout: str
    host_timeout: str
    verdict: str
    unknown_verdict: str
    release: str
    unavailable: str
    units: tuple[str, str, str]
    verdicts: dict[str, str] = field(default_factory=dict)


_RU = _Words(
    free="Lock ledger свободен.",
    held="Lock ledger занят: {operation}",
    no_operation="операция не записана",
    owner="Владелец: pid {pid} · {host}{where}",
    owner_unreadable="Владелец: запись не читается",
    age="Держится: {held} · до снятия по таймауту: {timeout}",
    available="доступно",
    alive_timeout="{remaining} (таймаут не снимает живого владельца)",
    host_timeout="не применяется (владелец на другом хосте)",
    verdict="Вердикт: {verdict}, {meaning}",
    unknown_verdict="неизвестный вердикт",
    release="Снять: «Снять lock ledger» (ledger release-lock; --force только с одобрением "
    "оператора и причиной)",
    unavailable="Lock ledger: состояние недоступно: {error}",
    units=("ч", "мин", "с"),
    verdicts={
        "owner-alive": "владелец жив; живой владелец не снимается",
        "owner-dead": "владелец завершился; ledger release-lock снимет lock",
        "owner-in-other-environment": "владелец из другого PID-пространства или загрузки; "
        "pid не проверить, снятие по таймауту",
        "owner-on-another-host": "владелец на другом хосте; проверить его можно только там",
        "owner-unknown-recent": "запись владельца не читается; снятие по таймауту",
        "owner-unknown-stale": "владелец неизвестен, таймаут прошёл; ledger release-lock "
        "снимет lock",
    },
)
_EN = _Words(
    free="The ledger lock is free.",
    held="Ledger lock held: {operation}",
    no_operation="operation not recorded",
    owner="Owner: pid {pid} · {host}{where}",
    owner_unreadable="Owner: record cannot be read",
    age="Held for: {held} · until the timeout release: {timeout}",
    available="available",
    alive_timeout="{remaining} (the timeout never releases a live owner)",
    host_timeout="not applicable (owner on another host)",
    verdict="Verdict: {verdict}, {meaning}",
    unknown_verdict="unknown verdict",
    release="Release: “Release the ledger lock” (ledger release-lock; --force only with an "
    "operator's approval and a reason)",
    unavailable="Ledger lock: state unavailable: {error}",
    units=("h", "min", "s"),
    verdicts={
        "owner-alive": "the owner is alive; a live owner is never released",
        "owner-dead": "the owner has exited; ledger release-lock releases the lock",
        "owner-in-other-environment": "the owner is in another PID namespace or boot; its "
        "pid cannot be checked, released by the timeout",
        "owner-on-another-host": "the owner is on another host; only there can it be checked",
        "owner-unknown-recent": "the owner record cannot be read; released by the timeout",
        "owner-unknown-stale": "the owner is unknown and the timeout has passed; ledger "
        "release-lock releases it",
    },
)
_WORDS = {"ru": _RU, "en": _EN}


def _words(language: str) -> _Words:
    return _WORDS.get(language, _RU)


def load_lock_status(repo: Path) -> JsonObject:
    """Read-only состояние lock ledger проекта ``repo``."""
    from harness.orchestration.ledger.ledger_admin import ledger_lock_status

    return ledger_lock_status(argparse.Namespace(repo=str(repo), state_dir=None))


def duration(seconds: int, language: str = "ru") -> str:
    """Длительность для оператора: «1 ч 05 мин», «3 мин 20 с», «45 с»."""
    hours_unit, minutes_unit, seconds_unit = _words(language).units
    seconds = max(0, seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} {hours_unit} {minutes:02d} {minutes_unit}"
    if minutes:
        return f"{minutes} {minutes_unit} {secs:02d} {seconds_unit}"
    return f"{secs} {seconds_unit}"


def _owner_line(owner: object, words: _Words) -> str:
    if not isinstance(owner, dict):
        return words.owner_unreadable
    namespace = owner.get("pid_namespace")
    where = f" · {namespace}" if isinstance(namespace, str) else ""
    return words.owner.format(pid=owner.get("pid"), host=owner.get("host"), where=where)


def lock_text(status: JsonObject, language: str = "ru") -> str:
    """Текст панели: кто держит lock, как долго, когда его можно снять и чем."""
    words = _words(language)
    if not status.get("locked"):
        return words.free
    held = int(status.get("held_seconds") or 0)
    remaining = int(status.get("remaining_seconds") or 0)
    verdict = str(status.get("verdict"))
    if status.get("release_available"):
        timeout = words.available
    elif verdict == "owner-alive":
        timeout = words.alive_timeout.format(remaining=duration(remaining, language))
    elif verdict == "owner-on-another-host":
        timeout = words.host_timeout
    else:
        timeout = duration(remaining, language)
    return "\n".join(
        (
            words.held.format(operation=status.get("operation") or words.no_operation),
            _owner_line(status.get("owner"), words),
            words.age.format(held=duration(held, language), timeout=timeout),
            words.verdict.format(
                verdict=verdict,
                meaning=words.verdicts.get(verdict, words.unknown_verdict),
            ),
            words.release,
        )
    )


def current_lock_text(
    repo: Path, load: LockLoader = load_lock_status, language: str = "ru"
) -> str:
    """Текст панели для ``repo``; ошибка чтения состояния тоже показывается оператору."""
    try:
        return lock_text(load(repo), language)
    except (HarnessError, OSError) as exc:
        return _words(language).unavailable.format(error=exc)
