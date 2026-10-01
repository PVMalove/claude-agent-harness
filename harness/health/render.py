"""Сгруппированный вывод отчёта Report на русском языке с ASCII-fallback для терминалов без поддержки эмодзи (задача #342)."""

from __future__ import annotations

import sys
from typing import Protocol

from .model import Report, Status


class _EncodingAware(Protocol):
    """Протокол потока вывода, предоставляющий кодировку текста для `supports_markers` и `render_text`."""

    @property
    def encoding(self) -> str | None:
        """Текстовая кодировка потока вывода."""
        ...


_MARKERS: dict[Status, str] = {"ok": "✅", "warn": "⚠️", "fail": "❌"}
_ASCII_MARKERS: dict[Status, str] = {"ok": "[OK]", "warn": "[WARN]", "fail": "[FAIL]"}
_SKIPPED_MARKER = "-"

# Human (Russian) labels for the machine group tokens on CheckResult; the token itself is never
# localized (it is part of the --json contract). Falls back to the raw token for a future group
# this dictionary has not been updated for yet.
GROUP_LABELS_RU: dict[str, str] = {
    "files": "Файлы харнесса",
    "repo_map": "Repo Map",
    "memory": "Память проекта",
    "environment": "Окружение",
    "directories": "Каталоги харнесса",
    "tracker": "Трекер задач",
    "orchestration": "Оркестрация",
}

_ACTIVATION_FOOTER = "activation: verify advertised and invoked skills/integrations in a fresh runtime session"


def supports_markers(stream: _EncodingAware) -> bool:
    """Проверить, поддерживает ли поток вывод эмодзи-маркеров статусов по умолчанию."""
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        "".join(_MARKERS.values()).encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def _marker(status: Status, *, ascii_fallback: bool) -> str:
    """Получить строковый маркер статуса проверки (эмодзи, ASCII-вариант или прочерк для skipped)."""
    if status == "skipped":
        return _SKIPPED_MARKER
    return (_ASCII_MARKERS if ascii_fallback else _MARKERS)[status]


def _group_order(report: Report) -> list[str]:
    """Получить список групп в порядке их первого появления в отчёте."""
    seen: list[str] = []
    for check in report.checks:
        if check.group not in seen:
            seen.append(check.group)
    return seen


def render_text(report: Report, *, stream: _EncodingAware | None = None) -> str:
    """Сформировать текстовое представление `report`, сгруппированное по категориям на русском языке.

    Для проверок с предложенным исправлением добавляется строка "-> Как исправить: ".
    Возвращает сформированный текст для последующей записи вызывающей стороной в `stream`.

    Если `stream` не передан, используется `sys.stdout` на момент вызова, что учитывает
    возможную перенастройку кодировки точкой входа CLI.
    """
    ascii_fallback = not supports_markers(stream if stream is not None else sys.stdout)
    lines: list[str] = []
    for group in _group_order(report):
        lines.append(f"== {GROUP_LABELS_RU.get(group, group)} ==")
        for check in report.checks:
            if check.group != group:
                continue
            marker = _marker(check.status, ascii_fallback=ascii_fallback)
            lines.append(f"{marker} {check.message}")
            if check.fix is not None:
                lines.append(f"-> Как исправить: {check.fix.text}")
                if check.fix.command:
                    lines.append(f"   {check.fix.command}")
    if report.fixes_applied:
        lines.append("== Исправлено (--fix) ==")
        lines.extend(f"- {applied}" for applied in report.fixes_applied)
    summary = report.summary()
    lines.append(
        "Итого: ok={ok} warn={warn} fail={fail} skipped={skipped}".format(**summary)
    )
    lines.append(_ACTIVATION_FOOTER)
    return "\n".join(lines) + "\n"
