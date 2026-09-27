"""Stdlib-only Markdown export shared by every console screen that exports what it shows (Reports:
a completion report or a batch chronology; Repo Map: the whole map, also as JSON).

A screen describes its content as a `MarkdownDocument`; `export_markdown` renders it and writes a
new dated file (`export_text` writes any other rendered text, such as Repo Map's JSON, the same
way) under the target repository's `docs/tasks/` (see docs/agents/artifacts.md): the
ticket's own folder `docs/tasks/issue-<N>-*/artifacts/`, the epic folder that holds the ticket under
`tickets/`, or a new `docs/tasks/issue-<N>/artifacts/`; without a ticket number,
`docs/tasks/console-exports/`. An existing file is never overwritten.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

TASKS_REL = Path("docs") / "tasks"
NO_TICKET_DIR = "console-exports"
_TICKET_NUMBER = re.compile(r"(\d+)\D*$")
_BACKTICKS = re.compile(r"`+")


@dataclass(frozen=True)
class MarkdownSection:
    heading: str
    body: str
    # Logs and other verbatim text: fenced so Markdown never reinterprets them.
    preformatted: bool = False


@dataclass(frozen=True)
class MarkdownDocument:
    title: str
    slug: str
    sections: list[MarkdownSection]
    ticket: str | None = None
    meta: list[tuple[str, str]] = field(default_factory=list)


def _fenced(body: str) -> str:
    longest = max((len(run) for run in _BACKTICKS.findall(body)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{body.rstrip(chr(10))}\n{fence}"


def render_markdown(document: MarkdownDocument) -> str:
    parts = [f"# {document.title}"]
    if document.meta:
        parts.append("\n".join(f"- **{key}:** {value}" for key, value in document.meta))
    for section in document.sections:
        body = section.body.strip("\n") or "—"
        parts.append(
            f"## {section.heading}\n\n{_fenced(body) if section.preformatted else body}"
        )
    return "\n\n".join(parts) + "\n"


def _ticket_number(ticket: str | None) -> str | None:
    match = _TICKET_NUMBER.search(ticket or "")
    return match.group(1) if match else None


def export_directory(repo: Path, ticket: str | None) -> Path:
    tasks = repo / TASKS_REL
    number = _ticket_number(ticket)
    if number is None:
        return tasks / NO_TICKET_DIR
    own = sorted(
        path
        for pattern in (f"issue-{number}", f"issue-{number}-*")
        for path in tasks.glob(pattern)
        if path.is_dir()
    )
    if own:
        return own[0] / "artifacts"
    holders = sorted(
        path.parent.parent
        for pattern in (
            f"*/tickets/issue-{number}.md",
            f"*/tickets/issue-{number}-*.md",
        )
        for path in tasks.glob(pattern)
    )
    if holders:
        return holders[0] / "artifacts"
    return tasks / f"issue-{number}" / "artifacts"


def _safe_slug(slug: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-") or "export"


def export_text(
    repo: Path,
    text: str,
    *,
    slug: str,
    suffix: str,
    ticket: str | None = None,
    now: datetime | None = None,
) -> Path:
    directory = export_directory(repo, ticket)
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{(now or datetime.now(UTC)):%Y-%m-%d-%H%M%S}-{_safe_slug(slug)}"
    number = 1
    while True:
        path = directory / (
            f"{stem}{suffix}" if number == 1 else f"{stem}-{number}{suffix}"
        )
        try:
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
        except FileExistsError:
            number += 1
            continue
        return path


def export_markdown(
    repo: Path, document: MarkdownDocument, *, now: datetime | None = None
) -> Path:
    return export_text(
        repo,
        render_markdown(document),
        slug=document.slug,
        suffix=".md",
        ticket=document.ticket,
        now=now,
    )
