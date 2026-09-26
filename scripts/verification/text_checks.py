"""Проверки содержимого текстовых файлов репозитория."""

from __future__ import annotations

import sys
from pathlib import Path


def normalized_text(path: Path) -> str:
    """Прочитать текст без BOM и с переводами строк LF.

    Файл, пересохранённый редактором в UTF-8 с BOM или CRLF, не должен считаться расхождением с
    копией в UTF-8/LF.
    """
    return path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")


def grep_line(path: Path, exact_line: str) -> None:
    """Завершиться с ошибкой, если в файле нет строки, точно равной `exact_line`."""
    if exact_line not in path.read_text(encoding="utf-8").splitlines():
        sys.exit(f"{path}: expected line {exact_line!r} not found")


def grep_contains(path: Path, substring: str) -> None:
    """Завершиться с ошибкой, если файл не содержит `substring`."""
    if substring not in path.read_text(encoding="utf-8"):
        sys.exit(f"{path}: expected text {substring!r} not found")


def check_no_todo(base: Path) -> None:
    """Завершиться с ошибкой, если в каком-либо файле под `base` остался маркер TODO."""
    found = []
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        for lineno, line in enumerate(path.read_bytes().split(b"\n"), 1):
            if b"TODO" in line:
                found.append(f"{path}:{lineno}:{line.decode('utf-8', errors='backslashreplace')}")
    if found:
        print("\n".join(found))
        sys.exit("global-skills still contains TODO markers")
