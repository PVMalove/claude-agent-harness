"""The textual half of harness.console.export: the "export to Markdown" action every screen calls
with the document it builds (and Repo Map's "export to JSON"), and the notice telling the operator
where the file landed."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from textual.screen import Screen

from ..export import MarkdownDocument, export_markdown, export_text

EXPORT_BINDING_KEY = "e"
EXPORT_JSON_BINDING_KEY = "j"


def _export(screen: Screen[None], repo: Path, write: Callable[[], Path]) -> Path | None:
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
    return _export(screen, repo, lambda: export_markdown(repo, document))


def export_json(
    screen: Screen[None], repo: Path, text: str, *, slug: str
) -> Path | None:
    return _export(
        screen, repo, lambda: export_text(repo, text, slug=slug, suffix=".json")
    )
