"""Health package hardening: malformed overlay locks, bounded skill inventory, the shared
backend-orchestration predicate and invariant errors instead of asserts."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness.errors import INTERNAL_INVARIANT_REMEDY, HarnessError
from harness.health import project_files
from harness.health.checks import files as files_checks
from harness.health.checks import orchestration as orchestration_checks
from harness.health.checks import tracker as tracker_checks
from harness.health.context import HealthContext
from harness.health.project_tracker import ProjectTracker
from harness.orchestration.core import constants


def test_an_overlay_lock_that_is_not_an_object_is_a_problem(tmp_path: Path) -> None:
    (tmp_path / ".harness" / "skills").mkdir(parents=True)
    overlays = tmp_path / ".harness" / "overlays"
    overlays.mkdir()
    (overlays / "vendor.lock").write_text("[]", encoding="utf-8")
    problems: list[str] = []
    project_files.validate_overlay_locks(tmp_path, {}, problems)
    assert problems == ["invalid overlay lock header: .harness/overlays/vendor.lock"]


def test_a_skill_inventory_that_does_not_answer_fails_with_a_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    timeouts: list[object] = []

    def hang(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        timeouts.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(SystemExit):
        project_files.project_skill_files(tmp_path, tmp_path / "skill")
    assert "cannot inventory project skill files" in capsys.readouterr().err
    assert timeouts == [project_files.GIT_TIMEOUT_SECONDS]


@pytest.mark.parametrize(
    ("lock", "enabled"),
    [
        (None, False),
        ({}, False),
        ({"capabilities": None}, False),
        ({"capabilities": ["pvmalove-suite"]}, False),
        ({"capabilities": ["pvmalove-suite", "backend-orchestration"]}, True),
    ],
)
def test_orchestration_enabled_reads_the_lock_capabilities(
    tmp_path: Path, lock: dict[str, object] | None, enabled: bool
) -> None:
    context = HealthContext(repo=tmp_path, lock=lock, online=False)
    assert context.orchestration_enabled() is enabled


def test_a_hosted_tracker_without_a_host_is_an_internal_error() -> None:
    with pytest.raises(HarnessError) as raised:
        tracker_checks._hosted(
            ProjectTracker("github", None, "group/project", "config")
        )
    assert raised.value.remedy == INTERNAL_INVARIANT_REMEDY


def test_a_non_integer_built_in_stale_threshold_is_an_internal_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        constants.DEFAULT_ATTENTION_POLICY, "stale_dispatch_seconds", "soon"
    )
    with pytest.raises(HarnessError) as raised:
        orchestration_checks._attention_policy_threshold(tmp_path)
    assert raised.value.remedy == INTERNAL_INVARIANT_REMEDY


def test_a_non_utf8_orchestration_config_skips_verification_routing(
    tmp_path: Path,
) -> None:
    config = tmp_path / ".harness" / "orchestration.json"
    config.parent.mkdir()
    config.write_bytes(b'{"verification_commands": ["\xff"]}')
    context = HealthContext(
        repo=tmp_path, lock={"capabilities": ["backend-orchestration"]}, online=False
    )
    assert files_checks.check_verification_routing(context).status == "skipped"
    assert project_files.verification_routing_health(tmp_path) == []
