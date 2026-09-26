"""Result and fix models the health-check registry works with (see registry.py)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# Raw JSON crosses into this package at exactly two points: the parsed harness.lock
# (context.HealthContext) and the --json report (report_json.to_json). Matches the existing idiom
# in harness/reporting/common.py.
JsonObject = dict[str, Any]  # type: ignore[explicit-any]

Status = Literal["ok", "warn", "fail", "skipped"]
_STATUSES: tuple[Status, ...] = ("ok", "warn", "fail", "skipped")


@dataclass(frozen=True)
class Fix:
    """An actionable remedy attached to a non-ok :class:`CheckResult`."""

    text: str
    command: str | None = None


@dataclass(frozen=True)
class CheckResult:
    """One health check's outcome.

    `id` is a stable, dot-separated identifier and part of the `--json` contract. `group` is a
    machine token; it is grouped and given a human (Russian) label only in render.py, never
    localized here. `message` is always a human-readable Russian sentence, even for
    `status == "ok"`.
    """

    id: str
    group: str
    status: Status
    message: str
    fix: Fix | None = None


@dataclass
class Report:
    """The full outcome of one `harness health` run."""

    schema_version: int
    repo: str
    online: bool
    checks: list[CheckResult] = field(default_factory=list)
    fixes_applied: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {status: 0 for status in _STATUSES}
        for check in self.checks:
            counts[check.status] += 1
        return counts
