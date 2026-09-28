"""Textual-составляющая модуля harness.console.export: действие экспорта в Markdown, вызываемое экранами
для сформированных документов (а также экспорт в JSON для Repo Map), и вывод уведомлений оператору
о пути созданного файла."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from textual.screen import Screen

from ..export import MarkdownDocument, export_markdown, export_text

EXPORT_BINDING_KEY = "e"
EXPORT_JSON_BINDING_KEY = "j"


def _export(screen: Screen[None], repo: Path, write: Callable[[], Path]) -> Path | None:
    """Выполняет операцию записи экспорта и показывает уведомление об успехе либо ошибке на экране."""
    try:
        path = write()
    except OSError as exc:
        screen.notify(f"экспорт не удался: {exc}", severity="error")
        return None
    try:
        shown = path.relative_to(repo)
    except ValueError:
        shown = path
    screen.notify(f"экспортировано: {shown.as_posix()}")
    return path


def export_document(
    screen: Screen[None], repo: Path, document: MarkdownDocument
) -> Path | None:
    """Экспортирует Markdown-документ и уведомляет пользователя о пути сохранения."""
    return _export(screen, repo, lambda: export_markdown(repo, document))


def export_json(
    screen: Screen[None], repo: Path, text: str, *, slug: str
) -> Path | None:
    """Экспортирует текстовые данные JSON в файл и уведомляет пользователя о пути сохранения."""
    return _export(
        screen, repo, lambda: export_text(repo, text, slug=slug, suffix=".json")
    )
