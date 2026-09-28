"""Нестрогие читатели для JSON-данных, которые консоль только отображает (записи журнала, отчёты
о завершении, карта репозитория): поле неверного типа считывается как пустое значение вместо сбоя экрана."""

from __future__ import annotations


def text(value: object, default: str = "") -> str:
    """Возвращает строковое значение, если объект является строкой, иначе значение по умолчанию."""
    return value if isinstance(value, str) else default


def strings(value: object) -> list[str]:
    """Возвращает список строковых элементов из переданного объекта, отбрасывая элементы других типов."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
