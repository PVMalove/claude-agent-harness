"""Общие контракты отчётов, используемые при сборе данных, вычислениях и отображении."""

from __future__ import annotations

from typing import Any

from harness.errors import HarnessError

# Dynamic JSON enters from gh, transcripts, ledger records, and saved reports.
JsonObject = dict[str, Any]  # type: ignore[explicit-any]

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


def _int(value: object) -> int:
    """Преобразовать целочисленное поле телеметрии, интерпретируя остальные значения как отсутствующие."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
