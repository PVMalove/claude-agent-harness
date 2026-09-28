"""Парсер на стандартной библиотеке для канонических таблиц меток целевого проекта
из `docs/agents/triage-labels.md` (проверка tracker.labels из задачи #346 читает их во время выполнения;
этот модуль не содержит собственных фиксированных данных о метках).

Считываются только таблицы GFM, строка заголовка которых содержит обе колонки — `Label` и `Color`:
ось типов (`type::*`) и чисто информационные оси `priority::*`/`severity::*`/`env::*` не имеют
колонки Color и намеренно пропускаются, в точности как на первом шаге команды `/setup-labels`.
"""

from __future__ import annotations

import re

_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
_CODE_SPAN = re.compile(r"`([^`]+)`")
_SEPARATOR_CELL = re.compile(r":?-+:?")


def _cells(line: str) -> list[str]:
    """Разбить строку markdown-таблицы на отдельные ячейки, очистив внешние пробелы."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _is_separator_row(cells: list[str]) -> bool:
    """Проверить, является ли строка ячеек разделителем заголовка таблицы (тире и двоеточия)."""
    return bool(cells) and all(
        cell == "" or _SEPARATOR_CELL.fullmatch(cell) for cell in cells
    )


def _clean_label(cell: str) -> str | None:
    """Извлечь имя метки из ячейки, удаляя обратные кавычки Markdown при их наличии."""
    match = _CODE_SPAN.search(cell)
    name = (match.group(1) if match else cell).strip()
    return name or None


def _clean_color(cell: str) -> str | None:
    """Извлечь шестнадцатеричный код цвета `#rrggbb` в нижнем регистре из текста ячейки."""
    match = _HEX_COLOR.search(cell)
    return match.group(0).lower() if match else None


def parse_canonical_labels(text: str) -> list[tuple[str, str]]:
    """Разобрать все GFM-таблицы в тексте, содержащие в заголовке колонки `Label` и `Color`.

    Возвращает пары `(name, "#rrggbb")` в порядке следования в документе. Дублирующиеся
    имена меток сохраняют только первое вхождение. Обрамляющие обратные кавычки кода
    (например, `` `hitl` ``) снимаются; окружающий текст ячейки с цветом (например, "yellow `#fbca04`")
    игнорируется, сохраняется только шестнадцатеричный код.
    """
    lines = text.splitlines()
    labels: list[tuple[str, str]] = []
    seen: set[str] = set()
    index = 0
    while index < len(lines) - 1:
        header_cells = _cells(lines[index])
        separator_cells = _cells(lines[index + 1])
        if (
            len(header_cells) >= 2
            and len(separator_cells) == len(header_cells)
            and _is_separator_row(separator_cells)
        ):
            lowered = [cell.lower() for cell in header_cells]
            if "label" in lowered and "color" in lowered:
                label_index = lowered.index("label")
                color_index = lowered.index("color")
                row = index + 2
                while row < len(lines) and lines[row].strip().startswith("|"):
                    cells = _cells(lines[row])
                    if len(cells) > max(label_index, color_index):
                        name = _clean_label(cells[label_index])
                        color = _clean_color(cells[color_index])
                        if name and color and name not in seen:
                            labels.append((name, color))
                            seen.add(name)
                    row += 1
                index = row
                continue
        index += 1
    return labels
