"""Согласованность документации с каталогом capability и зеркалом docs-agents.

Каждая проверка ловит расхождение, которое уже однажды попадало в релиз: текст, продублированный
вручную в нескольких документах, расходится с источником правды.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from scripts.verification.paths import CAPABILITIES, DOCS_AGENTS_TEMPLATE, HARNESS_GUIDE, ROOT
from scripts.verification.text_checks import normalized_text

RETIRED_PATH_INVENTORY_TERM = re.compile(r"filtered\s+Repo\s+Map", re.IGNORECASE)
PATH_INVENTORY_ROOTS = (
    ROOT / "skills" / "first-party" / "pvmalove" / "to-tickets",
    ROOT / "docs" / "agents",
    DOCS_AGENTS_TEMPLATE,
    ROOT / "docs" / "skills",
    ROOT / "docs" / "diagrams",
    ROOT / "docs" / "ARCHITECTURE.md",
    ROOT / "README.md",
)


@dataclass(frozen=True)
class PvmaloveSuite:
    """Состав pvmalove-suite из CAPABILITIES.json: переопределённые и добавленные скиллы."""

    overrides: frozenset[str]
    additions: frozenset[str]


def load_pvmalove_suite() -> PvmaloveSuite:
    """Прочитать состав pvmalove-suite из CAPABILITIES.json — источника правды для документации."""
    suite = json.loads(CAPABILITIES.read_text(encoding="utf-8"))["pvmalove-suite"]
    return PvmaloveSuite(
        overrides=frozenset(suite["overrides"].keys()),
        additions=frozenset(
            entry.removeprefix("first-party/pvmalove/") for entry in suite["additions"]
        ),
    )


def _documented(pattern: str, text: str) -> set[str]:
    """Имена в обратных кавычках, найденные в тексте по шаблону строки таблицы или списка."""
    return set(re.findall(pattern, text, re.MULTILINE))


def check_docs_agents_mirror() -> None:
    """Проверить, что docs/agents/*.md совпадают с harness/project/docs-agents/*.md.

    docs/agents — собственная копия файлов, которые scaffold_pvmalove_extras разворачивает в каждый
    проект pvmalove-suite. Обе копии правятся вручную, поэтому расхождение ловится здесь.
    """
    for template in sorted(DOCS_AGENTS_TEMPLATE.glob("*.md")):
        counterpart = ROOT / "docs" / "agents" / template.name
        if not counterpart.is_file():
            sys.exit(f"{template} has no docs/agents counterpart: {counterpart}")
        if normalized_text(template) != normalized_text(counterpart):
            sys.exit(
                f"docs/agents/{template.name} has drifted from harness/project/docs-agents/{template.name}"
            )


def check_no_retired_path_inventory_term() -> None:
    """Не допустить возврата термина «filtered Repo Map».

    Артефакт /to-tickets только с путями называется «Path inventory»; «Repo Map» — только
    семантическая карта (CONTEXT.md).
    """
    for base in PATH_INVENTORY_ROOTS:
        for path in sorted(base.rglob("*") if base.is_dir() else [base]):
            if path.is_file() and RETIRED_PATH_INVENTORY_TERM.search(
                path.read_text(encoding="utf-8", errors="replace")
            ):
                sys.exit(f'{path}: retired term "filtered Repo Map"; use "Path inventory"')


def check_docs_agents_enumeration() -> None:
    """Проверить перечисление docs/agents/{...}.md в README.md и harness-guide.md.

    Оба документа вручную перечисляют файлы, которые разворачивает scaffold_pvmalove_extras. Однажды
    harness-guide.md уже отсутствовал в собственном перечислении.
    """
    names = {path.stem for path in DOCS_AGENTS_TEMPLATE.glob("*.md")}
    for doc in (ROOT / "README.md", HARNESS_GUIDE):
        match = re.search(r"docs/agents/\{([a-z0-9,-]+)\}\.md", normalized_text(doc))
        if not match:
            sys.exit(f"{doc}: no docs/agents/{{...}}.md enumeration found")
        listed = set(match.group(1).split(","))
        if listed != names:
            sys.exit(
                f"{doc}: docs/agents/{{...}}.md enumeration {sorted(listed)} does not match "
                f"harness/project/docs-agents/*.md {sorted(names)}"
            )


def _guide_section(pattern: str, missing: str) -> str:
    """Раздел harness-guide.md по регулярному выражению или завершение с ошибкой."""
    section = re.search(pattern, normalized_text(HARNESS_GUIDE), re.DOTALL)
    if not section:
        sys.exit(missing)
    return section.group(1)


def check_pvmalove_override_docs_sync() -> None:
    """Сверить таблицу раздела 7 harness-guide.md с pvmalove-suite.overrides.

    Однажды wayfinder попал в overrides без строки в таблице.
    """
    expected = set(load_pvmalove_suite().overrides)
    section = _guide_section(
        r"\n## 7\. .*?\n(.*?)\n## ",
        "harness-guide.md: could not find section 7 (local customizations)",
    )
    documented = _documented(r"^\| `([a-z0-9-]+)` \|", section)
    if documented != expected:
        sys.exit(
            "harness-guide.md section 7 table is out of sync with pvmalove-suite overrides in "
            f"CAPABILITIES.json: missing={sorted(expected - documented)} extra={sorted(documented - expected)}"
        )


def check_pvmalove_additions_docs_sync() -> None:
    """Сверить таблицу проектных скиллов harness-guide.md с pvmalove-suite.additions.

    Однажды setup-labels попал в additions без строки в таблице.
    """
    expected = set(load_pvmalove_suite().additions)
    section = _guide_section(
        r"\n### Проектные \(first-party, вне апстрима\)\n(.*?)\n---\n",
        "harness-guide.md: could not find the project-specific skill catalog table",
    )
    documented = _documented(r"^\| `([a-z0-9-]+)` \(skill\) \|", section)
    if documented != expected:
        sys.exit(
            "harness-guide.md project-specific skill table is out of sync with pvmalove-suite "
            f"additions in CAPABILITIES.json: missing={sorted(expected - documented)} extra={sorted(documented - expected)}"
        )


def _check_listed(doc: Path, text: str, pattern: str, kind: str, expected: frozenset[str]) -> None:
    """Сверить список имён в прозе документа с ожидаемым набором из CAPABILITIES.json."""
    match = re.search(pattern, text)
    if not match:
        sys.exit(f"{doc}: no pvmalove-suite {kind} list found")
    documented = set(re.findall(r"`([a-z0-9-]+)`", match.group(1)))
    if documented != expected:
        sys.exit(
            f"{doc}: {kind} list {sorted(documented)} does not match "
            f"CAPABILITIES.json {kind} {sorted(expected)}"
        )


def check_pvmalove_suite_summary_sync() -> None:
    """Сверить списки переопределений и дополнений в прозе README.md и CONTEXT.md.

    Однажды устаревшие счётчики «5 overrides, 1 addition» прожили три дня после правки только
    раздела 7.
    """
    suite = load_pvmalove_suite()
    for doc in (ROOT / "README.md", ROOT / "CONTEXT.md"):
        text = normalized_text(doc)
        _check_listed(
            doc,
            text,
            r"переопределены в `skills/first-party/pvmalove/`:\s*(.+?);",
            "overrides",
            suite.overrides,
        )
        _check_listed(doc, text, r"доп\. скиллы:\s*(.+?)\.", "additions", suite.additions)
