"""Стабильность инструкций, которые отправляются с каждым dispatch."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from scripts.verification.paths import ROOT

ALWAYS_SENT_INSTRUCTION_FILES = [
    ROOT / "CLAUDE.md",
    ROOT / "AGENTS.md",
    ROOT / "harness" / "orchestration" / "playbook.md",
] + sorted((ROOT / "harness" / "orchestration" / "roles").glob("*.md"))
_TIMESTAMP_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:?\d{2})?\b")
_DISPATCH_ID_RE = re.compile(r"\b(?:batch|dispatch)-[0-9a-fA-F][0-9a-fA-F-]{5,}\b", re.IGNORECASE)
_HEX_TOKEN_RE = re.compile(r"\b[0-9a-fA-F]{7,40}\b")


def _line_problems(path: Path, lineno: int, line: str) -> list[str]:
    """Признаки данных конкретного dispatch в одной строке: время, идентификатор, SHA коммита."""
    problems = []
    if _TIMESTAMP_RE.search(line):
        problems.append(f"{path}:{lineno}: looks like a timestamp: {line.strip()}")
    if _DISPATCH_ID_RE.search(line):
        problems.append(f"{path}:{lineno}: looks like a batch/dispatch identifier: {line.strip()}")
    for token in _HEX_TOKEN_RE.findall(line):
        if any(ch.isdigit() for ch in token):
            problems.append(f"{path}:{lineno}: looks like a commit SHA ({token}): {line.strip()}")
    return problems


def check_no_dispatch_specific_data_in_always_sent_files() -> None:
    """Проверить, что постоянные инструкции не содержат данных конкретного dispatch.

    Системные инструкции, манифесты ролей и playbook отправляются с каждым dispatch и должны
    оставаться стабильным кэшируемым префиксом. SHA коммита, время и идентификаторы batch/dispatch
    принадлежат только неизменяемому brief.
    """
    problems = [
        problem
        for path in ALWAYS_SENT_INSTRUCTION_FILES
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        for problem in _line_problems(path, lineno, line)
    ]
    if problems:
        sys.exit(
            "always-sent instruction files must stay free of dispatch-specific data:\n"
            + "\n".join(problems)
        )
