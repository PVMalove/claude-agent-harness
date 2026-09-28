"""Экспорт в Markdown средствами только стандартной библиотеки, общий для всех экранов консоли,
экспортирующих отображаемые данные (Reports: отчёт о завершении или хронология батча; Repo Map:
вся карта репозитория, в том числе в формате JSON).

Экран описывает своё содержимое как `MarkdownDocument`; функция `export_markdown` выполняет рендеринг
и записывает новый датированный файл (`export_text` аналогично записывает любой другой готовый текст,
например JSON карты репозитория) в каталог `docs/tasks/` целевого репозитория (см. docs/agents/artifacts.md):
собственную папку тикета `docs/tasks/issue-<N>-*/artifacts/`, папку эпика, содержащую тикет в `tickets/`,
или новую папку `docs/tasks/issue-<N>-console-export/artifacts/`; при отсутствии номера тикета используется
общая папка `docs/tasks/console-exports/`. Существующий файл никогда не перезаписывается.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

TASKS_REL = Path("docs") / "tasks"
NO_TICKET_DIR = "console-exports"
NEW_TICKET_DIR_SLUG = "console-export"
_TICKET_NUMBER = re.compile(r"(\d+)\D*$")
_BACKTICKS = re.compile(r"`+")


@dataclass(frozen=True)
class MarkdownSection:
    """Секция Markdown-документа с заголовком, телом и признаком предварительного форматирования."""

    heading: str
    body: str
    # Logs and other verbatim text: fenced so Markdown never reinterprets them.
    preformatted: bool = False


@dataclass(frozen=True)
class MarkdownDocument:
    """Структурированный Markdown-документ для экспорта артефактов консоли."""

    title: str
    slug: str
    sections: list[MarkdownSection]
    ticket: str | None = None
    meta: list[tuple[str, str]] = field(default_factory=list)


def _fenced(body: str) -> str:
    """Оборачивает текст в блок кода с ограждением, избегая конфликтов с обратными кавычками."""
    longest = max((len(run) for run in _BACKTICKS.findall(body)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{body.rstrip(chr(10))}\n{fence}"


def render_markdown(document: MarkdownDocument) -> str:
    """Формирует итоговый текст Markdown из структуры MarkdownDocument."""
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
    """Извлекает числовой номер тикета из строки идентификатора задачи."""
    match = _TICKET_NUMBER.search(ticket or "")
    return match.group(1) if match else None


def export_directory(repo: Path, ticket: str | None) -> Path:
    """Определяет директорию артефактов в docs/tasks/ для сохранения экспорта тикета или консоли."""
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
    return tasks / f"issue-{number}-{NEW_TICKET_DIR_SLUG}" / "artifacts"


def _safe_slug(slug: str) -> str:
    """Преобразует строку в безопасный фрагмент имени файла (slug), содержащий только латиницу и цифры."""
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
    """Записывает текстовые данные в новый датированный файл с уникальным именем, исключая перезапись."""
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
    """Рендерит и экспортирует Markdown-документ в файл каталога артефактов задачи."""
    return export_text(
        repo,
        render_markdown(document),
        slug=document.slug,
        suffix=".md",
        ticket=document.ticket,
        now=now,
    )
