"""harness.console.export: the console's shared Markdown export (Reports and Repo Map). No
textual needed."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from harness.console.export import (
    MarkdownDocument,
    MarkdownSection,
    export_directory,
    export_markdown,
    render_markdown,
)

_NOW = datetime(2026, 9, 27, 18, 5, 9, tzinfo=UTC)


def _document(ticket: str | None = "#352") -> MarkdownDocument:
    return MarkdownDocument(
        title="Completion report dispatch-qa",
        slug="report-dispatch-qa",
        ticket=ticket,
        meta=[("Ticket", "#352"), ("Role", "qa")],
        sections=[
            MarkdownSection("Output", "QA gate passed"),
            MarkdownSection(
                "QA log", "$ make test\nexit_code=0\n```nested```", preformatted=True
            ),
        ],
    )


def test_render_markdown_has_title_meta_and_sections() -> None:
    text = render_markdown(_document())
    assert text.startswith("# Completion report dispatch-qa\n")
    assert "- **Ticket:** #352\n- **Role:** qa\n" in text
    assert "## Output\n\nQA gate passed\n" in text
    # A preformatted body is fenced with a fence longer than any backtick run inside it.
    assert (
        "## QA log\n\n````text\n$ make test\nexit_code=0\n```nested```\n````\n" in text
    )
    assert text.endswith("\n")


def test_ticket_export_goes_to_the_existing_ticket_folder(tmp_path: Path) -> None:
    folder = tmp_path / "docs" / "tasks" / "issue-352-console-qa-logs"
    folder.mkdir(parents=True)
    (tmp_path / "docs" / "tasks" / "issue-3521-other").mkdir()

    path = export_markdown(tmp_path, _document(), now=_NOW)

    assert path == folder / "artifacts" / "2026-09-27-180509-report-dispatch-qa.md"
    assert path.read_text(encoding="utf-8") == render_markdown(_document())


def test_ticket_export_uses_the_epic_folder_that_holds_the_ticket(
    tmp_path: Path,
) -> None:
    epic = tmp_path / "docs" / "tasks" / "issue-341-harness-console"
    (epic / "tickets").mkdir(parents=True)
    (epic / "tickets" / "issue-352-qa-logs.md").write_text("ticket", encoding="utf-8")

    assert (
        export_directory(tmp_path, "PVMalove/claude-agent-harness#352")
        == epic / "artifacts"
    )


def test_ticket_without_a_folder_gets_a_new_one(tmp_path: Path) -> None:
    assert export_directory(tmp_path, "#352") == (
        tmp_path / "docs" / "tasks" / "issue-352" / "artifacts"
    )


def test_export_without_a_ticket_is_a_dated_file_in_console_exports(
    tmp_path: Path,
) -> None:
    for ticket in (None, "", "без номера"):
        assert (
            export_directory(tmp_path, ticket)
            == tmp_path / "docs" / "tasks" / "console-exports"
        )

    path = export_markdown(tmp_path, _document(ticket=None), now=_NOW)
    assert path == (
        tmp_path
        / "docs"
        / "tasks"
        / "console-exports"
        / "2026-09-27-180509-report-dispatch-qa.md"
    )


def test_export_never_overwrites_an_earlier_file(tmp_path: Path) -> None:
    first = export_markdown(tmp_path, _document(ticket=None), now=_NOW)
    second = export_markdown(tmp_path, _document(ticket=None), now=_NOW)

    assert first != second
    assert second.name == "2026-09-27-180509-report-dispatch-qa-2.md"
    assert first.is_file() and second.is_file()


def test_slug_is_made_filesystem_safe(tmp_path: Path) -> None:
    document = MarkdownDocument(title="t", slug="Timeline: batch/../x", sections=[])
    path = export_markdown(tmp_path, document, now=_NOW)
    assert path.parent == tmp_path / "docs" / "tasks" / "console-exports"
    assert path.name == "2026-09-27-180509-timeline-batch-x.md"
