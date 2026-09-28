"""Контракт `--json` (schema_version 1, задача #342): стабильная машиночитаемая форма отчёта Report, не зависящая от вывода на русском языке в render.py."""

from __future__ import annotations

from .model import JsonObject, Report


def to_json(report: Report) -> JsonObject:
    """Преобразовать отчёт `report` в JSON-объект по контракту `--json` версии схемы 1.

    Идентификатор `checks[].id` стабилен и разделен точками — тесты и сценарии чистой комнаты
    опираются на него (и на `status`), а не на текст сообщения.
    """
    return {
        "schema_version": report.schema_version,
        "repo": report.repo,
        "online": report.online,
        "summary": report.summary(),
        "checks": [
            {
                "id": check.id,
                "group": check.group,
                "status": check.status,
                "message": check.message,
                "fix": None
                if check.fix is None
                else {"text": check.fix.text, "command": check.fix.command},
            }
            for check in report.checks
        ],
        "fixes_applied": list(report.fixes_applied),
    }
