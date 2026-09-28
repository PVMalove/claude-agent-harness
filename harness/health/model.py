"""Модели результатов и исправлений, используемые реестром проверок здоровья (см. registry.py)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# Raw JSON crosses into this package at exactly two points: the parsed harness.lock
# (context.HealthContext) and the --json report (report_json.to_json). Matches the existing idiom
# in harness/reporting/common.py.
JsonObject = dict[str, Any]  # type: ignore[explicit-any]

Status = Literal["ok", "warn", "fail", "skipped"]
_STATUSES: tuple[Status, ...] = ("ok", "warn", "fail", "skipped")


@dataclass(frozen=True)
class Fix:
    """Действие по исправлению, прикреплённое к неуспешному результату :class:`CheckResult`."""

    text: str
    command: str | None = None


@dataclass(frozen=True)
class CheckResult:
    """Результат выполнения одной проверки здоровья.

    Поле `id` — стабильный точечный идентификатор, входящий в контракт `--json`.
    Поле `group` — машинный токен группы; человекочитаемое название (на русском языке)
    присваивается ему только в render.py, здесь локализация не выполняется.
    Поле `message` — всегда понятное человеку предложение на русском языке,
    в том числе при `status == "ok"`.
    """

    id: str
    group: str
    status: Status
    message: str
    fix: Fix | None = None


@dataclass
class Report:
    """Полный результат одного запуска `harness health`."""

    schema_version: int
    repo: str
    online: bool
    checks: list[CheckResult] = field(default_factory=list)
    fixes_applied: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        """Подсчитать количество проверок по каждому из возможных статусов."""
        counts: dict[str, int] = {status: 0 for status in _STATUSES}
        for check in self.checks:
            counts[check.status] += 1
        return counts
