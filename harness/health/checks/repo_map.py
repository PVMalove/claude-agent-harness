"""Group 'repo_map': the Repo Map dispatch-tier check migrated from `harness/bin/harness`'s old
`cmd_health` (`repo_map_health`). Detection logic is unchanged; see the `_cli` bridge docstring."""

from __future__ import annotations

from ._cli import cli
from ..context import HealthContext
from ..model import CheckResult, Fix, Status

_REMEDY_PREFIX = "КАК ИСПРАВИТЬ: "


def check_tier(context: HealthContext) -> CheckResult:
    lines = cli().repo_map_health(context.repo)
    if not lines:
        return CheckResult(
            id="repo_map.tier", group="repo_map", status="skipped",
            message="Repo Map не установлен в проект",
        )
    status: Status = "ok" if lines[0].startswith("Repo Map: tier=full") else "warn"
    message = "; ".join(lines[:3])
    fix = None
    if status == "warn":
        for line in lines:
            if line.startswith(_REMEDY_PREFIX):
                fix = Fix(text=line.removeprefix(_REMEDY_PREFIX))
                break
    return CheckResult(id="repo_map.tier", group="repo_map", status=status, message=message, fix=fix)
