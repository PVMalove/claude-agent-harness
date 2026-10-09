"""Граница динамического JSON харнесса.

`JsonObject` — объект из `json.loads`: ledger, конфигурация, отчёты ролей, health-отчёт и
статистика доставки. Код проверяет его поля на входе, поэтому значения имеют тип `Any`. Это
единственное намеренное исключение из `disallow_any_explicit` (ADR 0007). Строгий рекурсивный тип
записей ledger объявлен в `harness.orchestration.ledger.lifecycle`.
"""

from __future__ import annotations

from typing import Any

JsonObject = dict[str, Any]  # type: ignore[explicit-any]
