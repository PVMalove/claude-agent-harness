"""Модульные тесты парсера GFM-таблиц канонических меток."""

from __future__ import annotations

from pathlib import Path

from harness.health.labels_table import parse_canonical_labels

HARNESS_ROOT = Path(__file__).resolve().parents[2]


def test_parses_a_simple_label_color_table() -> None:
    """Проверить, что простая таблица меток и цветов корректно парсится."""
    text = """
| Label | Color | Meaning |
| --- | --- | --- |
| `hitl` | yellow `#fbca04` | Human-in-the-loop |
| `afk` | light blue `#54c1e8` | Away-from-keyboard |
"""
    assert parse_canonical_labels(text) == [
        ("hitl", "#fbca04"),
        ("afk", "#54c1e8"),
    ]


def test_ignores_a_table_without_a_color_column() -> None:
    """Проверить, что таблица без колонки цвета игнорируется."""
    text = """
| Label | Meaning |
| --- | --- |
| `type::bug` | Something is broken |
"""
    assert parse_canonical_labels(text) == []


def test_reads_multiple_tables_and_normalizes_case() -> None:
    """Проверить, что считываются несколько таблиц с нормализацией регистра цветов."""
    text = """
## Axis A

| Label | Color | Meaning |
| --- | --- | --- |
| `status::ready` | green `#0E8A16` | Ready |

## Axis B

| Label | Color | Applied by | Meaning |
| --- | --- | --- | --- |
| `task-report::required` | gray `#6a737d` | `/to-spec` | Required |
"""
    assert parse_canonical_labels(text) == [
        ("status::ready", "#0e8a16"),
        ("task-report::required", "#6a737d"),
    ]


def test_duplicate_label_keeps_first_occurrence() -> None:
    """Проверить, что при наличии дубликатов меток сохраняется первое вхождение."""
    text = """
| Label | Color | Meaning |
| --- | --- | --- |
| `hitl` | `#111111` | first |
| `hitl` | `#222222` | second |
"""
    assert parse_canonical_labels(text) == [("hitl", "#111111")]


def test_label_without_code_span_and_bare_hex_color() -> None:
    """Проверить, что распознаются метки без обратных кавычек и шестнадцатеричные цвета без префикса."""
    text = """
| Label | Color |
| --- | --- |
| bare-label | #abcdef |
"""
    assert parse_canonical_labels(text) == [("bare-label", "#abcdef")]


def test_row_missing_a_color_cell_is_skipped() -> None:
    """Проверить, что строки без корректного шестнадцатеричного цвета пропускаются."""
    text = """
| Label | Color | Meaning |
| --- | --- | --- |
| `no-color` | no hex here | Missing color |
| `ok-label` | `#123456` | Fine |
"""
    assert parse_canonical_labels(text) == [("ok-label", "#123456")]


def test_no_tables_returns_empty() -> None:
    """Проверить, что текст без таблиц возвращает пустой список меток."""
    assert parse_canonical_labels("Just prose, no tables here.\n") == []


def test_repository_triage_labels_are_recognized_with_expected_colors() -> None:
    """Проверить, что канонические метки репозитория распознаются с ожидаемыми цветами."""
    text = (HARNESS_ROOT / "docs" / "agents" / "triage-labels.md").read_text(
        encoding="utf-8"
    )

    labels = dict(parse_canonical_labels(text))

    assert labels["status::ready"] == "#0e8a16"
    assert labels["hitl"] == "#fbca04"
    assert labels["afk"] == "#54c1e8"
    assert labels["task-report::required"] == "#6a737d"
