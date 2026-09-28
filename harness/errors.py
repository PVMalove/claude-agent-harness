"""Общий базовый класс ошибок для Python-модулей harness.

Каждое специфичное для harness исключение содержит `.message` (описание проблемы) и обязательный
именованный параметр `.remedy` (конкретное действие для исправления) вместо простой строки,
чтобы вызывающая сторона могла отреагировать на ошибку без чтения исходного кода.
См. docs/adr/0007-python-quality.md.
"""

from __future__ import annotations

import sys


class HarnessError(Exception):
    """Базовый класс для всех ошибок harness. Подклассы не добавляют собственного поведения."""

    def __init__(self, message: str, *, remedy: str) -> None:
        """Инициализировать ошибку сообщением и рекомендацией по исправлению."""
        super().__init__(message)
        self.message = message
        self.remedy = remedy


class PolicyError(HarnessError):
    """Проектная политика не может быть безопасно применена."""


INTERNAL_INVARIANT_REMEDY = (
    "internal invariant violated -- file a bug report with this traceback"
)


def print_and_exit(exc: HarnessError) -> int:
    """Вывести сообщение и рекомендацию HarnessError в stderr и вернуть код завершения CLI.

    Возвращает 2 вместо прямого вызова `sys.exit`, сохраняя стандартный контракт точек входа.
    """
    print(f"ERROR: {exc.message}\nREMEDY: {exc.remedy}", file=sys.stderr)
    return 2
