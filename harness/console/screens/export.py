"""The textual half of harness.console.export: the "export to Markdown" action every screen calls
with the document it builds, and the notice telling the operator where the file landed."""

from __future__ import annotations

from pathlib import Path

from textual.screen import Screen

from ..export import MarkdownDocument, export_markdown

EXPORT_BINDING_KEY = "e"


def export_document(
    screen: Screen[None], repo: Path, document: MarkdownDocument
) -> Path | None:
    try:
        path = export_markdown(repo, document)
    except OSError as exc:
        screen.notify(f"экспорт не удался: {exc}", severity="error")
        return None
    try:
        shown = path.relative_to(repo)
    except ValueError:
        shown = path
    screen.notify(f"экспортировано: {shown.as_posix()}")
    return path
