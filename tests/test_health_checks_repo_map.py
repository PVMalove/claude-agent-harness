"""Group 'repo_map' health check: wraps the unchanged repo_map_health detection logic already
defined in harness/bin/harness.py (see harness/health/checks/_cli.py) into a CheckResult."""

from __future__ import annotations

from pathlib import Path

from harness.health.checks import repo_map as checks
from harness.health.context import HealthContext


def test_check_tier_skipped_when_repo_map_not_installed(tmp_path: Path) -> None:
    result = checks.check_tier(HealthContext(repo=tmp_path, lock=None, online=False))

    assert result.id == "repo_map.tier"
    assert result.group == "repo_map"
    assert result.status == "skipped"
    assert result.fix is None


def test_check_tier_warns_when_policy_requests_minimal(tmp_path: Path) -> None:
    (tmp_path / ".harness" / "repo_map").mkdir(parents=True)
    (tmp_path / ".harness" / "repo_map" / "repo_map.py").write_text(
        "", encoding="utf-8"
    )
    orchestration = tmp_path / ".harness" / "orchestration.json"
    orchestration.write_text(
        '{"repo_map_policy": {"tier": "minimal"}}', encoding="utf-8"
    )

    result = checks.check_tier(HealthContext(repo=tmp_path, lock=None, online=False))

    assert result.status == "warn"
    assert result.message.startswith("Repo Map: tier=minimal (requested by policy)")
    assert result.fix is None
