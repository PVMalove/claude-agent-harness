"""The `--json` contract (schema_version 1, ticket #342): a stable, machine-readable shape for a
Report, independent of the Russian text rendering in render.py."""

from __future__ import annotations

from harness.health.model import JsonObject, Report


def to_json(report: Report) -> JsonObject:
    """Render `report` as the schema_version-1 --json contract.

    `checks[].id` is stable and dot-separated - clean-room scenarios and unit tests key off it
    (and `status`) instead of message text.
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
