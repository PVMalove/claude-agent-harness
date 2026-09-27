"""harness/health/ registry: it runs every registered check without early exit and builds a
HealthContext from .harness/harness.lock exactly once per run. The registry-shape tests below
monkeypatch REGISTRY to an empty list so they stay independent of which checks are wired in."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.health import registry
from harness.health.context import HealthContext
from harness.health.model import CheckResult, Report


def test_run_with_empty_registry_returns_a_schema_valid_empty_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(registry, "REGISTRY", [])
    report = registry.run(tmp_path)

    assert isinstance(report, Report)
    assert report.schema_version == 1
    assert report.repo == str(tmp_path)
    assert report.online is False
    assert report.checks == []
    assert report.fixes_applied == []
    assert report.summary() == {"ok": 0, "warn": 0, "fail": 0, "skipped": 0}


def test_run_wires_every_registered_group(tmp_path: Path) -> None:
    report = registry.run(tmp_path)

    groups = {check.group for check in report.checks}
    assert groups == {"files", "repo_map", "environment"}


def test_run_passes_online_through_to_the_report(tmp_path: Path) -> None:
    report = registry.run(tmp_path, online=True)

    assert report.online is True


def test_run_without_a_lock_file_builds_a_context_with_no_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[HealthContext] = []

    def _capture(context: HealthContext) -> CheckResult:
        captured.append(context)
        return CheckResult(id="test.probe", group="test", status="ok", message="ok")

    monkeypatch.setattr(registry, "REGISTRY", [("test.probe", _capture)])
    report = registry.run(tmp_path)

    assert captured == [HealthContext(repo=tmp_path, lock=None, online=False)]
    assert [check.id for check in report.checks] == ["test.probe"]


def test_run_parses_an_existing_lock_file_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / ".harness" / "harness.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(
        json.dumps({"capabilities": ["pvmalove-suite"]}), encoding="utf-8"
    )
    captured: list[HealthContext] = []

    def _capture(context: HealthContext) -> CheckResult:
        captured.append(context)
        return CheckResult(id="test.probe", group="test", status="ok", message="ok")

    monkeypatch.setattr(registry, "REGISTRY", [("test.probe", _capture)])
    registry.run(tmp_path)

    assert captured[0].lock == {"capabilities": ["pvmalove-suite"]}


def _ok(check_id: str) -> registry.CheckFn:
    def _check(_context: HealthContext) -> CheckResult:
        return CheckResult(id=check_id, group="test", status="ok", message="ok")

    return _check


@pytest.mark.parametrize(
    "error", [RuntimeError("boom"), FileNotFoundError("git"), SystemExit(1)]
)
def test_a_crashing_check_becomes_a_fail_and_the_run_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    def check_crashes(_context: HealthContext) -> CheckResult:
        raise error

    monkeypatch.setattr(
        registry,
        "REGISTRY",
        [
            ("test.before", _ok("test.before")),
            ("test.crashes", check_crashes),
            ("test.after", _ok("test.after")),
        ],
    )
    report = registry.run(tmp_path)

    assert [check.id for check in report.checks] == [
        "test.before",
        "test.crashes",
        "test.after",
    ]
    crashed = report.checks[1]
    assert crashed.group == "test"
    assert crashed.status == "fail"
    assert "check_crashes" in crashed.message
    assert type(error).__name__ in crashed.message


def test_keyboard_interrupt_is_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _interrupted(_context: HealthContext) -> CheckResult:
        raise KeyboardInterrupt

    monkeypatch.setattr(registry, "REGISTRY", [("test.interrupted", _interrupted)])

    with pytest.raises(KeyboardInterrupt):
        registry.run(tmp_path)


def test_every_registered_id_matches_the_id_its_check_returns(tmp_path: Path) -> None:
    """The crash fallback reports under the declared id, so it must be the check's real id."""
    report = registry.run(tmp_path)

    assert [check.id for check in report.checks] == [
        check_id for check_id, _ in registry.REGISTRY
    ]


def test_summary_counts_every_status(tmp_path: Path) -> None:
    report = Report(schema_version=1, repo=str(tmp_path), online=False)
    report.checks = [
        CheckResult(id="a", group="g", status="ok", message="m"),
        CheckResult(id="b", group="g", status="ok", message="m"),
        CheckResult(id="c", group="g", status="warn", message="m"),
        CheckResult(id="d", group="g", status="fail", message="m"),
        CheckResult(id="e", group="g", status="skipped", message="m"),
    ]

    assert report.summary() == {"ok": 2, "warn": 1, "fail": 1, "skipped": 1}
