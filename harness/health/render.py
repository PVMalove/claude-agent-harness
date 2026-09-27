"""Grouped, Russian text rendering of a Report, with an ASCII fallback for stdout that cannot
encode the (OK)/(WARN)/(FAIL) markers (ticket #342)."""

from __future__ import annotations

import sys
from typing import Protocol

from .model import Report, Status


class _EncodingAware(Protocol):
    """The only thing `supports_markers`/`render_text` need from a stream: its text encoding."""

    @property
    def encoding(self) -> str | None: ...


_MARKERS: dict[Status, str] = {"ok": "✅", "warn": "⚠️", "fail": "❌"}
_ASCII_MARKERS: dict[Status, str] = {"ok": "[OK]", "warn": "[WARN]", "fail": "[FAIL]"}
_SKIPPED_MARKER = "-"

# Human (Russian) labels for the machine group tokens on CheckResult; the token itself is never
# localized (it is part of the --json contract). Falls back to the raw token for a future group
# this dictionary has not been updated for yet.
GROUP_LABELS_RU: dict[str, str] = {
    "files": "Файлы харнесса",
    "repo_map": "Repo Map",
    "environment": "Окружение",
    "directories": "Каталоги харнесса",
    "tracker": "Трекер задач",
    "orchestration": "Оркестрация",
}

_ACTIVATION_FOOTER = "activation: verify advertised and invoked skills/integrations in a fresh runtime session"


def supports_markers(stream: _EncodingAware) -> bool:
    """Whether `stream` can encode the emoji status markers used by the default text format."""
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        "".join(_MARKERS.values()).encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def _marker(status: Status, *, ascii_fallback: bool) -> str:
    if status == "skipped":
        return _SKIPPED_MARKER
    return (_ASCII_MARKERS if ascii_fallback else _MARKERS)[status]


def _group_order(report: Report) -> list[str]:
    """Groups in the order they first appear in REGISTRY, not alphabetical."""
    seen: list[str] = []
    for check in report.checks:
        if check.group not in seen:
            seen.append(check.group)
    return seen


def render_text(report: Report, *, stream: _EncodingAware | None = None) -> str:
    """Render `report` grouped by group, in Russian, with a "-> Как исправить: " line under any
    check that has a fix. Returns the rendered text; the caller writes it to `stream`.

    `stream` defaults to `sys.stdout` at call time (not at import time) so it reflects any
    encoding reconfiguration the CLI entry point does before calling this.
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
