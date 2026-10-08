"""Общие контракты отчётов, используемые при сборе данных, вычислениях и отображении."""

from __future__ import annotations

from typing import TypeGuard

from harness.errors import HarnessError
from harness.json_types import JsonObject as JsonObject

# Dynamic JSON enters from gh, transcripts, ledger records, and saved reports.

MISSING = "нет данных"
BASELINE_SCHEMA_VERSION = 1
CLAUDE_FIELDS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)
CODEX_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
)


class StatsError(HarnessError):
    """Ошибка запроса, на который невозможно ответить на основе локальных данных."""


def is_count(value: object) -> TypeGuard[int]:
    """Проверить, что поле телеметрии является целым числом; bool числом не считается."""
    return isinstance(value, int) and not isinstance(value, bool)


def _int(value: object) -> int:
    """Преобразовать целочисленное поле телеметрии, интерпретируя остальные значения как отсутствующие."""
    return value if is_count(value) else 0


def compact_count(value: int, space: str = " ") -> str:
    """Форматировать целое число в компактном виде с русскими суффиксами (тыс, млн, млрд).

    Знак сохраняется: отрицательная разница сравнения масштабируется так же, как положительная.
    `space` отделяет суффикс (терминал — пробел, HTML — неразрывный пробел).
    """
    magnitude = abs(value)
    for limit, suffix in ((1_000_000_000, "млрд"), (1_000_000, "млн"), (1_000, "тыс")):
        if magnitude >= limit:
            sign = "-" if value < 0 else ""
            scaled = (
                f"{magnitude / limit:.2f}".rstrip("0").rstrip(".").replace(".", ",")
            )
            return f"{sign}{scaled}{space}{suffix}"
    return str(value)
