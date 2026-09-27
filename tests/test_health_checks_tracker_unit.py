"""In-process unit tests for the tracker group's timeout and missing-tool contract (ticket #346):
every external call gets a 10 second timeout, and a missing gh/glab warns instead of failing the
whole check. The end-to-end scenarios (fake gh/glab/git on PATH, run as a subprocess) live in
test_health_checks_tracker.py; these tests instead monkeypatch subprocess.run and shutil.which
directly so the timeout and missing-tool paths are exercised without spawning a process."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from harness.health.checks import tracker
from harness.health.context import HealthContext


def _online_context(tmp_path: Path) -> HealthContext:
    return HealthContext(repo=tmp_path, lock=None, online=True)


def test_online_timeout_constant_is_ten_seconds() -> None:
    assert tracker._ONLINE_TIMEOUT_SECONDS == 10


def test_run_returns_none_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_timeout(
        *_args: object, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="gh", timeout=tracker._ONLINE_TIMEOUT_SECONDS)

    monkeypatch.setattr(subprocess, "run", _raise_timeout)

    assert tracker._run(["gh", "auth", "status"]) is None


def test_run_passes_the_online_timeout_to_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def _fake_run(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    tracker._run(["git", "ls-remote", "origin"])

    assert seen["timeout"] == tracker._ONLINE_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    ("check_fn", "check_id"),
    [
        (tracker.check_auth, "tracker.auth"),
        (tracker.check_permissions, "tracker.permissions"),
    ],
)
def test_missing_tracker_tool_warns_instead_of_failing(
    check_fn: object,
    check_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tracker, "detect_tracker", lambda _context: ("github", "acme/widgets"))
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    context = _online_context(tmp_path)

    result = check_fn(context)  # type: ignore[operator]

    assert result.id == check_id
    assert result.status == "warn"


def test_missing_git_warns_reachability_instead_of_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        tracker, "detect_tracker", lambda _context: ("github", "acme/widgets")
    )
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    context = _online_context(tmp_path)

    result = tracker.check_reachability(context)

    assert result.status == "warn"


def test_a_stalled_git_call_fails_reachability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        tracker, "detect_tracker", lambda _context: ("github", "acme/widgets")
    )
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(tracker, "_run", lambda *_args, **_kwargs: None)
    context = _online_context(tmp_path)

    result = tracker.check_reachability(context)

    assert result.status == "fail"
    assert str(tracker._ONLINE_TIMEOUT_SECONDS) in result.message
